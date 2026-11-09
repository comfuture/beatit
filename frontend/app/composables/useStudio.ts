import {
  defaultOptions,
  type BeatReview,
  type DrumEvent,
  type Job,
  type ScoreMetadata,
  type SystemInfo,
} from "~/types/studio";
import { validateOptions } from "~/utils/rhythm";
import { mergeBeatReview, reviewAfterEdit } from "~/utils/beatReview";
import { connectionUnavailable, runtimeStatus } from "~/utils/execution";
import { sessionIdFromPath, sessionPath } from "~/utils/sessionLocation";
import { actionErrorMessage, isTransientReadError } from "~/utils/connection";
import { discardUpload, uploadMedia } from "~/utils/upload";
import { workspaceEvents } from "~/utils/workspaceEvents";
import { alignmentGrid, canonicalAlignment, resolvedRevision, shiftFirstBar } from "~/utils/alignment";

export function useStudio(userId: string, onUnauthorized: () => void) {
  const storageKey = `beatit-session:${userId}`;
  type LocationMode = "push" | "replace" | "none";
  const sessionLocation = ref("/");
  function commitLocation(path: string, mode: LocationMode) {
    sessionLocation.value = path;
    if (mode !== "none" && window.location.pathname !== path) {
      window.history[mode === "replace" ? "replaceState" : "pushState"](
        window.history.state, "", path,
      );
      window.dispatchEvent(new Event("beatit-location-change"));
    }
  }
  const jobs = ref<Job[]>([]);
  const current = ref<Job | null>(null);
  const pendingRemoteJob = shallowRef<Job | null>(null);
  const system = ref<SystemInfo | null>(null);
  const selectedFile = shallowRef<File | null>(null);
  const options = ref(defaultOptions());
  const revision = ref(defaultOptions());
  const metadata = ref<ScoreMetadata>({ title: "", author: "" });
  const savedMetadata = (job: Job): ScoreMetadata => ({
    ...(job.result?.metadata || {
      title: job.filename.replace(/\.[^.]+$/, ""),
      author: "Automatic drum transcription",
    }),
  });
  const metadataDirty = computed(
    () =>
      !!current.value?.result &&
      JSON.stringify(metadata.value) !== JSON.stringify(savedMetadata(current.value)),
  );
  const events = ref<DrumEvent[]>([]);
  const originalEvents = ref<DrumEvent[]>([]);
  const beatReview = ref<BeatReview>({ grid_key: "", beats: [] });
  const originalReview = ref<BeatReview>({ grid_key: "", beats: [] });
  type EditSnapshot = { kind: "events"; events: DrumEvent[]; review: BeatReview }
    | { kind: "alignment"; offset: number | null };
  const undoStack = ref<EditSnapshot[]>([]);
  const redoStack = ref<EditSnapshot[]>([]);
  const error = ref("");
  const uploading = ref(false);
  const uploadProgress = ref(0);
  const mutating = ref(false);
  const loadingEvents = ref(false);
  const online = ref(false);
  const loading = ref(true);
  const activeId = ref<string | null>(null);
  const busy = computed(
    () =>
      uploading.value ||
      mutating.value ||
      ["queued", "running"].includes(current.value?.status || ""),
  );
  const eventsDirty = computed(
    () => JSON.stringify(events.value) !== JSON.stringify(originalEvents.value),
  );
  const reviewDirty = computed(
    () => JSON.stringify(beatReview.value) !== JSON.stringify(originalReview.value),
  );
  const dirty = computed(
    () =>
      metadataDirty.value ||
      eventsDirty.value ||
      reviewDirty.value ||
      (!!current.value?.result &&
        JSON.stringify(revision.value) !==
          JSON.stringify(resolvedRevision(current.value.result))),
  );
  const result = computed(() => current.value?.result);
  const savedOffset = computed(() => result.value ? resolvedRevision(result.value).offset : null);
  const alignmentDirty = computed(() => !!result.value && revision.value.offset !== savedOffset.value);
  const firstHit = computed(() => events.value.length
    ? events.value.reduce((earliest, event) => Math.min(earliest, event.time), Infinity) : null);
  const toast = useToast();
  const copyEvents = (value: DrumEvent[]) =>
    value.map((event) => ({ ...event }));
  const copyReview = (value: BeatReview): BeatReview => ({
    grid_key: value.grid_key,
    beats: value.beats.map((beat) => ({ ...beat, reasons: [...beat.reasons] })),
  });
  const snapshot = (kind: EditSnapshot["kind"] = "events"): EditSnapshot => kind === "alignment"
    ? { kind, offset: revision.value.offset }
    : { kind, events: copyEvents(events.value), review: copyReview(beatReview.value) };
  let generation = "";
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let selection = 0;
  let displayRevision = 0;
  let bootstrapAttempt = 0;
  let resyncNeeded = false;
  let initialized = false;
  let retrySelection = false;
  let pendingClientEdit: { id: string; draftKey: string } | null = null;
  const draftKey = () => JSON.stringify([revision.value, events.value, metadata.value, beatReview.value]);
  const streamJob = () => current.value || retrySelection ? activeId.value : undefined;
  const pendingRequests = new Set<AbortController>();
  let uploadRequest: AbortController | undefined;
  let uploadSession: { file: File; options: string; id?: string } | undefined;
  const assertActive = () => {
    if (disposed) throw new DOMException("Workspace closed", "AbortError");
  };

  async function api<T>(path: string, init?: RequestInit): Promise<T> {
    assertActive();
    const controller = new AbortController();
    pendingRequests.add(controller);
    const signal = init?.signal
      ? AbortSignal.any([controller.signal, init.signal])
      : controller.signal;
    try {
      const response = await fetch(path, {
        ...init, credentials: "same-origin", cache: "no-store", signal,
      });
      assertActive();
      if (response.status === 401) {
        onUnauthorized();
        throw Object.assign(new DOMException("Session expired", "AbortError"), { status: 401 });
      }
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const detail = body?.detail;
        const error = new Error(
          typeof detail === "string"
            ? detail
            : detail
              ? JSON.stringify(detail)
              : `서버 오류 (${response.status})`,
        );
        throw Object.assign(error, { status: response.status });
      }
      const result = response.status === 204 ? (undefined as T) : await response.json();
      assertActive();
      return result;
    } finally {
      pendingRequests.delete(controller);
    }
  }
  function report(reason: unknown) {
    if (disposed || (reason as { status?: number } | null)?.status === 401) return;
    error.value = actionErrorMessage(reason);
  }
  function fileUrl(name: string, download = false) {
    return `/api/jobs/${current.value?.id}/files/${encodeURIComponent(name)}?v=${encodeURIComponent(result.value?.generation || "")}${download ? "&download=true" : ""}`;
  }
  async function refreshHistory() {
    jobs.value = await api<Job[]>("/api/jobs");
  }
  async function refreshSystem() {
    const available = await api<SystemInfo>("/api/system", {
      signal: AbortSignal.timeout(5000),
    });
    if (disposed) return;
    system.value = available;
  }
  async function showJob(job: Job, token: number) {
    if (token !== selection || disposed) return;
    if (current.value?.id === job.id && current.value.updated > job.updated) return;
    const newGeneration = !!job.result && current.value?.result?.generation !== job.result.generation;
    // An uncertain response can release the editor before publication arrives.
    // Correlation alone must not replace drafts edited after that submission.
    const ownAlignment = newGeneration && !pendingRemoteJob.value && !!pendingClientEdit &&
      job.request?.client_edit_id === pendingClientEdit.id && pendingClientEdit.draftKey === draftKey();
    const keepMetadata = metadataDirty.value;
    const headingOnly = current.value?.id === job.id &&
      current.value.result?.generation === job.result?.metadata_source_generation;
    if (current.value?.id === job.id && current.value.result && job.result &&
        current.value.result.generation !== job.result.generation &&
        (alignmentDirty.value || pendingClientEdit || pendingRemoteJob.value) && !ownAlignment && !headingOnly) {
      pendingRemoteJob.value = job;
      pendingClientEdit = null;
      // Keep the published grid and local draft while observing current job status.
      displayRevision++;
      loadingEvents.value = false;
      current.value = { ...job, result: current.value.result };
      return;
    }
    if (ownAlignment) {
      pendingRemoteJob.value = null;
      error.value = "";
    }
    if (["failed", "cancelled"].includes(job.status)) pendingClientEdit = null;
    const display = ++displayRevision;
    current.value = job;
    loadingEvents.value = false;
    const next = job.result ? `${job.id}:${job.result.generation}` : "";
    if (next && next !== generation) {
      // A heading-only generation preserves the saved rhythm and local edit history.
      if (generation === `${job.id}:${job.result!.metadata_source_generation}`) {
        if (!keepMetadata) metadata.value = savedMetadata(job);
        generation = next;
        return;
      }
      loadingEvents.value = true;
      try {
        const loaded = await api<DrumEvent[]>(
          `/api/jobs/${job.id}/files/events.json?v=${job.result!.generation}`,
        );
        if (token !== selection || display !== displayRevision || disposed) return;
        events.value = loaded;
        originalEvents.value = copyEvents(loaded);
        beatReview.value = mergeBeatReview(loaded, job.result!.grid, job.result!.beat_review);
        originalReview.value = copyReview(beatReview.value);
        revision.value = resolvedRevision(job.result!);
        metadata.value = savedMetadata(job);
        undoStack.value = [];
        redoStack.value = [];
        generation = next;
        if (ownAlignment) pendingClientEdit = null;
      } finally {
        if (token === selection && display === displayRevision) loadingEvents.value = false;
      }
    }
  }
  async function initializeLocation() {
    initialized = true;
    // Do not discard a file the user picked while startup reads were pending.
    if (window.location.pathname === "/") commitLocation("/", "none");
    else await openLocation();
  }
  async function reconcileSelection() {
    const id = activeId.value;
    const token = selection;
    if (!id || disposed) return;
    try {
      await showJob(await api<Job>(`/api/jobs/${id}`), token);
      if (token === selection && !disposed) {
        retrySelection = false;
        loadingEvents.value = false;
        localStorage.setItem(storageKey, id);
      }
    } catch (reason) {
      if (token !== selection || disposed) return;
      if ((reason as { status?: number }).status === 404) {
        newSession("replace");
        error.value = "세션을 찾을 수 없습니다. 삭제되었거나 접근 권한이 없는 주소입니다.";
      } else {
        online.value = false;
        if (!isTransientReadError(reason) && !error.value) report(reason);
        throw reason;
      }
    }
  }
  const stream = workspaceEvents({
    connected: () => { if (!disposed) online.value = !retrySelection; },
    disconnected: () => { online.value = false; resyncNeeded = true; },
    checkSession: () => {
      window.dispatchEvent(new Event("beatit-session-check"));
      if (activeId.value && (current.value || retrySelection)) void reconcileSelection().catch(() => {});
    },
    event: async (type, value) => {
      if (disposed) return;
      if (type === "session-expired") {
        stream.close();
        onUnauthorized();
      } else if (type === "snapshot") {
        if (!Array.isArray(value.jobs) || !value.system || typeof value.system.ffmpeg !== "boolean")
          throw new Error("Invalid workspace snapshot");
        const known = new Map(jobs.value.map((job) => [job.id, job]));
        jobs.value = (value.jobs as Job[]).map((job) => {
          const previous = known.get(job.id);
          return previous && previous.updated > job.updated ? previous : job;
        });
        system.value = value.system;
        const selected = jobs.value.find((job) => job.id === activeId.value);
        if (activeId.value && (retrySelection || (current.value &&
            (resyncNeeded || (selected && selected.updated !== current.value.updated)))))
          await reconcileSelection();
        resyncNeeded = false;
        online.value = !retrySelection;
      } else if (type === "system") {
        system.value = value;
      } else if (type === "job") {
        const job = value.job as Job;
        if (!job || typeof job.id !== "string") throw new Error("Invalid job event");
        const previous = jobs.value.find((item) => item.id === job.id);
        if (previous && previous.updated > job.updated) return;
        jobs.value = [job, ...jobs.value.filter((item) => item.id !== job.id)]
          .sort((a, b) => b.created - a.created).slice(0, 100);
        if (job.id === activeId.value) await showJob(job, selection);
      } else if (type === "job-deleted") {
        jobs.value = jobs.value.filter((job) => job.id !== value.id);
        if (activeId.value === value.id) {
          newSession("replace");
          error.value = "세션이 삭제되었습니다.";
        }
      }
    },
  });
  async function bootstrap() {
    if (disposed) return;
    clearTimeout(timer);
    try {
      await refreshSystem();
      await refreshHistory();
      if (!initialized) await initializeLocation();
      else if (retrySelection) await reconcileSelection();
      if (retrySelection) throw new TypeError("Network unavailable");
      online.value = true;
      bootstrapAttempt = 0;
      if (!document.hidden) stream.start(streamJob());
      if (system.value && !system.value.ffmpeg)
        error.value = "FFmpeg/ffprobe가 없습니다. README의 설치 안내를 확인하세요.";
    } catch (reason) {
      online.value = false;
      if (!isTransientReadError(reason) && !error.value) report(reason);
      if (!disposed) timer = setTimeout(bootstrap, Math.min(1800 * 2 ** bootstrapAttempt++, 30000));
    } finally {
      loading.value = false;
    }
  }
  function onVisibility() {
    if (document.hidden) stream.suspend();
    else if (system.value && initialized) stream.resume(streamJob());
    else void bootstrap();
  }
  async function selectJob(id: string, locationMode: LocationMode = "push") {
    const path = sessionPath(id);
    const token = ++selection;
    retrySelection = false;
    pendingRemoteJob.value = null;
    pendingClientEdit = null;
    activeId.value = id;
    current.value = null;
    generation = "";
    error.value = "";
    events.value = [];
    originalEvents.value = [];
    beatReview.value = { grid_key: "", beats: [] };
    originalReview.value = { grid_key: "", beats: [] };
    undoStack.value = [];
    redoStack.value = [];
    loadingEvents.value = true;
    commitLocation(path, locationMode);
    try {
      await showJob(await api<Job>(`/api/jobs/${id}`), token);
      if (token === selection) localStorage.setItem(storageKey, id);
    } catch (e) {
      if (token === selection) {
        if ((e as { status?: number }).status === 404)
          error.value = "세션을 찾을 수 없습니다. 삭제되었거나 접근 권한이 없는 주소입니다.";
        else if (isTransientReadError(e)) {
          retrySelection = true;
          online.value = false;
        } else report(e);
      }
    } finally {
      if (token === selection) {
        loadingEvents.value = retrySelection;
        if (!loading.value && !disposed && !document.hidden) stream.start(streamJob());
      }
    }
  }
  function newSession(locationMode: LocationMode = "push") {
    selection++;
    retrySelection = false;
    pendingRemoteJob.value = null;
    pendingClientEdit = null;
    activeId.value = null;
    current.value = null;
    generation = "";
    error.value = "";
    events.value = [];
    originalEvents.value = [];
    beatReview.value = { grid_key: "", beats: [] };
    originalReview.value = { grid_key: "", beats: [] };
    undoStack.value = [];
    redoStack.value = [];
    selectedFile.value = null;
    loadingEvents.value = false;
    localStorage.removeItem(storageKey);
    commitLocation("/", locationMode);
    if (!loading.value && !disposed && !document.hidden) stream.start();
  }
  async function openLocation(path = window.location.pathname, mode: LocationMode = "none") {
    const id = sessionIdFromPath(path);
    if (id) await selectJob(id, mode);
    else {
      newSession(mode);
      if (id === undefined) error.value = "올바르지 않은 세션 주소입니다.";
    }
  }
  function chooseFile(file: File) {
    const extensions =
      /\.(wav|mp3|flac|ogg|opus|m4a|aac|aiff?|wma|mp4|mov|mkv|webm|avi|m4v)$/i;
    if (!extensions.test(file.name)) {
      error.value = "지원되는 오디오 또는 비디오 파일을 선택하세요.";
      return;
    }
    if (!file.size) {
      error.value = "빈 파일은 분석할 수 없습니다.";
      return;
    }
    if (system.value && file.size > system.value.max_upload_bytes) {
      error.value = "파일 크기 제한을 초과했습니다.";
      return;
    }
    selectedFile.value = file;
    if (uploadSession && uploadSession.file !== file) {
      if (uploadSession.id) void discardUpload(uploadSession.id).catch(() => {});
      uploadSession = undefined;
    }
    error.value = "";
  }
  async function upload() {
    if (!selectedFile.value || busy.value || !system.value?.ffmpeg) return;
    if (connectionUnavailable(system.value)) {
      error.value = runtimeStatus(system.value);
      return;
    }
    try {
      validateOptions(options.value);
    } catch (e) {
      report(e);
      return;
    }
    uploading.value = true;
    error.value = "";
    uploadProgress.value = 0;
    try {
      const file = selectedFile.value;
      const uploadOptions = { ...options.value, tom_refinement: true };
      const serializedOptions = JSON.stringify(uploadOptions);
      if (uploadSession?.file !== file || uploadSession.options !== serializedOptions) {
        if (uploadSession?.id) void discardUpload(uploadSession.id).catch(() => {});
        uploadSession = { file, options: serializedOptions };
      }
      uploadRequest = new AbortController();
      const job = await uploadMedia(file, uploadOptions, {
        signal: uploadRequest.signal,
        sessionId: uploadSession.id,
        chunkSize: system.value?.upload_chunk_bytes,
        mode: system.value?.upload_mode,
        onSession: (id) => { if (uploadSession) uploadSession.id = id; },
        onProgress: (percent) => { if (!disposed) uploadProgress.value = percent; },
      });
      uploadSession = undefined;
      assertActive();
      await selectJob(job.id);
      assertActive();
      selectedFile.value = null;
      await refreshHistory();
    } catch (e) {
      if ((e as { status?: number })?.status === 401) onUnauthorized();
      else if ((e as Error)?.name !== "AbortError") report(e);
    } finally {
      uploadRequest = undefined;
      uploading.value = false;
    }
  }
  async function cancelUpload() {
    mutating.value = true;
    uploadRequest?.abort();
    const session = uploadSession;
    try {
      if (session?.id) await discardUpload(session.id);
      if (uploadSession === session) uploadSession = undefined;
    } catch (reason) {
      report(reason);
    } finally {
      mutating.value = false;
    }
  }
  async function action(name: "cancel" | "retry" | "delete") {
    const id = current.value?.id;
    if (!id || mutating.value) return;
    const token = selection;
    mutating.value = true;
    error.value = "";
    try {
      const job = await api<Job>(
        `/api/jobs/${id}${name === "delete" ? "" : "/" + name}`,
        { method: name === "delete" ? "DELETE" : "POST" },
      );
      if (name === "delete") {
        if (token === selection) newSession();
      } else await showJob(job, token);
      await refreshHistory();
    } catch (e) {
      report(e);
    } finally {
      mutating.value = false;
    }
  }
  function rememberEdit(kind: EditSnapshot["kind"] = "events") {
    undoStack.value.push(snapshot(kind));
    if (undoStack.value.length > 60) undoStack.value.shift();
    redoStack.value = [];
  }
  function editEvents(next: DrumEvent[]) {
    if (!result.value || busy.value || loadingEvents.value) return;
    rememberEdit();
    beatReview.value = reviewAfterEdit(events.value, next, result.value.grid, beatReview.value);
    events.value = next;
  }
  function resolveBeat(tick: number) {
    if (busy.value || loadingEvents.value) return;
    const beat = beatReview.value.beats.find((item) => item.tick === tick);
    if (!beat || beat.resolved) return;
    rememberEdit();
    beat.resolved = true;
  }
  function shiftAlignment(beats: number) {
    if (!result.value || busy.value || loadingEvents.value) return;
    try {
      const shifted = shiftFirstBar(revision.value, beats);
      canonicalAlignment({ ...revision.value, offset: shifted.offset }, firstHit.value);
      rememberEdit("alignment");
      revision.value.offset = shifted.offset;
    } catch (reason) { report(reason); }
  }
  function resetAlignment() {
    if (!result.value || busy.value || loadingEvents.value || !alignmentDirty.value) return;
    rememberEdit("alignment");
    revision.value.offset = savedOffset.value;
  }
  function restoreEdit(edit: EditSnapshot) {
    if (edit.kind === "alignment") revision.value.offset = edit.offset;
    else { events.value = edit.events; beatReview.value = edit.review; }
  }
  function undo() {
    const previous = undoStack.value.pop();
    if (previous) {
      redoStack.value.push(snapshot(previous.kind));
      restoreEdit(previous);
    }
  }
  function redo() {
    const next = redoStack.value.pop();
    if (next) {
      undoStack.value.push(snapshot(next.kind));
      restoreEdit(next);
    }
  }
  function resetEdits() {
    events.value = copyEvents(originalEvents.value);
    beatReview.value = copyReview(originalReview.value);
    undoStack.value = [];
    redoStack.value = [];
    if (result.value) revision.value = resolvedRevision(result.value);
    if (current.value) metadata.value = savedMetadata(current.value);
  }
  async function applyMetadata() {
    if (!current.value?.result || busy.value || loadingEvents.value || pendingRemoteJob.value || !metadataDirty.value)
      return;
    const token = selection;
    mutating.value = true;
    error.value = "";
    try {
      const job = await api<Job>(`/api/jobs/${current.value.id}/score-metadata`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(metadata.value),
      });
      await showJob(job, token);
      await refreshHistory();
      assertActive();
      toast.add({
        title: "악보 정보 적용을 시작했습니다",
        description: "완료되면 악보 보기와 내보내기 파일이 갱신됩니다.",
        icon: "i-lucide-check",
      });
    } catch (e) {
      report(e);
    } finally {
      mutating.value = false;
    }
  }
  async function save() {
    if (!current.value?.result || busy.value || loadingEvents.value || pendingRemoteJob.value) return;
    if (
      !eventsDirty.value &&
      !reviewDirty.value &&
      JSON.stringify(revision.value) === JSON.stringify(resolvedRevision(result.value!))
    ) {
      await applyMetadata();
      return;
    }
    try {
      validateOptions(revision.value);
    } catch (e) {
      report(e);
      return;
    }
    const token = selection;
    mutating.value = true;
    error.value = "";
    let editId: string | null = null;
    try {
      const preserve = alignmentDirty.value;
      const savedOptions = preserve
        ? { ...revision.value, offset: canonicalAlignment(revision.value, firstHit.value).offset }
        : { ...revision.value, tom_refinement: true };
      let review = beatReview.value;
      if (preserve) {
        try { review = mergeBeatReview(events.value, alignmentGrid(savedOptions, result.value!.duration, events.value)); }
        catch { review = { grid_key: "", beats: [] }; }
      }
      if (preserve) {
        editId = crypto.randomUUID();
        pendingClientEdit = { id: editId, draftKey: draftKey() };
      }
      const job = await api<Job>(`/api/jobs/${current.value.id}/score`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          options: savedOptions,
          events: preserve || eventsDirty.value ? events.value : null,
          metadata: metadata.value,
          beat_review: review,
          ...(preserve ? { preserve_events: true, source_generation: result.value!.generation, client_edit_id: editId } : {}),
        }),
      });
      await showJob(job, token);
      await refreshHistory();
      assertActive();
      toast.add({
        title: "악보 재생성을 시작했습니다",
        description: "편집 내용은 새 악보가 완성되면 저장됩니다.",
        icon: "i-lucide-check",
      });
    } catch (e) {
      if (editId && pendingClientEdit?.id === editId && (e as { status?: number }).status &&
          (e as { status: number }).status < 500) pendingClientEdit = null;
      report(e);
    } finally {
      mutating.value = false;
    }
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (dirty.value || uploading.value) {
      event.preventDefault();
      event.returnValue = "";
    }
  }
  onMounted(() => {
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("visibilitychange", onVisibility);
    void bootstrap();
  });
  onBeforeUnmount(() => {
    disposed = true;
    selection++;
    for (const request of pendingRequests) request.abort();
    pendingRequests.clear();
    localStorage.removeItem(storageKey);
    uploadRequest?.abort();
    stream.close();
    jobs.value = [];
    current.value = null;
    selectedFile.value = null;
    events.value = [];
    originalEvents.value = [];
    beatReview.value = { grid_key: "", beats: [] };
    originalReview.value = { grid_key: "", beats: [] };
    undoStack.value = [];
    redoStack.value = [];
    metadata.value = { title: "", author: "" };
    clearTimeout(timer);
    window.removeEventListener("beforeunload", beforeUnload);
    document.removeEventListener("visibilitychange", onVisibility);
  });
  return {
    jobs,
    current,
    pendingRemoteJob,
    system,
    selectedFile,
    options,
    revision,
    metadata,
    metadataDirty,
    alignmentDirty,
    savedOffset,
    firstHit,
    events,
    beatReview,
    error,
    uploading,
    uploadProgress,
    mutating,
    loadingEvents,
    online,
    loading,
    busy,
    dirty,
    result,
    undoStack,
    redoStack,
    fileUrl,
    selectJob,
    newSession,
    openLocation,
    sessionLocation,
    chooseFile,
    upload,
    cancelUpload,
    action,
    editEvents,
    resolveBeat,
    undo,
    redo,
    resetEdits,
    shiftAlignment,
    resetAlignment,
    save,
    applyMetadata,
  };
}
