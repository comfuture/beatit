import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { workspaceEvents } from "../app/utils/workspaceEvents";

class MockEventSource extends EventTarget {
  static instances: MockEventSource[] = [];
  onopen?: () => void;
  onerror?: () => void;
  closed = false;
  constructor(public url: string) { super(); MockEventSource.instances.push(this); }
  close() { this.closed = true; }
  emit(type: string, value: unknown) { this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(value) })); }
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0);
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function setup() {
  const handlers = { event: vi.fn(), connected: vi.fn(), disconnected: vi.fn(), checkSession: vi.fn() };
  return { handlers, stream: workspaceEvents(handlers) };
}
const settled = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };

describe("workspace push connection lifecycle", () => {
  it("delivers a reconnect snapshot while the old handler is pending and ignores its late rejection", async () => {
    const { stream, handlers } = setup();
    let reject!: (reason: Error) => void;
    const blocked = new Promise<void>((_, fail) => { reject = fail; });
    handlers.event.mockImplementation((type) => type === "job" ? blocked : undefined);
    stream.start();
    const previous = MockEventSource.instances[0]!;
    previous.emit("job", { job: { id: "pending-artifact" } });
    await settled();
    previous.onerror?.();
    await vi.advanceTimersByTimeAsync(1800);
    const active = MockEventSource.instances[1]!;
    active.emit("snapshot", { jobs: [], system: {} });
    await settled();
    expect(handlers.event).toHaveBeenLastCalledWith("snapshot", { jobs: [], system: {} });
    reject(new Error("old artifact failed"));
    await settled();
    expect(active.closed).toBe(false);
    expect(handlers.disconnected).toHaveBeenCalledOnce();
    stream.close();
  });
  it("expires immediately even when a previous job event is blocked on an artifact", async () => {
    const { stream, handlers } = setup();
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => { release = resolve; });
    handlers.event.mockImplementation((type) => type === "job" ? blocked : undefined);
    stream.start();
    const source = MockEventSource.instances[0]!;
    source.emit("job", { job: { id: "pending-artifact" } });
    await settled();
    source.emit("session-expired", {});
    expect(handlers.event).toHaveBeenLastCalledWith("session-expired", {});
    expect(source.closed).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    release();
    await settled();
  });
  it("keeps one connection, pins selection and ignores delayed events from a closed stream", async () => {
    const { stream, handlers } = setup();
    stream.start(); stream.start();
    expect(MockEventSource.instances).toHaveLength(1);
    const previous = MockEventSource.instances[0]!;
    stream.start("a".repeat(32));
    expect(previous.closed).toBe(true);
    expect(MockEventSource.instances[1]!.url).toBe(`/api/events?job_id=${"a".repeat(32)}`);
    previous.emit("job", { job: { id: "previous-account" } });
    previous.emit("resync", {});
    MockEventSource.instances[1]!.emit("job", { job: { id: "current" } });
    await settled();
    expect(handlers.event).toHaveBeenCalledExactlyOnceWith("job", { job: { id: "current" } });
    stream.close();
    expect(MockEventSource.instances.every((source) => source.closed)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("backs off failed connections and checks authentication only once per outage", async () => {
    const { stream, handlers } = setup();
    stream.start();
    MockEventSource.instances[0]!.onerror?.();
    expect(handlers.checkSession).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1800);
    MockEventSource.instances[1]!.onerror?.();
    await vi.advanceTimersByTimeAsync(1800);
    expect(MockEventSource.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1800);
    expect(MockEventSource.instances).toHaveLength(3);
    expect(handlers.checkSession).toHaveBeenCalledOnce();
    MockEventSource.instances[2]!.emit("snapshot", { jobs: [], system: {} });
    await settled();
    MockEventSource.instances[2]!.onerror?.();
    expect(handlers.checkSession).toHaveBeenCalledTimes(2);
    stream.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("resynchronizes overflow and releases hidden/disposed connections and timers", async () => {
    const { stream, handlers } = setup();
    stream.start();
    MockEventSource.instances[0]!.emit("resync", {});
    expect(handlers.disconnected).toHaveBeenCalledOnce();
    expect(MockEventSource.instances).toHaveLength(2);
    stream.suspend();
    await vi.advanceTimersByTimeAsync(60000);
    expect(MockEventSource.instances).toHaveLength(2);
    stream.resume();
    expect(MockEventSource.instances).toHaveLength(3);
    MockEventSource.instances[2]!.onerror?.();
    stream.close();
    await vi.advanceTimersByTimeAsync(60000);
    expect(MockEventSource.instances).toHaveLength(3);
  });
});
