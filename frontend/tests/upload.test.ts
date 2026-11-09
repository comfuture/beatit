import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { defaultOptions } from "../app/types/studio";
import { uploadMedia, type UploadSession } from "../app/utils/upload";

type Sent = { method: string; path: string; headers: Record<string, string>; body: any; timeout: number };
type Reply = { status?: number; json?: unknown; text?: string } | undefined;
let sent: Sent[];
let respond: (request: Sent) => Reply;

class MockXHR {
  upload: { onprogress?: (event: { loaded: number }) => void; onload?: () => void } = {};
  onload?: () => void;
  onabort?: () => void;
  onerror?: () => void;
  ontimeout?: () => void;
  status = 0;
  timeout = 0;
  responseText = "";
  headers: Record<string, string> = {};
  method = "";
  path = "";
  open(method: string, path: string) { this.method = method; this.path = path; }
  setRequestHeader(key: string, value: string) { this.headers[key] = value; }
  abort() { this.onabort?.(); }
  send(body: any) {
    const request = { method: this.method, path: this.path, headers: this.headers, body, timeout: this.timeout };
    sent.push(request);
    queueMicrotask(() => {
      const reply = respond(request);
      if (!reply) return;
      this.status = reply.status || 200;
      this.responseText = reply.text || JSON.stringify(reply.json);
      this.upload.onprogress?.({ loaded: body instanceof Blob ? body.size : 0 });
      this.onload?.();
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  sent = [];
  vi.stubGlobal("XMLHttpRequest", MockXHR);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

function server({ loseFirstAck = false, failMiddle = false } = {}) {
  const session: UploadSession = { id: "a".repeat(32), offset: 0, size: 19, chunk_size: 8, state: "uploading" };
  const received: Blob[] = [];
  const job = { id: session.id, status: "queued" };
  respond = (request) => {
    if (request.path === "/api/uploads") return { status: 201, json: session };
    if (request.path.startsWith("/api/jobs/")) return { json: job };
    if (request.path.endsWith("/complete")) {
      session.state = "finalizing";
      return { status: 202, json: session };
    }
    if (request.method === "GET") {
      if (session.state === "finalizing") session.state = "completed";
      return { json: session };
    }
    if (request.method === "PATCH") {
      const offset = Number(request.headers["Upload-Offset"]);
      if (failMiddle && offset === 8) return { status: 503, json: { detail: "temporary failure" } };
      expect(offset).toBe(session.offset);
      expect(request.body.size).toBeLessThanOrEqual(8);
      received.push(request.body);
      session.offset += request.body.size;
      if (loseFirstAck && offset === 0) return { status: 503, text: "upstream disconnected" };
      return { json: session };
    }
    if (request.method === "PUT") {
      expect(request.body.size).toBe(session.size);
      received.push(request.body);
      session.offset = session.size;
      if (loseFirstAck) return { status: 503, text: "upstream disconnected" };
      return { json: session };
    }
    throw new Error(`Unexpected request ${request.method} ${request.path}`);
  };
  return { session, received, job, resume: () => { failMiddle = false; } };
}

describe("bounded, retryable media uploads", () => {
  it.each([false, true])("reconciles a direct 413 in the same session (body already accepted: %s)", async (accepted) => {
    const backend = server();
    const original = respond;
    respond = (request) => {
      if (request.method !== "PUT") return original(request);
      if (accepted) original(request);
      return { status: 413, text: "<html>Request Entity Too Large</html>" };
    };
    const onSession = vi.fn();
    const upload = uploadMedia(new File(["12345678abcdefghEND"], "song.mp4"), defaultOptions(), {
      mode: "direct", signal: new AbortController().signal, onSession, onProgress: vi.fn(),
    });
    await vi.runAllTimersAsync();
    expect(await upload).toEqual(backend.job);
    expect(await new Blob(backend.received).text()).toBe("12345678abcdefghEND");
    expect(sent.filter((request) => request.method === "PUT")).toHaveLength(1);
    expect(sent.filter((request) => request.method === "PATCH")).toHaveLength(accepted ? 0 : 3);
    expect(sent.filter((request) => request.path === "/api/uploads")).toHaveLength(1);
    expect(sent.filter((request) => request.path.endsWith("/complete"))).toHaveLength(1);
    expect(onSession).toHaveBeenCalledOnce();
  });
  it("keeps session-creation size rejection authoritative in direct mode", async () => {
    respond = () => ({ status: 413, json: { detail: "File exceeds application size limit" } });
    await expect(uploadMedia(new File(["audio"], "song.wav"), defaultOptions(), {
      mode: "direct", signal: new AbortController().signal, onSession: vi.fn(), onProgress: vi.fn(),
    })).rejects.toMatchObject({ status: 413 });
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ method: "POST", path: "/api/uploads" });
  });
  it("sends direct mode as one whole-file request and reconciles a lost response", async () => {
    const backend = server({ loseFirstAck: true });
    const upload = uploadMedia(new File(["12345678abcdefghEND"], "song.mp4"), defaultOptions(), {
      mode: "direct", signal: new AbortController().signal, onSession: vi.fn(), onProgress: vi.fn(),
    });
    await vi.runAllTimersAsync();
    expect(await upload).toEqual(backend.job);
    expect(sent.filter((request) => request.method === "PUT")).toHaveLength(1);
    expect(sent.filter((request) => request.method === "PATCH")).toHaveLength(0);
    expect(sent.find((request) => request.method === "PUT")!.timeout).toBe(30 * 60 * 1000);
    expect(await new Blob(backend.received).text()).toBe("12345678abcdefghEND");
  });
  it("sends bounded chunks, recovers a lost acknowledgement and publishes once", async () => {
    const backend = server({ loseFirstAck: true });
    const progress: number[] = [];
    const onSession = vi.fn();
    const upload = uploadMedia(new File(["12345678abcdefghEND"], "song.mp4"), defaultOptions(), {
      signal: new AbortController().signal, chunkSize: 8, onSession, onProgress: (n) => progress.push(n),
    });
    await vi.runAllTimersAsync();
    expect(await upload).toEqual(backend.job);
    expect(await new Blob(backend.received).text()).toBe("12345678abcdefghEND");
    expect(sent.filter((r) => r.method === "PATCH").map((r) => r.headers["Upload-Offset"])).toEqual(["0", "8", "16"]);
    expect(sent.filter((r) => r.path.endsWith("/complete"))).toHaveLength(1);
    expect(sent.every((r) => r.timeout === 120000)).toBe(true);
    expect(onSession).toHaveBeenCalledOnce();
    expect(progress).toContain(100);
  });

  it("resumes from the server offset after bounded retries are exhausted", async () => {
    const backend = server({ failMiddle: true });
    const file = new File(["12345678abcdefghEND"], "song.mp4");
    const settings = { signal: new AbortController().signal, chunkSize: 8, onSession: vi.fn(), onProgress: vi.fn() };
    const failed = uploadMedia(file, defaultOptions(), settings);
    const failure = expect(failed).rejects.toMatchObject({ status: 503 });
    await vi.runAllTimersAsync();
    await failure;
    expect(backend.session.offset).toBe(8);
    backend.resume();
    const resumed = uploadMedia(file, defaultOptions(), { ...settings, sessionId: backend.session.id });
    await vi.runAllTimersAsync();
    expect(await resumed).toEqual(backend.job);
    expect(await new Blob(backend.received).text()).toBe("12345678abcdefghEND");
    expect(sent.filter((r) => r.path === "/api/uploads")).toHaveLength(1);
  });

  it("ends a stalled small upload and supports immediate cancellation", async () => {
    respond = () => undefined;
    const settings = { signal: new AbortController().signal, onSession: vi.fn(), onProgress: vi.fn() };
    const stalled = uploadMedia(new File(["audio"], "song.wav"), defaultOptions(), settings);
    const timeout = expect(stalled).rejects.toThrow("파일 전송이 지연");
    await vi.advanceTimersByTimeAsync(30000);
    await timeout;
    const controller = new AbortController();
    const cancelled = uploadMedia(new File(["audio"], "song.wav"), defaultOptions(), { ...settings, signal: controller.signal });
    const cancellation = expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await cancellation;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("reports an HTML size-limit response as an actionable upload error", async () => {
    respond = () => ({ status: 413, text: "<html>Request Entity Too Large</html>" });
    await expect(uploadMedia(new File(["audio"], "song.wav"), defaultOptions(), {
      signal: new AbortController().signal, onSession: vi.fn(), onProgress: vi.fn(),
    })).rejects.toMatchObject({ status: 413, message: expect.stringContaining("업로드 크기 제한") });
  });
});
