/** One reconnecting, owner-authenticated event stream per workspace. */
export type WorkspaceEventType = "snapshot" | "job" | "system" | "job-deleted" | "session-expired";
export type WorkspaceEventHandlers = {
  event: (type: WorkspaceEventType, value: any) => void | Promise<void>;
  connected: () => void;
  disconnected: () => void;
  checkSession: () => void;
};

export function workspaceEvents(handlers: WorkspaceEventHandlers) {
  let source: EventSource | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  let suspended = false;
  let attempt = 0;
  let checkedSession = false;
  let path = "/api/events";
  let delivery = Promise.resolve();

  function closeSource() {
    clearTimeout(timer);
    source?.close();
    source = undefined;
    // An old artifact handler may still be pending after its connection closes.
    // New snapshots must have their own delivery queue.
    delivery = Promise.resolve();
  }
  function reconnect() {
    closeSource();
    if (stopped || suspended) return;
    handlers.disconnected();
    if (!checkedSession) {
      checkedSession = true;
      handlers.checkSession();
    }
    const delay = Math.min(1800 * 2 ** attempt++, 30000);
    timer = setTimeout(connect, delay + Math.floor(Math.random() * delay * 0.1));
  }
  function connect() {
    if (stopped || suspended || source) return;
    const active = new EventSource(path);
    source = active;
    timer = setTimeout(() => { if (source === active) reconnect(); }, 15000);
    active.onopen = () => {
      if (source !== active || stopped) return;
      clearTimeout(timer);
      handlers.connected();
    };
    for (const type of ["snapshot", "job", "system", "job-deleted", "session-expired"] as const) {
      active.addEventListener(type, (event) => {
        if (source !== active || stopped) return;
        let value: unknown;
        try { value = JSON.parse((event as MessageEvent).data); }
        catch { reconnect(); return; }
        // Revocation cannot wait behind an artifact read started by a job event.
        if (type === "session-expired") {
          stopped = true;
          closeSource();
          void Promise.resolve(handlers.event(type, value)).catch(() => {});
          return;
        }
        delivery = delivery.then(async () => {
          if (source !== active || stopped) return;
          await handlers.event(type, value);
          if (source !== active || stopped) return;
          if (type === "snapshot") { clearTimeout(timer); attempt = 0; checkedSession = false; }
        }).catch(() => { if (source === active) reconnect(); });
      });
    }
    active.addEventListener("resync", () => {
      if (source !== active || stopped) return;
      closeSource();
      if (!stopped && !suspended) { handlers.disconnected(); connect(); }
    });
    active.onerror = () => { if (source === active) reconnect(); };
  }
  return {
    start(jobId?: string | null) {
      const next = jobId ? `/api/events?job_id=${encodeURIComponent(jobId)}` : "/api/events";
      if (next !== path) { path = next; closeSource(); }
      connect();
    },
    suspend() { suspended = true; closeSource(); },
    resume(jobId?: string | null) { suspended = false; this.start(jobId); },
    close() { stopped = true; closeSource(); },
  };
}
