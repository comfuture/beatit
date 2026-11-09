import type { Job, Options } from "../types/studio";

export const DEFAULT_CHUNK_BYTES = 8 * 1024 ** 2;
export type UploadSession = {
  id: string;
  offset: number;
  size: number;
  chunk_size: number;
  state: "uploading" | "finalizing" | "completed" | "failed";
  error?: string;
};
type UploadSettings = {
  signal: AbortSignal;
  onProgress: (percent: number) => void;
  onSession: (id: string) => void;
  sessionId?: string;
  chunkSize?: number;
};
const aborted = () => new DOMException("업로드를 취소했습니다.", "AbortError");
const statusOf = (error: unknown) => (error as { status?: number })?.status || 0;
const retryable = (error: unknown) =>
  [0, 408, 409, 425, 429].includes(statusOf(error)) || statusOf(error) >= 500;

function pause(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) return reject(aborted());
    const abort = () => { clearTimeout(timer); reject(aborted()); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, ms);
    signal.addEventListener("abort", abort, { once: true });
  });
}

/** Every request has both a total deadline and a no-progress deadline. */
function request<T>(method: string, path: string, signal: AbortSignal, body?: Document | XMLHttpRequestBodyInit | null,
                    headers: Record<string, string> = {}, progress?: (bytes: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(aborted());
    const xhr = new XMLHttpRequest();
    let idle: ReturnType<typeof setTimeout>;
    const abort = () => xhr.abort();
    const cleanup = () => { clearTimeout(idle); signal.removeEventListener("abort", abort); };
    const fail = (error: Error) => { cleanup(); reject(error); };
    const armIdle = () => {
      clearTimeout(idle);
      idle = setTimeout(() => {
        fail(new Error("파일 전송이 지연되고 있습니다. 분석 시작을 눌러 이어서 전송하세요."));
        xhr.abort();
      }, 30000);
    };
    xhr.open(method, path);
    xhr.timeout = 120000;
    for (const [key, value] of Object.entries(headers)) xhr.setRequestHeader(key, value);
    xhr.upload.onprogress = (event) => { armIdle(); progress?.(event.loaded); };
    xhr.upload.onload = armIdle;
    xhr.onabort = () => fail(aborted());
    xhr.onerror = () => fail(new TypeError("Network upload failed"));
    xhr.ontimeout = () => fail(new Error("파일 전송 시간이 초과되었습니다. 다시 시도하세요."));
    xhr.onload = () => {
      cleanup();
      let data: any;
      try { data = xhr.responseText ? JSON.parse(xhr.responseText) : undefined; } catch { /* HTML proxy error. */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (xhr.status !== 204 && data === undefined) reject(new Error("서버 응답을 읽을 수 없습니다."));
        else resolve(data as T);
      } else {
        const message = typeof data?.detail === "string" ? data.detail
          : xhr.status === 413 ? "업로드 크기 제한을 초과했습니다. 페이지를 새로 고친 뒤 다시 시도하세요."
          : `서버 오류 (${xhr.status})`;
        reject(Object.assign(new Error(message), { status: xhr.status }));
      }
    };
    signal.addEventListener("abort", abort, { once: true });
    armIdle();
    try { xhr.send(body); } catch (error) { cleanup(); reject(error); }
  });
}

async function retry<T>(action: () => Promise<T>, signal: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try { return await action(); } catch (error) {
      if (signal.aborted || attempt === 2 || !retryable(error)) throw error;
      await pause(500 * 2 ** attempt, signal);
    }
  }
}

export async function uploadMedia(file: File, options: Options, settings: UploadSettings): Promise<Job> {
  const { signal, onProgress, onSession } = settings;
  const chunkSize = settings.chunkSize || DEFAULT_CHUNK_BYTES;
  if (file.size <= chunkSize && !settings.sessionId) {
    const form = new FormData();
    form.append("file", file);
    form.append("options", JSON.stringify(options));
    return request<Job>("POST", "/api/jobs", signal, form, {},
      (bytes) => onProgress(Math.min(99, Math.round(bytes / file.size * 100))));
  }
  let session: UploadSession | undefined;
  if (settings.sessionId) {
    try { session = await retry(() => request<UploadSession>("GET", `/api/uploads/${settings.sessionId}`, signal), signal); }
    catch (error) { if (statusOf(error) !== 404) throw error; }
  }
  if (!session) {
    // Creating a new session is deliberately not retried after an unknown outcome.
    session = await request<UploadSession>("POST", "/api/uploads", signal,
      JSON.stringify({ filename: file.name, size: file.size, options }), { "Content-Type": "application/json" });
    onSession(session.id);
  }
  let current: UploadSession = session;
  const path = `/api/uploads/${current.id}`;
  const updateProgress = () => onProgress(Math.floor(current.offset / file.size * 100));
  updateProgress();
  while (current.state === "uploading" && current.offset < file.size) {
    const offset: number = current.offset;
    const chunk: Blob = file.slice(offset, Math.min(offset + current.chunk_size, file.size));
    current = await retry<UploadSession>(async (): Promise<UploadSession> => {
      try {
        return await request<UploadSession>("PATCH", path, signal, chunk,
          { "Content-Type": "application/octet-stream", "Upload-Offset": String(offset) },
          (bytes) => onProgress(Math.min(99, Math.floor((offset + bytes) / file.size * 100))));
      } catch (error) {
        if (signal.aborted || !retryable(error)) throw error;
        const status = await request<UploadSession>("GET", path, signal);
        if (status.offset >= offset + chunk.size) return status;
        throw error;
      }
    }, signal);
    updateProgress();
  }
  if (current.state !== "completed" && current.state !== "finalizing")
    current = await retry(() => request<UploadSession>("POST", `${path}/complete`, signal), signal);
  const deadline = Date.now() + 15 * 60 * 1000;
  while (current.state !== "completed") {
    if (current.state === "failed") throw new Error(current.error || "파일 저장에 실패했습니다. 다시 시도하세요.");
    if (Date.now() >= deadline) throw new Error("파일 저장이 지연되고 있습니다. 분석 시작을 눌러 상태를 다시 확인하세요.");
    await pause(1000, signal);
    current = await retry(() => request<UploadSession>("GET", path, signal), signal);
  }
  return retry(() => request<Job>("GET", `/api/jobs/${current.id}`, signal), signal);
}

export function discardUpload(id: string) {
  const signal = AbortSignal.timeout(10000);
  return retry(() => request<void>("DELETE", `/api/uploads/${id}`, signal), signal);
}
