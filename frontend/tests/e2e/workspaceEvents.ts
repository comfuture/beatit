import type { Page } from "@playwright/test";

/** Controlled EventSource transport; application mutations still use real HTTP mocks. */
export async function mockWorkspaceEvents(page: Page, snapshot: () => unknown) {
  await page.exposeBinding("__workspaceSnapshot", () => snapshot());
  await page.addInitScript(() => {
    const sources = new Set<MockEventSource>();
    class MockEventSource extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;
      CONNECTING = 0;
      OPEN = 1;
      CLOSED = 2;
      readyState = 0;
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      constructor(public url: string) {
        super();
        sources.add(this);
        void (window as any).__workspaceSnapshot().then((value: unknown) => {
          if (this.readyState === 2) return;
          this.readyState = 1;
          this.onopen?.(new Event("open"));
          if (value) this.emit("snapshot", value);
        });
      }
      emit(type: string, value: unknown) {
        if (this.readyState !== 1) return;
        this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(value) }));
      }
      close() { this.readyState = 2; }
    }
    (window as any).EventSource = MockEventSource;
    (window as any).__workspaceEmit = (type: string, value: unknown) => {
      for (const source of sources) source.emit(type, value);
    };
    (window as any).__workspaceFail = () => {
      for (const source of sources) if (source.readyState === 1) source.onerror?.(new Event("error"));
    };
    (window as any).__workspaceConnections = () => [...sources].filter((source) => source.readyState === 1).length;
  });
  const emit = (type: string, value: unknown) => page.evaluate(
    ({ type, value }) => (window as any).__workspaceEmit(type, value), { type, value },
  ).catch((error) => {
    if (!page.isClosed() && !/Execution context was destroyed|Cannot find context/.test(String(error))) throw error;
  });
  return {
    emit,
    snapshot: () => emit("snapshot", snapshot()),
    fail: () => page.evaluate(() => (window as any).__workspaceFail()),
    connections: () => page.evaluate(() => (window as any).__workspaceConnections()),
  };
}
