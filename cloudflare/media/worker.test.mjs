import { test } from 'node:test';
import assert from 'node:assert/strict';
import { byteRange, serveMedia } from './worker.mjs';

const origin = 'https://beatit.changkyun.kim';
const id = 'a'.repeat(32);
const url = `${origin}/media/${id}/audio.wav`;
const bytes = new TextEncoder().encode('0123456789');
function fixture(status = 200) {
  const authorization = [], reads = [];
  const env = { APP_ORIGIN: origin, MEDIA: {
    async get(key, options) {
      reads.push({ key, options });
      const range = options?.range;
      const body = range ? bytes.slice(range.offset, range.offset + range.length) : bytes;
      return { size: bytes.length, body };
    },
    async head(key) { reads.push({ key, head: true }); return { size: bytes.length }; },
  } };
  const fetchOrigin = async (target, options) => {
    authorization.push({ target: String(target), options });
    return Response.json(status === 200 ? {
      object_key: 'owner/job/hash/audio.wav', size: bytes.length,
      sha256: 'f'.repeat(64), content_type: 'audio/wav',
    } : { detail: 'not allowed' }, { status });
  };
  const request = (headers = {}, method = 'GET', path = url) =>
    new Request(path, { method, headers: { Cookie: '__Host-beatit-session=opaque', ...headers } });
  return { env, fetchOrigin, authorization, reads, request };
}

test('full bytes come from R2 after one fresh owner authorization, with no shared caching', async () => {
  const f = fixture();
  const response = await serveMedia(f.request(), f.env, f.fetchOrigin);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '0123456789');
  assert.equal(f.authorization[0].target, `${origin}/api/jobs/${id}/media-authorize?source=audio.wav`);
  assert.equal(f.authorization[0].options.headers.Cookie, '__Host-beatit-session=opaque');
  assert.equal(f.authorization[0].options.redirect, 'manual');
  assert.equal(response.headers.get('Cache-Control'), 'private, no-store');
  assert.equal(response.headers.get('Vary'), 'Cookie');
  assert.equal(response.headers.get('X-Beatit-Media'), 'r2-edge');
  assert.equal(response.headers.get('Content-Length'), '10');
  assert.equal(f.reads.length, 1);
});

for (const [value, expected, content] of [
  ['bytes=2-5', { offset: 2, length: 4 }, '2345'],
  ['bytes=7-', { offset: 7, length: 3 }, '789'],
  ['bytes=-3', { offset: 7, length: 3 }, '789'],
  ['bytes=8-100', { offset: 8, length: 2 }, '89'],
]) test(`R2 receives the exact partial range ${value}`, async () => {
  const f = fixture();
  const response = await serveMedia(f.request({ Range: value }), f.env, f.fetchOrigin);
  assert.equal(response.status, 206);
  assert.equal(await response.text(), content);
  assert.deepEqual(f.reads[0].options.range, expected);
  assert.equal(response.headers.get('Content-Range'), `bytes ${expected.offset}-${expected.offset + expected.length - 1}/10`);
});

for (const range of ['bytes=10-', 'bytes=3-1', 'bytes=-0', 'bytes=-', 'bytes=0-1,5-6', 'bytes=9999999999999999999999-'])
  test(`invalid range ${range} never reads R2`, async () => {
    const f = fixture();
    const response = await serveMedia(f.request({ Range: range }), f.env, f.fetchOrigin);
    assert.equal(response.status, 416);
    assert.equal(response.headers.get('Content-Range'), 'bytes */10');
    assert.equal(f.reads.length, 0);
  });

test('If-Range mismatch returns the full current object', async () => {
  const f = fixture();
  const response = await serveMedia(f.request({ Range: 'bytes=2-5', 'If-Range': '"old"' }), f.env, f.fetchOrigin);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '0123456789');
});
test('HEAD is authorized and does not download an object body', async () => {
  const f = fixture();
  const response = await serveMedia(f.request({}, 'HEAD'), f.env, f.fetchOrigin);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '');
  assert.deepEqual(f.reads, [{ key: 'owner/job/hash/audio.wav', head: true }]);
});
for (const status of [401, 403, 404, 503]) test(`authorization ${status} fails closed before touching R2`, async () => {
  const f = fixture(status);
  const response = await serveMedia(f.request(), f.env, f.fetchOrigin);
  assert.equal(response.status, status);
  assert.equal(f.reads.length, 0);
});
test('each subsequent range is reauthorized; revocation denies the next read', async () => {
  const f = fixture();
  assert.equal((await serveMedia(f.request({ Range: 'bytes=0-1' }), f.env, f.fetchOrigin)).status, 206);
  const revoked = fixture(401);
  assert.equal((await serveMedia(f.request({ Range: 'bytes=2-3' }), f.env, revoked.fetchOrigin)).status, 401);
  assert.equal(f.reads.length, 1);
});
test('unauthenticated, foreign-origin, mutation and arbitrary object-key paths cannot reach R2', async () => {
  const f = fixture();
  for (const [request, status] of [
    [new Request(url), 401],
    [f.request({ 'Sec-Fetch-Site': 'cross-site' }), 403],
    [f.request({}, 'POST'), 405],
    [f.request({}, 'GET', `${origin}/media/${id}/score-bundle.zip`), 404],
    [f.request({}, 'GET', `${url}?object_key=another-owner`), 404],
    [f.request({}, 'GET', url.replace(origin, 'https://other.example')), 404],
  ]) assert.equal((await serveMedia(request, f.env, f.fetchOrigin)).status, status);
  assert.equal(f.authorization.length, 0);
  assert.equal(f.reads.length, 0);
});
test('storage and auth outages are recoverable 503 responses', async () => {
  const f = fixture();
  f.env.MEDIA.get = async () => { throw new Error('private storage details'); };
  const response = await serveMedia(f.request(), f.env, f.fetchOrigin);
  assert.equal(response.status, 503);
  assert.equal(response.headers.get('Retry-After'), '5');
  assert.ok(!(await response.text()).includes('private storage details'));
});
test('empty range means a full response', () => assert.equal(byteRange(null, 10), null));

test('source URLs can serve the authorized compact AAC preview without exposing the original key', async () => {
  const f = fixture();
  const response = await serveMedia(f.request(), f.env, async () => Response.json({
    object_key: 'owner/job/immutable/audio-preview.m4a', size: bytes.length,
    sha256: 'f'.repeat(64), content_type: 'audio/mp4',
  }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Content-Type'), 'audio/mp4');
  assert.equal(f.reads[0].key, 'owner/job/immutable/audio-preview.m4a');
  assert.equal(await response.text(), '0123456789');
});
