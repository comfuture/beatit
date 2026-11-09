# Private media delivery

Beatit can serve playback bytes directly from private R2 through a Cloudflare
Worker while the Python application continues to authorize every request. Only
session/ownership/manifest metadata crosses the application origin path; the audio
body flows from R2 to the Worker to the browser.

This document describes the implementation and rollout procedure. The earlier
`dcef260` [managed deployment record](benchmarks/multiuser-managed-2026-11-09.json)
precedes this media change. A new image, Worker release and deployed browser result
must be recorded separately; the measurements below are a pre-deployment origin
diagnostic, not evidence that the Worker is already deployed.

## Request and authorization path

```mermaid
sequenceDiagram
    participant Browser
    participant Worker as Same-origin media Worker
    participant API as Beatit API
    participant DB as Managed MySQL
    participant R2 as Private R2
    Browser->>Worker: GET /media/job/source + cookie + optional Range
    Worker->>API: GET /api/jobs/job/media-authorize?source=... + cookie
    API->>DB: Verify session, owner and artifact manifest
    DB-->>API: Authorized immutable object reference
    API-->>Worker: Object key, size, SHA-256, content type
    Worker->>R2: Get object or requested byte range
    R2-->>Worker: Readable stream
    Worker-->>Browser: 200 or 206 streaming response
```

The implemented route is `/media/{job}/{source}`, with a 32-character job ID and a
fixed audio allowlist: `audio.wav`, `drums.wav`, and the five `stem-*.flac` kit
tracks. Arbitrary keys, score downloads and query-string substitutions are not
accepted. The Worker forwards the browser's existing HttpOnly session cookie to
the same-origin API; it does not decode a client identity or trust a user ID in the
URL. The API checks the revocable database session, owner and committed artifact
manifest without downloading R2 bytes or hydrating application scratch.

Each GET, HEAD and range request obtains a fresh authorization decision. There is
no permission cache or reusable signed object URL. Anonymous requests receive 401;
an authenticated different owner receives 404. Revocation or expiry causes the
next request to receive 401. A missing object is 404; an unavailable authorization
or storage dependency fails closed with 503. The media route does not weaken the
existing API's ownership checks.

All responses use `Cache-Control: private, no-store` and `Vary: Cookie`.
`Cross-Origin-Resource-Policy: same-origin` and an explicit cross-site request
check keep the route on the application origin. No public bucket, browser-held R2
credential, CORS relaxation or bearer download link is introduced. Score/ZIP
exports remain on the authenticated API download path.

