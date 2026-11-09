// R2 -> Worker -> browser; only authorization metadata crosses the OCI tunnel.
const noStore = {
  "Cache-Control": "private, no-store", "Vary": "Cookie",
  "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "same-origin",
};
function failure(status, message, headers = {}) {
  return new Response(message, { status, headers: { ...noStore, ...headers } });
}
export function byteRange(value, size) {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) throw new RangeError();
  const first = match[1] ? Number(match[1]) : null;
  const last = match[2] ? Number(match[2]) : null;
  if ((first !== null && !Number.isSafeInteger(first)) ||
      (last !== null && !Number.isSafeInteger(last))) throw new RangeError();
  const offset = first === null ? Math.max(0, size - last) : first;
  const end = first === null ? size - 1 : Math.min(last ?? size - 1, size - 1);
  if (offset >= size || end < offset) throw new RangeError();
  return { offset, length: end - offset + 1 };
}
export async function serveMedia(request, env, fetchOrigin = fetch) {
  const url = new URL(request.url);
  if (url.origin !== env.APP_ORIGIN) return failure(404, "Not found");
  if (!['GET', 'HEAD'].includes(request.method))
    return failure(405, "Method not allowed", { Allow: "GET, HEAD" });
  const match = /^\/media\/([0-9a-f]{32})\/(audio\.wav|drums\.wav|stem-(kick|snare|toms|hh|cymbals)\.flac)$/.exec(url.pathname);
  if (!match || url.search) return failure(404, "Not found");
  if (request.headers.get("Sec-Fetch-Site") === "cross-site")
    return failure(403, "Cross-origin media requests are disabled");
  const cookie = request.headers.get("Cookie");
  if (!cookie) return failure(401, "Google sign-in required");
  try {
    const authorization = new URL(`/api/jobs/${match[1]}/media-authorize`, env.APP_ORIGIN);
    authorization.searchParams.set("source", match[2]);
    // Every full/range/HEAD request rechecks the revocable DB session and owner.
    // There is no permission cache, public bucket, or transferable signed URL.
    const response = await fetchOrigin(authorization, {
      method: "GET", headers: { Cookie: cookie, Accept: "application/json" },
      redirect: "manual", signal: AbortSignal.timeout(10000),
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    if (!response.ok) {
      const status = [401, 403, 404].includes(response.status) ? response.status : 503;
      return failure(status, status === 503 ? "Media authorization unavailable" : "Media unavailable");
    }
    const manifest = await response.json();
    if (!manifest.object_key || !Number.isSafeInteger(manifest.size) || manifest.size <= 0 ||
        !/^[0-9a-f]{64}$/.test(manifest.sha256) ||
        !['audio/wav', 'audio/flac'].includes(manifest.content_type))
      return failure(503, "Invalid media manifest");
    const etag = `"${manifest.sha256}"`;
    const ifRange = request.headers.get("If-Range");
    let range;
    try {
      range = byteRange(!ifRange || ifRange === etag ? request.headers.get("Range") : null, manifest.size);
    } catch {
      return failure(416, "Range not satisfiable", { "Content-Range": `bytes */${manifest.size}` });
    }
    const object = request.method === 'HEAD'
      ? await env.MEDIA.head(manifest.object_key)
      : await env.MEDIA.get(manifest.object_key, range ? { range } : undefined);
    if (!object) return failure(404, "Media unavailable");
    if (object.size !== manifest.size) return failure(503, "Media size does not match manifest");
    const headers = new Headers({
      ...noStore, "Content-Type": manifest.content_type, "Accept-Ranges": "bytes",
      "Content-Length": String(range?.length ?? manifest.size), ETag: etag,
      "X-Beatit-Media": "r2-edge",
    });
    if (range) headers.set("Content-Range", `bytes ${range.offset}-${range.offset + range.length - 1}/${manifest.size}`);
    return new Response(request.method === 'HEAD' ? null : object.body, { status: range ? 206 : 200, headers });
  } catch {
    return failure(503, "Media temporarily unavailable", { "Retry-After": "5" });
  }
}
export default { fetch(request, env) { return serveMedia(request, env); } };
