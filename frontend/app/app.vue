<script setup lang="ts">
import type { SplitterItem } from "@nuxt/ui";
import { statusLabels } from "~/types/studio";
import { colabConnectionUnavailable, isColabExecution, runtimeStatus } from "~/utils/execution";

const studio = useStudio();
const {
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
} = studio;
const colabExecution = computed(() => isColabExecution(system.value));
const runtimeDescription = computed(() => runtimeStatus(system.value));
const runtimeConnected = computed(() => {
  if (!colabExecution.value) return true;
  const runtime = system.value?.remote_runtime;
  return !colabConnectionUnavailable(system.value) && !!runtime?.prepared &&
    ["ready", "running", "idle"].includes(runtime.state);
});
const sidebarOpen = ref(true);
const inspectorOpen = ref(false);
const sessionBrowserOpen = ref(false);
const selectedJobId = ref<string | null>(null);
const sessionInfoPopoverContent = {
  side: "right" as const,
  align: "start" as const,
  collisionPadding: 12,
  "aria-label": "세션 정보",
};
const search = ref("");
const view = ref("edit");
const position = ref(0);
const playbackEnabled = ref(true);
const timeline = useTemplateRef("timeline");
const beatEditor = useTemplateRef("beatEditor");
const discardOpen = ref(false);
const deleteOpen = ref(false);
const helpOpen = ref(false);
let pendingAction: (() => void) | null = null;
const compact = ref(false);
const timelineMinimumHeight = ref(260);
const editorMinimumHeight = 240;
const filteredJobs = computed(() =>
  jobs.value.filter((j) =>
    j.filename.toLowerCase().includes(search.value.toLowerCase()),
  ),
);
const tabs = [
  { label: "비트 편집", icon: "i-lucide-grid-2x2", value: "edit" },
  { label: "악보 보기", icon: "i-lucide-file-music", value: "score" },
];
const mainPanels: SplitterItem[] = [
  { slot: "workspace", defaultSize: 77, minSize: 50, class: "overflow-y-auto" },
  { slot: "inspector", defaultSize: 23, minSize: 19, maxSize: 38 },
];
const editorPanels = computed<SplitterItem[]>(() => [
  { slot: "timeline", sizeUnit: "px", defaultSize: 300, minSize: timelineMinimumHeight.value },
  { slot: "editor", sizeUnit: "px", minSize: editorMinimumHeight },
]);
function downloadLink(filename: string) {
  // Bypass Nuxt routing so downloads never change the workspace URL/history.
  return {
    href: studio.fileUrl(filename, true),
    external: true,
    download: filename,
  };
}
const exportItems = computed(() => [
  [
    {
      label: "전체 파일 (.zip)",
      icon: "i-lucide-package",
      ...downloadLink("score-bundle.zip"),
    },
  ],
  [
    {
      label: "MusicXML",
      icon: "i-lucide-file-music",
      ...downloadLink("score.musicxml"),
    },
    {
      label: "MIDI · 악보 격자",
      icon: "i-lucide-piano",
      ...downloadLink("score.mid"),
    },
    {
      label: "MIDI · 원시 타격",
      icon: "i-lucide-piano",
      ...downloadLink("performance.mid"),
    },
    ...(result.value?.pdf
      ? [
          {
            label: "PDF 악보",
            icon: "i-lucide-file-text",
            ...downloadLink("score.pdf"),
          },
        ]
      : []),
    {
      label: "타격 데이터 (.json)",
      icon: "i-lucide-braces",
      ...downloadLink("events.json"),
    },
    {
      label: "분석 데이터 (.json)",
      icon: "i-lucide-file-json",
      ...downloadLink("analysis.json"),
    },
    {
      label: "SVG 페이지",
      icon: "i-lucide-file-image",
      children: (result.value?.pages || []).map((page, index) => ({
        label: `${index + 1}페이지`,
        ...downloadLink(page),
      })),
    },
    ...(result.value?.tom_refinement?.enabled
      ? [
          {
            label: "탐 보정 내역",
            icon: "i-lucide-file-json",
            ...downloadLink("tom-refinement.json"),
          },
        ]
      : []),
  ],
]);
function navigate(action: () => void) {
  if (uploading.value || mutating.value) return;
  if (dirty.value) {
    pendingAction = action;
    discardOpen.value = true;
  } else action();
}
function openJob(id: string) {
  navigate(() => {
    studio.selectJob(id);
    position.value = 0;
    if (compact.value) sidebarOpen.value = false;
  });
}
function browseSessions() {
  search.value = "";
  selectedJobId.value = current.value?.id || null;
  if (compact.value) sidebarOpen.value = false;
  sessionBrowserOpen.value = true;
}
function loadSession() {
  if (!selectedJobId.value || !filteredJobs.value.some((job) => job.id === selectedJobId.value)) return;
  sessionBrowserOpen.value = false;
  openJob(selectedJobId.value);
}
function newSession() {
  navigate(() => {
    studio.newSession();
    position.value = 0;
    if (compact.value) sidebarOpen.value = false;
  });
}
function discard() {
  studio.resetEdits();
  discardOpen.value = false;
  pendingAction?.();
  pendingAction = null;
}
function globalDrop(event: DragEvent) {
  event.preventDefault();
}
function shortcuts(event: KeyboardEvent) {
  if (event.key === "s" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    if (dirty.value) studio.save();
  }
  if (
    (event.target as HTMLElement)?.closest(
      'input, textarea, [contenteditable], [role="dialog"]',
    )
  )
    return;
  if (
    event.key === "z" &&
    (event.metaKey || event.ctrlKey) &&
    !busy.value &&
    !loadingEvents.value
  ) {
    event.preventDefault();
    event.shiftKey ? studio.redo() : studio.undo();
  }
}
let media: MediaQueryList | undefined;
function resize() {
  compact.value = window.innerWidth < 1100;
}
onMounted(() => {
  resize();
  sidebarOpen.value = window.innerWidth >= 1024;
  media = window.matchMedia("(max-width: 1099px)");
  media.addEventListener("change", resize);
  window.addEventListener("dragover", globalDrop);
  window.addEventListener("drop", globalDrop);
  window.addEventListener("keydown", shortcuts);
});
onBeforeUnmount(() => {
  media?.removeEventListener("change", resize);
  window.removeEventListener("dragover", globalDrop);
  window.removeEventListener("drop", globalDrop);
  window.removeEventListener("keydown", shortcuts);
});
</script>