[R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
authorize their holder until expiry. For Beatit, this implies that revoking the
application session would not revoke an already issued direct R2 link. Rechecking the database for every media request
retains Beatit's session-revocation behavior without waiting for a URL TTL. That
check occurs when the request starts: logout cannot recall bytes already delivered
or cached in the player's memory, and it does not cancel an already authorized
in-flight response. The next network request must authorize again. Origin/database
availability and authorization latency therefore remain part of every new request.

## Streaming and ranges

The Worker uses its R2 binding's `get()` body directly as the HTTP response stream.
It requests a bounded R2 range when the browser requests one; it does not buffer or
download the whole audio file before returning that range. HEAD reads object
metadata without downloading a body. The Worker checks the returned object's total
size against the authorized manifest. Its quoted ETag is the manifest's SHA-256;
it does not recompute a full-file hash for each streamed range.

| Request | Response contract |
| --- | --- |
| GET without Range | 200, full object body |
| HEAD without Range | 200, object headers and no body |
| `Range: bytes=100-199` | 206 for those bytes, clipped at the object end |
| `Range: bytes=100-` | 206 from the offset through the end |
| `Range: bytes=-100` | 206 for the final 100 bytes, or the whole shorter object |
| Unsatisfiable, malformed or multipart range | 416 with `Content-Range: bytes */SIZE` |
| If-Range matching the current quoted ETag | Honor the requested single range |
| If-Range mismatch | 200 with the full current object |

Responses include `Accept-Ranges`, accurate `Content-Length`, and `Content-Range`
for partial responses. `X-Beatit-Media: r2-edge` identifies this delivery path.
Only GET and HEAD are accepted. The implementation supports single byte ranges;
it does not implement multipart range responses.

Cloudflare's [R2 Workers API reference](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)
documents bucket bindings, metadata-only `head()`, ranged `get()` and streaming
object bodies. The [Worker routes documentation](https://developers.cloudflare.com/workers/configuration/routing/routes/)
describes routing a selected URL pattern on an existing proxied hostname. Beatit's
`/media/*` route leaves `/api/*` on the existing application origin path.

## Configuration and rollout

`BEATIT_MEDIA_BASE_URL` accepts exactly two values:

| Value | Playback route |
| --- | --- |
| Empty string, the default | Existing authenticated `/api/jobs/{job}/files/{source}` relay |
| `/media` | Same-origin Worker `/media/{job}/{source}` |

An arbitrary host or URL is rejected at application startup. Compose passes this
setting explicitly, and the authenticated `/api/system` response reports it as
`media_base_url`. The Worker expects the canonical `APP_ORIGIN`, the existing
private bucket bound as `MEDIA`, and a route limited to
`beatit.changkyun.kim/media/*`. Its configuration disables workers.dev and preview
URLs. Wrangler is pinned to `4.158.0`; local `.wrangler` state is ignored.

Deploy the application containing `media-authorize` with the empty default first.
Deploy and verify the Worker using the existing account and bucket configuration:

```sh
cd cloudflare/media
npm ci
npm test
npm run deploy
```

Use the protected operator authentication already configured for that account;
keep credentials and session cookies out of shell output, tracked files and logs.
After checking the route's authorization and range behavior, set
`BEATIT_MEDIA_BASE_URL=/media` in the protected deployment environment and recreate
the correct application project:

```sh
cd /home/ubuntu/services/beatit-multiuser
docker compose -p beatit-multiuser --env-file .env \
  -f compose.yaml -f compose.mysql-tls.yaml up -d --no-build --wait
```

Verify an authenticated full request, seek/range, HEAD, invalid range, an anonymous
request, a different owner's request and a revoked session's subsequent request.
Check the `r2-edge` header and partial byte counts, then exercise playback,
source/stem changes and seeks in the browser. Record both the application image
and Worker version with those observations.

For rollback, restore the empty `BEATIT_MEDIA_BASE_URL` value and recreate the
application. Playback then uses the authenticated relay again. The private bucket
and existing application/Tunnel routes stay in place. Keep the scoped Worker route
until no clients depend on it; any route removal must target only this media route.

## Relay fallback and browser buffering

The relay still hydrates and SHA-verifies a whole object on its first cold read.
Warm scratch-cache reads avoid another R2 download. Its cache now uses a lock per
object and a fast cache-hit check, so a cold stem download does not hold the shared
job/process lock or block an unrelated cached source. Concurrent readers of the
same cold object share one verified transfer. Waveform generation and non-edge
artifact access continue to use this cache; their behavior is separate from the
Worker's streaming path.

The player requests `preload="auto"` and shows an explicit buffering state.
While enabled source audio is waiting/seeking, the matching synthesized-drum clock
is held; playback resumes against the media clock when it starts playing. Preload
is a browser hint, not a guarantee that a whole song is downloaded. Authentication,
range streaming, cache independence and buffer feedback address different parts
of the playback path; none establishes a full-song no-stutter guarantee by itself.

## Measured origin diagnostic (2026-11-09)

The ignored local record `work/issue7/media-delivery-probe-2026-11-09.json` contains
one observation per condition using the existing eight-second verification audio:
1,411,278 bytes of source WAV, 1,411,244 bytes of drum WAV, and a 175,693-byte kick
FLAC. Source size corresponds to about 1.411 Mbit/s, including its header.

Measurements used FastAPI TestClient inside chunsik's production container with
real OCI manifest reads and private R2 objects. Authorization was overridden only
inside that isolated diagnostic process for the fixture's known owner. It created
no auth rows, changed no installed application files and used a fresh temporary
cache per condition; the production cache was left untouched. The candidate was
loaded into an isolated module. This was not a public network or browser test.

| Operation, milliseconds | Previous global cache lock | Candidate per-object lock |
| --- | ---: | ---: |
| Cold first 65,536-byte range | 248.682 | 311.079 |
| Warm first 65,536-byte range | 12.943 | 13.156 |
| Warm seek 65,536-byte range | 11.808 | 11.510 |
| Warm full 1,411,278-byte response | 16.415 | 16.426 |
| Warm lookup during an unrelated cold drums download | 72.060 | 4.578 |
| Concurrent pair total | 102.401 | 120.714 |

Both range cases returned 206 with exact range/length headers; full reads returned
200 with the expected bytes. Cold relay reads fetched and verified the entire
object even for a short range. The candidate reduced this one observation's
unrelated warm lookup wait; its cold read and concurrent-pair total were slower.
These samples do not establish an overall speedup or a bandwidth distribution.

The baseline cold operation fetched/verified about 1.41 MB in 248.7 ms, whereas the
fixture's playback consumes about 176.4 KB/s. That single backend observation does
not establish an R2 throughput shortage. It also excludes public Cloudflare-to-
browser delivery, HTMLMediaElement buffering, long-song working sets, client
network variability and user-visible stutter. It predates Worker deployment. Measure
cold and warm playback/seek behavior through the deployed media route before
attributing a user-visible improvement to it.
