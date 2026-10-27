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
import { colabConnectionUnavailable, runtimeStatus } from "~/utils/execution";

export function useStudio() {
  const jobs = ref<Job[]>([]);
  const current = ref<Job | null>(null);
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
  type EditSnapshot = { events: DrumEvent[]; review: BeatReview };
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
          JSON.stringify(current.value.result.options)),
  );
  const result = computed(() => current.value?.result);
  const toast = useToast();
  const copyEvents = (value: DrumEvent[]) =>
    value.map((event) => ({ ...event }));
  const copyReview = (value: BeatReview): BeatReview => ({
    grid_key: value.grid_key,
    beats: value.beats.map((beat) => ({ ...beat, reasons: [...beat.reasons] })),
  });
  const snapshot = (): EditSnapshot => ({
    events: copyEvents(events.value), review: copyReview(beatReview.value),
  });
  let generation = "";
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  let selection = 0;
  let systemRefreshedAt = 0;

  async function api<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(path, init);
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const detail = body?.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail
            ? JSON.stringify(detail)
            : `서버 오류 (${response.status})`,
      );
    }
    return response.status === 204 ? (undefined as T) : response.json();
  }
  function report(reason: unknown) {
    error.value = reason instanceof Error ? reason.message : String(reason);
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
    systemRefreshedAt = Date.now();
  }
  async function showJob(job: Job, token: number) {
    if (token !== selection || disposed) return;
    current.value = job;
    const next = job.result ? `${job.id}:${job.result.generation}` : "";
    if (next && next !== generation) {
      // A heading-only generation preserves the saved rhythm and local edit history.
      if (generation === `${job.id}:${job.result!.metadata_source_generation}`) {
        metadata.value = savedMetadata(job);
        generation = next;
        return;
      }
      loadingEvents.value = true;
      try {
        const loaded = await api<DrumEvent[]>(
          `/api/jobs/${job.id}/files/events.json?v=${job.result!.generation}`,
        );
        if (token !== selection || disposed) return;
        events.value = loaded;
        originalEvents.value = copyEvents(loaded);
        beatReview.value = mergeBeatReview(loaded, job.result!.grid, job.result!.beat_review);
        originalReview.value = copyReview(beatReview.value);
        revision.value = { ...job.result!.options };
        metadata.value = savedMetadata(job);
        undoStack.value = [];
        redoStack.value = [];
        generation = next;
      } finally {
        if (token === selection) loadingEvents.value = false;
      }
    }
  }
  async function poll() {
    if (disposed) return;
    const token = selection;
    const id = activeId.value;
    try {
      if (!system.value || Date.now() - systemRefreshedAt >= 10000)
        await refreshSystem();
      await refreshHistory();
      if (
        id &&
        current.value &&
        (["running", "queued"].includes(current.value.status) ||
          (current.value.result &&
            generation !== `${id}:${current.value.result.generation}`))
      ) {
        await showJob(await api<Job>(`/api/jobs/${id}`), token);
      }
      online.value = true;
    } catch (e) {
      online.value = false;
      report(e);
    }
    if (!disposed) timer = setTimeout(poll, 1800);
  }
  async function selectJob(id: string) {
    const token = ++selection;
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
    try {
      await showJob(await api<Job>(`/api/jobs/${id}`), token);
      if (token === selection) localStorage.setItem("beatit-session", id);
    } catch (e) {
      if (token === selection) report(e);
    } finally {
      if (token === selection) loadingEvents.value = false;
    }
  }
  function newSession() {
    selection++;
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
    localStorage.removeItem("beatit-session");
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
    error.value = "";
  }
  async function upload() {
    if (!selectedFile.value || busy.value || !system.value?.ffmpeg) return;
    if (colabConnectionUnavailable(system.value)) {
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
      const form = new FormData();
      form.append("file", selectedFile.value);
      form.append("options", JSON.stringify({ ...options.value, tom_refinement: true }));
      const job = await new Promise<Job>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/jobs");
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable)
            uploadProgress.value = Math.round((e.loaded / e.total) * 100);
        };
        xhr.onerror = () =>
          reject(
            new Error("파일 전송에 실패했습니다. 서버 연결을 확인하세요."),
          );
        xhr.onload = () => {
          try {
            const data = JSON.parse(xhr.responseText);
            if (xhr.status >= 200 && xhr.status < 300) resolve(data);
            else
              reject(
                new Error(
                  typeof data.detail === "string"
                    ? data.detail
                    : JSON.stringify(data.detail),
                ),
              );
          } catch {
            reject(new Error("서버 응답을 읽을 수 없습니다."));
          }
        };
        xhr.send(form);
      });
      await selectJob(job.id);
      selectedFile.value = null;
      await refreshHistory();
    } catch (e) {
      report(e);
    } finally {
      uploading.value = false;
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
  function rememberEdit() {
    undoStack.value.push(snapshot());
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
  function undo() {
    const previous = undoStack.value.pop();
    if (previous) {
      redoStack.value.push(snapshot());
      events.value = previous.events;
      beatReview.value = previous.review;
    }
  }
  function redo() {
    const next = redoStack.value.pop();
    if (next) {
      undoStack.value.push(snapshot());
      events.value = next.events;
      beatReview.value = next.review;
    }
  }
  function resetEdits() {
    events.value = copyEvents(originalEvents.value);
    beatReview.value = copyReview(originalReview.value);
    undoStack.value = [];
    redoStack.value = [];
    if (result.value) revision.value = { ...result.value.options };
    if (current.value) metadata.value = savedMetadata(current.value);
  }
  async function applyMetadata() {
    if (!current.value?.result || busy.value || loadingEvents.value || !metadataDirty.value)
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
    if (!current.value?.result || busy.value || loadingEvents.value) return;
    if (
      !eventsDirty.value &&
      !reviewDirty.value &&
      JSON.stringify(revision.value) === JSON.stringify(result.value!.options)
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
    try {
      const job = await api<Job>(`/api/jobs/${current.value.id}/score`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          options: { ...revision.value, tom_refinement: true },
          events: eventsDirty.value ? events.value : null,
          metadata: metadata.value,
          beat_review: beatReview.value,
        }),
      });
      await showJob(job, token);
      await refreshHistory();
      toast.add({
        title: "악보 재생성을 시작했습니다",
        description: "편집 내용은 새 악보가 완성되면 저장됩니다.",
        icon: "i-lucide-check",
      });
    } catch (e) {
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
  onMounted(async () => {
    window.addEventListener("beforeunload", beforeUnload);
    try {
      await refreshSystem();
      online.value = true;
      await refreshHistory();
      const saved = localStorage.getItem("beatit-session");
      if (saved && jobs.value.some((j) => j.id === saved))
        await selectJob(saved);
      if (system.value && !system.value.ffmpeg)
        error.value =
          "FFmpeg/ffprobe가 없습니다. README의 설치 안내를 확인하세요.";
    } catch (e) {
      report(e);
    } finally {
      loading.value = false;
      timer = setTimeout(poll, 1800);
    }
  });
  onBeforeUnmount(() => {
    disposed = true;
    selection++;
    clearTimeout(timer);
    window.removeEventListener("beforeunload", beforeUnload);
  });
  return {
    jobs,
    current,
    system,
    selectedFile,
    options,
    revision,
    metadata,
    metadataDirty,
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
    chooseFile,
    upload,
    action,
    editEvents,
    resolveBeat,
    undo,
    redo,
    resetEdits,
    save,
    applyMetadata,
  };
}
