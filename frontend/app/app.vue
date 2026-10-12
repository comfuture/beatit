<script setup lang="ts">
import type { SplitterItem } from "@nuxt/ui";
import { kit, statusLabels } from "~/types/studio";
import { formatTime } from "~/utils/rhythm";

const studio = useStudio();
const {
  jobs,
  current,
  system,
  selectedFile,
  options,
  revision,
  events,
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
const sidebarOpen = ref(true);
const inspectorOpen = ref(false);
const search = ref("");
const view = ref("edit");
const position = ref(0);
const timeline = useTemplateRef("timeline");
const discardOpen = ref(false);
const deleteOpen = ref(false);
const helpOpen = ref(false);
let pendingAction: (() => void) | null = null;
const compact = ref(false);
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
  { slot: "workspace", defaultSize: 77, minSize: 50 },
  { slot: "inspector", defaultSize: 23, minSize: 19, maxSize: 38 },
];
const editorPanels: SplitterItem[] = [
  { slot: "timeline", defaultSize: 34, minSize: 25, maxSize: 60 },
  { slot: "editor", defaultSize: 66, minSize: 35 },
];
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
const instrumentCounts = computed(() =>
  kit.map((instrument) => ({
    ...instrument,
    count: events.value.filter((e) => e.pitch === instrument.pitch).length,
  })),
);
const maximumCount = computed(() =>
  Math.max(1, ...instrumentCounts.value.map((k) => k.count)),
);
function navigate(action: () => void) {
  if (uploading.value || mutating.value) return;
  if (dirty.value && !busy.value) {
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
        title="세션 브라우저"
        description="저장된 오디오 작업"
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
              ><span class="brand-version">LOCAL</span></template
            >
          </div></template
        >
        <template #default="{ state }">
          <div class="sidebar-top">
            <UButton
              icon="i-lucide-plus"
              :label="state === 'expanded' ? '새 세션' : undefined"
              aria-label="새 세션"
              :block="state === 'expanded'"
              color="neutral"
              variant="outline"
              :disabled="uploading || mutating"
              @click="newSession"
            />
          </div>
          <template v-if="state === 'expanded'">
            <div class="session-heading">
              <span>세션 브라우저</span
              ><UBadge color="neutral" variant="soft" size="xs">{{
                jobs.length
              }}</UBadge>
            </div>
            <div class="session-search">
              <UInput
                v-model="search"
                icon="i-lucide-search"
                placeholder="세션 검색"
                aria-label="세션 검색"
                size="sm"
                class="w-full"
              />
            </div>
            <div class="sessions">
              <div v-if="loading" class="p-4 space-y-4">
                <USkeleton class="h-10" /><USkeleton class="h-10" /><USkeleton
                  class="h-10"
                />
              </div>
              <p v-else-if="!filteredJobs.length" class="session-empty">
                {{
                  search
                    ? "검색 결과가 없습니다."
                    : "아직 저장된 세션이 없습니다."
                }}
              </p>
              <button
                v-for="job in filteredJobs"
                :key="job.id"
                class="session-item"
                :class="{ selected: job.id === current?.id }"
                :aria-current="job.id === current?.id ? 'true' : undefined"
                :disabled="uploading || mutating"
                :title="job.filename"
                @click="openJob(job.id)"
              >
                <span class="session-file-icon"
                  ><UIcon name="i-lucide-file-audio" /></span
                ><span class="session-info"
                  ><strong>{{ job.filename.replace(/\.[^.]+$/, "") }}</strong
                  ><small
                    ><i class="status-dot" :class="job.status" />{{
                      statusLabels[job.status]
                    }}<span>{{
                      new Date(job.created * 1000).toLocaleDateString("ko-KR", {
                        month: "2-digit",
                        day: "2-digit",
                      })
                    }}</span></small
                  ></span
                >
              </button>
            </div>
            <div class="sidebar-guide">
              <span class="eyebrow">YOUR LOCAL WORKSPACE</span>
              <p>소리에서 리듬으로.<br />검토하고, 다듬고, 연주하세요.</p>
              <span class="sidebar-guide-line" />
            </div>
          </template>
        </template>
        <template #footer="{ state }"
          ><div class="sidebar-footer">
            <span class="engine-indicator" :class="{ online }" />
            <div v-if="state === 'expanded'">
              <strong>{{
                online ? "로컬 엔진 연결됨" : "엔진 연결 확인"
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
              aria-label="세션 브라우저 토글"
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
                ? [{ slot: 'workspace', defaultSize: 100, minSize: 100 }]
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
                      @time="position = $event"
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
                        :key="current.id"
                        :events="events"
                        :grid="current.result.grid"
                        :duration="current.result.duration"
                        :position="position"
                        :disabled="busy"
                        @edit="studio.editEvents"
                        @seek="timeline?.seek($event)"
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
                    {{ result ? "세션 속성" : "분석 설정" }}</span
                  ><UIcon name="i-lucide-settings-2" class="text-dimmed" />
                </div>
                <div class="inspector-body">
                  <template v-if="result"
                    ><div class="section-label">
                      SESSION INFO
                      <UBadge size="xs" variant="subtle" color="neutral">{{
                        dirty ? "수정됨" : "저장됨"
                      }}</UBadge>
                    </div>
                    <div class="session-metrics">
                      <div>
                        <strong>{{ result.grid.bpm.toFixed(1) }}</strong
                        ><span>BPM</span>
                      </div>
                      <div>
                        <strong>{{ result.grid.meter }}</strong
                        ><span>TIME SIG.</span>
                      </div>
                      <div>
                        <strong>{{ result.grid.bars }}</strong
                        ><span>BARS</span>
                      </div>
                    </div>
                    <div class="metadata-row">
                      <span>오디오 길이</span
                      ><span class="mono">{{
                        formatTime(result.duration)
                      }}</span>
                    </div>
                    <div class="metadata-row">
                      <span>전체 타격</span
                      ><span class="mono">{{
                        events.length.toLocaleString()
                      }}</span>
                    </div>
                    <USeparator class="my-5"
                  /></template>
                  <div class="section-label">
                    {{ result ? "SCORE SETTINGS" : "TRANSCRIPTION" }}
                  </div>
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
                  <template v-if="result"
                    ><USeparator class="my-5" />
                    <div class="section-label">
                      DETECTED INSTRUMENTS <span>{{ events.length }}</span>
                    </div>
                    <div class="kit-counts">
                      <div
                        v-for="instrument in instrumentCounts"
                        :key="instrument.pitch"
                        class="kit-count"
                        :style="{ '--track-color': instrument.color }"
                      >
                        <span><i />{{ instrument.name }}</span
                        ><b class="mono">{{ instrument.count }}</b>
                        <div
                          :style="{
                            width: `${(instrument.count / maximumCount) * 100}%`,
                          }"
                        />
                      </div>
                    </div>
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
                    /><UButton
                      label="세션 삭제"
                      icon="i-lucide-trash-2"
                      color="neutral"
                      variant="ghost"
                      size="xs"
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
    <USlideover
      v-model:open="inspectorOpen"
      title="세션 속성"
      description="템포, 박자 및 채보 설정"
      ><template #body
        ><SessionOptions
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
          /><UButton
            label="세션 삭제"
            icon="i-lucide-trash-2"
            color="error"
            variant="ghost"
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
      description="저장하지 않은 타격 및 악보 설정 변경이 있습니다."
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
      description="오디오에서 편집 가능한 드럼 악보까지."
      ><template #body
        ><div class="help-content">
          <p>
            새 세션에서 원본을 가져온 후 분석을 시작하세요. 완료되면 원음과 드럼
            스템을 비교하면서 비트와 악보를 검토할 수 있습니다.
          </p>
          <p>
            비트 셀을 클릭하면 타격이 추가되거나 삭제됩니다. 템포·박자·첫 마디는
            속성 패널에서 조정하고 <strong>악보 재생성</strong>으로 저장하세요.
          </p>
          <dl>
            <dt>재생 / 정지</dt>
            <dd><UKbd>space</UKbd></dd>
            <dt>저장 및 악보 재생성</dt>
            <dd><UKbd>meta</UKbd> <UKbd>S</UKbd></dd>
            <dt>실행 취소</dt>
            <dd><UKbd>meta</UKbd> <UKbd>Z</UKbd></dd>
            <dt>다시 실행</dt>
            <dd><UKbd>meta</UKbd> <UKbd>shift</UKbd> <UKbd>Z</UKbd></dd>
          </dl>
          <p>
            분석 결과는 편집용 초안입니다. 원음과 비교해 템포와 악기 배치를
            확인하세요. 직접 권리를 보유하거나 처리 허가를 받은 음원을
            사용하세요.
          </p>
        </div></template
      ></UModal
    >
  </UApp>
</template>