<template>
  <UApp :toaster="{ position: 'bottom-right' }">
    <div class="studio-shell">
      <USidebar
        v-model:open="sidebarOpen"
        title="워크스페이스"
        collapsible="icon"
        rail
        :style="{ '--sidebar-width': '244px', '--sidebar-width-icon': '56px' }"
        :ui="{
          container: 'h-full',
          inner: 'bg-muted',
          header: 'p-0',
          body: 'p-0 gap-0',
          footer: 'p-0',
        }"
      >
        <template #header="{ state }"
          ><div class="brand">
            <span class="brand-mark"><UIcon name="i-lucide-audio-lines" /></span
            ><template v-if="state === 'expanded'"
              ><strong>beatit<span>STUDIO</span></strong
              ><span class="brand-version">{{ colabExecution ? "COLAB" : "LOCAL" }}</span></template
            >
          </div></template
        >
        <template #default="{ state }">
          <div class="sidebar-top">
            <UButton
              icon="i-lucide-plus"
              :label="state === 'expanded' ? '새로 만들기' : undefined"
              aria-label="새로 만들기"
              :block="state === 'expanded'"
              color="neutral"
              variant="outline"
              :disabled="uploading || mutating"
              @click="newSession"
            />
            <UButton
              icon="i-lucide-folder-open"
              :label="state === 'expanded' ? '불러오기' : undefined"
              aria-label="불러오기"
              :block="state === 'expanded'"
              color="neutral"
              variant="ghost"
              :disabled="uploading || mutating"
              @click="browseSessions"
            />
          </div>
          <template v-if="result">
            <div v-if="state === 'expanded'" class="sidebar-session-info">
              <SessionInfo :result="result" :event-count="events.length" :dirty="dirty" />
              <SessionStatistics :events="events" />
            </div>
            <UPopover
              v-else
              :content="sessionInfoPopoverContent"
              arrow
            >
              <UButton
                icon="i-lucide-info"
                aria-label="세션 정보"
                color="neutral"
                variant="ghost"
                class="sidebar-info-button"
              />
              <template #content>
                <div class="session-info-popover">
                  <SessionInfo :result="result" :event-count="events.length" :dirty="dirty" />
                  <SessionStatistics :events="events" />
                </div>
              </template>
            </UPopover>
          </template>
        </template>
        <template #footer="{ state }"
          ><div class="sidebar-footer">
            <span
              class="engine-indicator"
              :class="{ online: online && runtimeConnected }"
            />
            <div v-if="state === 'expanded'">
              <strong>{{
                online
                  ? colabExecution
                    ? system?.execution_label || "Google Colab GPU"
                    : "로컬 엔진 연결됨"
                  : "엔진 연결 확인"
              }}</strong
              ><small
                >{{ system?.default_device.toUpperCase() || "—" }} ·
                {{
                  system?.musescore
                    ? "MuseScore"
                    : system?.verovio
                      ? "Verovio"
                      : "렌더러 확인"
                }}</small
              >
              <small v-if="colabExecution" role="status">{{ runtimeDescription }}</small>
            </div>
            <UTooltip text="워크스페이스 도움말"
              ><UButton
                icon="i-lucide-circle-help"
                aria-label="도움말"
                color="neutral"
                variant="ghost"
                size="xs"
                @click="helpOpen = true"
            /></UTooltip></div
        ></template>
      </USidebar>
      <main class="studio-main">
        <header class="app-header">
          <div class="header-left">
            <UButton
              icon="i-lucide-panel-left"
              color="neutral"
              variant="ghost"
              aria-label="사이드바 토글"
              @click="sidebarOpen = !sidebarOpen"
            /><span class="header-section">워크스페이스</span
            ><UIcon
              name="i-lucide-chevron-right"
              class="text-dimmed size-3"
            /><span class="header-filename">{{
              current?.filename || "새 세션"
            }}</span
            ><span
              v-if="dirty"
              class="unsaved-dot"
              title="저장하지 않은 변경"
            />
          </div>
          <div class="header-actions">
            <UButton
              v-if="compact"
              icon="i-lucide-sliders-horizontal"
              aria-label="속성 패널"
              color="neutral"
              variant="ghost"
              @click="inspectorOpen = true"
            /><UDropdownMenu v-if="result" :items="exportItems"
              ><UButton
                label="내보내기"
                icon="i-lucide-download"
                trailing-icon="i-lucide-chevron-down"
                color="neutral"
                variant="outline"
            /></UDropdownMenu>
          </div>
        </header>
        <UAlert
          v-if="error"
          :title="error"
          color="error"
          variant="soft"
          icon="i-lucide-circle-alert"
          :close="{ onClick: () => (error = '') }"
          class="app-alert"
        />
        <div class="workspace-container">
          <USplitter
            id="studio-layout"
            auto-save-id="beatit-layout-v1"
            :items="
              compact
                ? [{ slot: 'workspace', defaultSize: 100, minSize: 100, class: 'overflow-y-auto' }]
                : mainPanels
            "
            class="workspace-splitter"
            :ui="{
              handle: 'studio-divider',
              panel: 'min-w-0 min-h-0 overflow-hidden',
            }"
          >
            <template #workspace>
              <template v-if="current?.result">
                <USplitter
                  id="studio-editor"
                  auto-save-id="beatit-editor-v1"
                  orientation="vertical"
                  :items="editorPanels"
                  :style="{ minHeight: `${timelineMinimumHeight + editorMinimumHeight + 8}px` }"
                  :ui="{
                    handle: 'studio-divider',
                    panel: 'min-w-0 min-h-0 overflow-hidden',
                  }"
                >
                  <template #timeline
                    ><AudioTimeline
                      ref="timeline"
                      :key="current.id"
                      :job="current"
                      :events="events"
                      @time="position = $event"
                      @navigate="beatEditor?.revealPosition($event)"
                      @enabled="playbackEnabled = $event"
                      @minimum-height="timelineMinimumHeight = $event"
                  /></template>
                  <template #editor
                    ><section class="edit-panel">
                      <div class="panel-toolbar edit-toolbar">
                        <UTabs
                          v-model="view"
                          :items="tabs"
                          :content="false"
                          variant="link"
                          size="sm"
                          :ui="{
                            root: 'gap-0',
                            list: 'border-0',
                            trigger: 'px-3 py-3',
                          }"
                        />
                        <div class="edit-actions">
                          <UTooltip text="실행 취소 · ⌘Z"
                            ><UButton
                              icon="i-lucide-undo-2"
                              aria-label="실행 취소"
                              color="neutral"
                              variant="ghost"
                              size="xs"
                              :disabled="
                                busy || loadingEvents || !undoStack.length
                              "
                              @click="studio.undo" /></UTooltip
                          ><UTooltip text="다시 실행 · ⇧⌘Z"
                            ><UButton
                              icon="i-lucide-redo-2"
                              aria-label="다시 실행"
                              color="neutral"
                              variant="ghost"
                              size="xs"
                              :disabled="
                                busy || loadingEvents || !redoStack.length
                              "
                              @click="studio.redo" /></UTooltip
                          ><USeparator
                            orientation="vertical"
                            class="h-4 mx-1"
                          /><UButton
                            :label="busy ? '처리 중' : '악보 재생성'"
                            icon="i-lucide-check"
                            size="xs"
                            :loading="busy"
                            :disabled="busy || loadingEvents || !dirty"
                            @click="studio.save"
                          />
                        </div>
                      </div>
                      <div v-if="busy" class="editor-busy" role="status">
                        <UIcon
                          name="i-lucide-loader-circle"
                          class="animate-spin"
                        />{{ current.progress.message
                        }}<UProgress
                          :model-value="
                            Math.round(current.progress.progress * 100)
                          "
                          size="xs"
                          class="w-24"
                        />
                      </div>
                      <div v-if="loadingEvents" class="loading-view">
                        <UIcon
                          name="i-lucide-loader-circle"
                          class="animate-spin"
                        />
                        타격 데이터 불러오는 중
                      </div>
                      <BeatEditor
                        v-else-if="view === 'edit'"
                        ref="beatEditor"
                        :key="current.id"
                        :events="events"
                        :review="beatReview"
                        :grid="current.result.grid"
                        :duration="current.result.duration"
                        :position="position"
                        :disabled="busy"
                        :playback-enabled="playbackEnabled"
                        @edit="studio.editEvents"
                        @resolve="studio.resolveBeat"
                        @seek="timeline?.seek($event)"
                        @play-bar="timeline?.playBar($event)"
                      />
                      <ScoreViewer
                        v-else
                        :job="current"
                        :position="position"
                        :url="studio.fileUrl"
                        :stale="dirty || busy"
                        @seek="timeline?.seek($event)"
                      /></section
                  ></template>
                </USplitter>
              </template>
              <div v-else-if="current" class="processing-workspace">
                <div class="panel-toolbar">
                  <span><UIcon name="i-lucide-activity" /> 세션 처리</span
                  ><span class="toolbar-caption">ANALYSIS</span>
                </div>
                <div class="processing-center">
                  <span class="processing-symbol"
                    ><UIcon
                      :name="
                        current.status === 'failed'
                          ? 'i-lucide-circle-alert'
                          : 'i-lucide-audio-lines'
                      "
                  /></span>
                  <h1>
                    {{
                      current.status === "failed"
                        ? "분석을 완료하지 못했습니다"
                        : current.status === "cancelled"
                          ? "분석이 취소되었습니다"
                          : "드럼 트랙을 분석하고 있습니다"
                    }}
                  </h1>
                  <p class="processing-filename">{{ current.filename }}</p>
                  <ProcessPanel
                    :job="current"
                    :colab="colabExecution"
                    :disabled="mutating"
                    @cancel="studio.action('cancel')"
                    @retry="studio.action('retry')"
                  />
                </div>
              </div>
              <div v-else-if="loadingEvents" class="loading-view">
                <UIcon name="i-lucide-loader-circle" class="animate-spin" />
                세션 불러오는 중
              </div>
              <SourcePanel
                v-else
                :file="selectedFile"
                :system="system"
                :job="current"
                :busy="busy"
                :uploading="uploading"
                :upload-progress="uploadProgress"
                @choose="studio.chooseFile"
                @clear="selectedFile = null"
                @upload="studio.upload"
                @cancel="studio.action('cancel')"
                @retry="studio.action('retry')"
                @error="error = $event"
              />
            </template>
            <template #inspector
              ><div class="inspector">
                <div class="panel-toolbar">
                  <span
                    ><UIcon name="i-lucide-sliders-horizontal" />
                    {{ result ? "악보 설정" : "분석 설정" }}</span>
                </div>
                <div class="inspector-body">
                  <template v-if="result">
                    <ScoreMetadataFields
                      v-model="metadata"
                      :dirty="metadataDirty"
                      :disabled="busy || loadingEvents"
                      @apply="studio.applyMetadata"
                    />
                    <USeparator class="my-5" />
                  </template>
                  <SessionOptions
                    v-if="result"
                    v-model="revision"
                    :system="system"
                    revision
                    :disabled="busy"
                  /><SessionOptions
                    v-else
                    v-model="options"
                    :system="system"
                    :disabled="busy || !!current"
                  />
                  <template v-if="result">
                    <UCollapsible
                      v-if="result.warnings.length"
                      class="review-notes"
                      ><UButton
                        :label="`검토 메모 ${result.warnings.length}`"
                        icon="i-lucide-message-square-warning"
                        trailing-icon="i-lucide-chevron-down"
                        variant="ghost"
                        color="warning"
                        size="xs"
                        block
                      /><template #content
                        ><p v-for="warning in result.warnings" :key="warning">
                          {{ warning }}
                        </p></template
                      ></UCollapsible
                    ></template
                  >
                  <ProcessPanel
                    v-else-if="!current"
                    :job="null"
                    :colab="colabExecution"
                    compact
                    :uploading="uploading"
                    :upload-progress="uploadProgress"
                  />
                  <div v-if="current" class="inspector-links">
                    <UButton
                      label="처리 로그"
                      icon="i-lucide-terminal"
                      :href="`/api/jobs/${current.id}/log`"
                      target="_blank"
                      color="neutral"
                      variant="ghost"
                      size="xs"
                    />
                  </div>
                  <div v-if="current" class="inspector-danger-zone">
                    <UButton
                      label="세션 삭제"
                      icon="i-lucide-trash-2"
                      color="error"
                      variant="soft"
                      size="xs"
                      block
                      :disabled="busy"
                      @click="deleteOpen = true"
                    />
                  </div>
                </div></div
            ></template>
          </USplitter>
        </div>
        <footer class="status-bar">
          <div>
            <span class="engine-indicator" :class="{ online }" /><span>{{
              uploading
                ? `파일 전송 ${uploadProgress}%`
                : busy
                  ? "처리 중"
                  : dirty
                    ? "저장하지 않은 변경 사항"
                    : result
                      ? "편집 준비 완료"
                      : "소스 파일을 가져오세요"
            }}</span>
          </div>
          <div>
            <span v-if="result" class="mono"
              >{{ result.grid.meter }} ·
              {{
                result.options.grid === "triplet"
                  ? "1/8T"
                  : `1/${result.options.grid}`
              }}
              GRID</span
            ><span class="status-hint">SPACE 재생 / 정지</span
            ><span>BEATIT STUDIO</span>
          </div>
        </footer>
      </main>
    </div>
    <UModal
      v-model:open="sessionBrowserOpen"
      title="불러오기"
      :ui="{ content: 'sm:max-w-2xl', body: 'p-0 sm:p-0', footer: 'justify-end' }"
    >
      <template #body>
        <div class="session-search">
          <UInput
            v-model="search"
            icon="i-lucide-search"
            placeholder="세션 검색"
            aria-label="세션 검색"
            class="w-full"
            autofocus
          />
        </div>
        <div class="session-list-heading" aria-hidden="true">
          <span>이름</span><span>상태</span><span>수정일</span>
        </div>
        <div class="sessions" aria-label="저장된 세션">
          <div v-if="loading" class="p-4 space-y-4">
            <USkeleton class="h-10" /><USkeleton class="h-10" />
          </div>
          <p v-else-if="!filteredJobs.length" class="session-empty">
            {{ search ? "검색 결과가 없습니다." : "저장된 세션이 없습니다." }}
          </p>
          <button
            v-for="job in filteredJobs"
            :key="job.id"
            class="session-item"
            :class="{ selected: job.id === selectedJobId }"
            :aria-pressed="job.id === selectedJobId"
            :disabled="uploading || mutating"
            :title="job.filename"
            @click="selectedJobId = job.id"
            @dblclick="loadSession"
            @keydown.enter.prevent="selectedJobId = job.id; loadSession()"
          >
            <span class="session-filename">
              <UIcon name="i-lucide-file-audio" /><strong>{{ job.filename }}</strong>
            </span>
            <span class="session-status"><i class="status-dot" :class="job.status" />{{ statusLabels[job.status] }}</span>
            <time class="session-date" :datetime="new Date(job.updated * 1000).toISOString()">{{
              new Date(job.updated * 1000).toLocaleDateString("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" })
            }}</time>
          </button>
        </div>
      </template>
      <template #footer>
        <UButton label="취소" color="neutral" variant="outline" @click="sessionBrowserOpen = false" />
        <UButton
          label="불러오기"
          icon="i-lucide-folder-open"
          :disabled="uploading || mutating || !filteredJobs.some((job) => job.id === selectedJobId)"
          @click="loadSession"
        />
      </template>
    </UModal>
    <USlideover
      v-model:open="inspectorOpen"
      title="악보 설정"
      ><template #body
        ><template v-if="result">
          <ScoreMetadataFields
            v-model="metadata"
            :dirty="metadataDirty"
            :disabled="busy || loadingEvents"
            @apply="studio.applyMetadata"
          />
          <USeparator class="my-5" />
        </template><SessionOptions
          v-if="result"
          v-model="revision"
          :system="system"
          revision
          :disabled="busy" /><SessionOptions
          v-else
          v-model="options"
          :system="system"
          :disabled="busy || !!current" /><template v-if="result"
          ><div class="section-label mt-8">검토 메모</div>
          <p
            v-for="warning in result.warnings"
            :key="warning"
            class="text-muted text-xs mb-3"
          >
            {{ warning }}
          </p></template
        ><ProcessPanel
          v-else
          :job="current"
          :colab="colabExecution"
          :uploading="uploading"
          :upload-progress="uploadProgress"
          :disabled="mutating"
          @cancel="studio.action('cancel')"
          @retry="studio.action('retry')" />
        <div v-if="current" class="inspector-links">
          <UButton
            label="처리 로그"
            :href="`/api/jobs/${current.id}/log`"
            target="_blank"
            color="neutral"
            variant="ghost"
          />
        </div>
        <div v-if="current" class="inspector-danger-zone">
          <UButton
            label="세션 삭제"
            icon="i-lucide-trash-2"
            color="error"
            variant="soft"
            block
            :disabled="busy"
            @click="deleteOpen = true"
          /></div></template
      ><template #footer
        ><UButton
          v-if="result"
          label="악보 재생성"
          :disabled="busy || !dirty || loadingEvents"
          :loading="busy"
          block
          @click="
            studio.save();
            inspectorOpen = false;
          " /><UButton
          v-else
          label="설정 완료"
          block
          @click="inspectorOpen = false" /></template
    ></USlideover>
    <UModal
      v-model:open="discardOpen"
      title="편집 내용을 버릴까요?"
      description="저장하지 않은 타격, 악보 정보 또는 설정 변경이 있습니다."
      ><template #footer
        ><UButton
          label="편집 계속"
          color="neutral"
          variant="outline"
          @click="discardOpen = false" /><UButton
          label="변경 버리고 이동"
          color="warning"
          @click="discard" /></template
    ></UModal>
    <UModal
      v-model:open="deleteOpen"
      title="세션을 삭제할까요?"
      description="이 세션의 원본 파일과 모든 분석 결과가 호스트에서 삭제됩니다."
      ><template #footer
        ><UButton
          label="취소"
          color="neutral"
          variant="outline"
          @click="deleteOpen = false" /><UButton
          label="세션 삭제"
          color="error"
          @click="
            deleteOpen = false;
            studio.action('delete');
          " /></template
    ></UModal>
    <UModal
      v-model:open="helpOpen"
      title="Beatit Studio"
      ><template #body
        ><div class="help-content">
          <p>
            오디오와 비디오의 드럼을 분석하고, 원음과 비교하며 비트와 악보를
            다듬는 작업 공간입니다.
          </p>
          <dl>
            <dt>재생 / 정지</dt>
            <dd><UKbd value="space" /></dd>
            <dt>악보 재생성</dt>
            <dd><UKbd value="meta" /><UKbd value="S" /></dd>
            <dt>실행 취소</dt>
            <dd><UKbd value="meta" /><UKbd value="Z" /></dd>
            <dt>다시 실행</dt>
            <dd><UKbd value="meta" /><UKbd value="shift" /><UKbd value="Z" /></dd>
          </dl>
        </div></template
      ></UModal
    >
  </UApp>
</template>
