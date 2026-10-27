<script setup lang="ts">
import type { DrumEvent, Job } from "~/types/studio";
import { formatTime } from "~/utils/rhythm";
import { timePlaybackRange } from "~/utils/drumPlayback";
const props = defineProps<{ job: Job; events: DrumEvent[] }>();
const emit = defineEmits<{
  time: [value: number];
  navigate: [value: number];
  enabled: [value: boolean];
  minimumHeight: [value: number];
}>();
const surface = useTemplateRef("surface");
const player = useTemplateRef("player");
const waveform = useTemplateRef("waveform");
const source = ref("audio.wav");
const peaks = ref<number[]>([]);
const waveLoading = ref(false);
const waveError = ref("");
let request: AbortController | undefined;
let layoutObserver: ResizeObserver | undefined;
let lastMinimumHeight = 0;
function measureMinimumHeight() {
  if (!surface.value || !waveform.value) return;
  const waveArea = surface.value.querySelector<HTMLElement>(".wave-area")!;
  const controlsHeight = [...surface.value.children]
    .filter((child) => child !== waveArea)
    .reduce((sum, child) => sum + child.getBoundingClientRect().height, 0);
  const waveLabelsHeight = [...waveArea.children]
    .filter((child) => child !== waveform.value)
    .reduce((sum, child) => sum + child.getBoundingClientRect().height, 0);
  const minimum = Math.ceil(controlsHeight + waveLabelsHeight
    + Number.parseFloat(getComputedStyle(waveform.value).minHeight)) + 2;
  if (minimum !== lastMinimumHeight) {
    lastMinimumHeight = minimum;
    emit("minimumHeight", minimum);
  }
}
const sources = computed(() => [
  { label: "원본 오디오", value: "audio.wav" },
  { label: "분리된 드럼", value: "drums.wav" },
  ...(props.job.result?.stems || []).map((value) => ({
    value,
    label:
      (
        {
          "stem-kick.flac": "Kick",
          "stem-snare.flac": "Snare",
          "stem-toms.flac": "Toms",
          "stem-hh.flac": "Hi-hat",
          "stem-cymbals.flac": "Cymbals",
        } as Record<string, string>
      )[value] || value,
  })),
]);
const duration = computed(() => props.job.result?.duration || 0);
const transport = usePlaybackTransport(
  player, computed(() => props.events), computed(() => props.job.result!.grid),
  duration, (value) => emit("time", value),
  (value) => emit("navigate", value),
);
const { position, playing, audioVolume, drumVolume, rate, audioEnabled, drumsEnabled, range, repeatEnabled, pendingEdits, canPlay, error: playbackError } = transport;
const { seek, toggle } = transport;
const drag = shallowRef<{
  pointer: number; target: HTMLElement; left: number; width: number;
  originX: number; first: number; last: number; moved: boolean;
} | null>(null);
const displayedRange = computed(() => drag.value?.moved
  ? timePlaybackRange(drag.value.first, drag.value.last, duration.value)
  : range.value);
watch(canPlay, (value) => emit("enabled", value), { immediate: true });
const url = computed(() => `/api/jobs/${props.job.id}/files/${source.value}`);
const path = computed(() =>
  peaks.value
    .map((value, index) => {
      const x = (index / Math.max(1, peaks.value.length - 1)) * 1200;
      const h = Math.max(0.4, value * 47);
      return `M${x.toFixed(1)},${(50 - h).toFixed(1)}V${(50 + h).toFixed(1)}`;
    })
    .join(" "),
);
async function loadWaveform() {
  request?.abort();
  request = new AbortController();
  const signal = request.signal;
  waveLoading.value = true;
  waveError.value = "";
  peaks.value = [];
  try {
    const response = await fetch(
      `/api/jobs/${props.job.id}/waveform?source=${encodeURIComponent(source.value)}`,
      { signal },
    );
    if (!response.ok)
      throw new Error(
        "파형을 불러올 수 없습니다. 오디오 재생은 계속 사용할 수 있습니다.",
      );
    const data = await response.json();
    if (!signal.aborted) peaks.value = data.peaks;
  } catch (e) {
    if (!signal.aborted)
      waveError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!signal.aborted) waveLoading.value = false;
  }
}
watch(source, () => {
  transport.sourceChanged();
  loadWaveform();
});
function waveSeek(event: MouseEvent) {
  // Pointer clicks are handled on pointerup; retain synthetic/assistive clicks.
  if (event.detail !== 0) return;
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
  seek(((event.clientX - bounds.left) / bounds.width) * duration.value);
}
function dragTime(clientX: number, left: number, width: number) {
  return Math.max(0, Math.min(duration.value, (clientX - left) / width * duration.value));
}
function beginSelection(event: PointerEvent) {
  if (event.button !== 0 || !event.isPrimary || duration.value <= 0 || drag.value) return;
  const target = event.currentTarget as HTMLElement;
  const bounds = target.getBoundingClientRect();
  if (!bounds.width) return;
  event.preventDefault();
  target.focus({ preventScroll: true });
  const time = dragTime(event.clientX, bounds.left, bounds.width);
  drag.value = { pointer: event.pointerId, target, left: bounds.left, width: bounds.width,
    originX: event.clientX, first: time, last: time, moved: false };
  target.setPointerCapture(event.pointerId);
}
function moveSelection(event: PointerEvent) {
  const selection = drag.value;
  if (!selection || selection.pointer !== event.pointerId) return;
  drag.value = { ...selection,
    last: dragTime(event.clientX, selection.left, selection.width),
    moved: selection.moved || Math.abs(event.clientX - selection.originX) >= 4,
  };
}
function cancelSelection(event?: PointerEvent) {
  const selection = drag.value;
  if (!selection || (event && event.pointerId !== selection.pointer)) return;
  drag.value = null;
  if (selection.target.hasPointerCapture(selection.pointer))
    selection.target.releasePointerCapture(selection.pointer);
}
function finishSelection(event: PointerEvent) {
  if (drag.value?.pointer !== event.pointerId) return;
  moveSelection(event);
  const selection = drag.value!;
  cancelSelection();
  if (selection.moved) transport.selectRange(selection.first, selection.last);
  else seek(selection.last);
}
function escapeSelection() {
  if (drag.value) cancelSelection();
  else transport.clearRange();
}
function clearSelection() {
  transport.clearRange();
  waveform.value?.focus({ preventScroll: true });
}
function extendSelection(direction: number) {
  const first = range.value?.start ?? position.value;
  const last = range.value?.end ?? position.value;
  transport.selectRange(direction < 0 ? first - 1 : first, direction > 0 ? last + 1 : last);
}
function shortcut(event: KeyboardEvent) {
  if (
    (event.target as HTMLElement)?.closest(
      'input, textarea, select, button, [role="slider"], [role="combobox"], [contenteditable], [role="dialog"]',
    )
  )
    return;
  if (event.code === "Space") {
    event.preventDefault();
    toggle();
  }
}
onMounted(() => {
  loadWaveform();
  layoutObserver = new ResizeObserver(measureMinimumHeight);
  layoutObserver.observe(surface.value!);
  for (const child of surface.value!.querySelectorAll(":scope > *, .time-ruler, .wave-selection-help"))
    layoutObserver.observe(child);
  measureMinimumHeight();
  window.addEventListener("keydown", shortcut);
});
watch(playbackError, measureMinimumHeight, { flush: "post" });
onBeforeUnmount(() => {
  cancelSelection();
  request?.abort();
  layoutObserver?.disconnect();
  window.removeEventListener("keydown", shortcut);
});
defineExpose({ seek, playBar: transport.playBar });
</script>

<template>
  <section ref="surface" class="audio-timeline" aria-label="오디오 타임라인">
    <div class="panel-toolbar">
      <span><UIcon name="i-lucide-audio-lines" /> 오디오</span>
      <div class="flex items-center gap-2">
        <span class="toolbar-caption hidden sm:block">SOURCE</span
        ><USelect
          v-model="source"
          :items="sources"
          aria-label="재생 소스"
          class="w-40"
          size="xs"
        />
      </div>
    </div>
    <div class="wave-area">
      <div class="time-ruler">
        <span v-for="n in 7" :key="n">{{
          formatTime((duration * (n - 1)) / 6).split(".")[0]
        }}</span>
      </div>
      <div
        ref="waveform"
        class="wave-track"
        :class="{ drums: source !== 'audio.wav', selecting: drag?.moved }"
        role="slider"
        tabindex="0"
        aria-label="오디오 재생 위치"
        :aria-valuemin="0"
        :aria-valuemax="duration"
        :aria-valuenow="position"
        :aria-valuetext="formatTime(position)"
        aria-describedby="wave-selection-help"
        aria-keyshortcuts="Shift+ArrowLeft Shift+ArrowRight Escape"
        @click="waveSeek"
        @pointerdown="beginSelection"
        @pointermove="moveSelection"
        @pointerup="finishSelection"
        @pointercancel="cancelSelection"
        @lostpointercapture="cancelSelection"
        @keydown.right.exact.prevent="seek(position + 1)"
        @keydown.left.exact.prevent="seek(position - 1)"
        @keydown.shift.right.exact.prevent="extendSelection(1)"
        @keydown.shift.left.exact.prevent="extendSelection(-1)"
        @keydown.esc.prevent="escapeSelection"
        @keydown.home.prevent="seek(0)"
        @keydown.end.prevent="seek(duration)"
      >
        <div
          v-if="displayedRange"
          class="playback-region"
          :data-start="displayedRange.start"
          :data-end="displayedRange.end"
          :style="{ left: `${displayedRange.start / duration * 100}%`, width: `${(displayedRange.end - displayedRange.start) / duration * 100}%` }"
          aria-hidden="true"
        />
        <span class="track-tag">{{
          source === "audio.wav" ? "ORIGINAL" : "DRUM STEM"
        }}</span>
        <svg
          v-if="peaks.length"
          viewBox="0 0 1200 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path
            :d="path"
            fill="none"
            stroke="currentColor"
            stroke-width="0.8"
          />
        </svg>
        <div v-else class="wave-placeholder">
          <UIcon
            v-if="waveLoading"
            name="i-lucide-loader-circle"
            class="animate-spin"
          /><span>{{ waveLoading ? "파형 불러오는 중" : waveError }}</span>
        </div>
        <div
          class="playhead"
          :style="{ left: `${duration ? (position / duration) * 100 : 0}%` }"
        >
          <i />
        </div>
      </div>
      <p id="wave-selection-help" class="wave-selection-help">클릭: 위치 이동 · 드래그: 구간 선택 <span class="hidden sm:inline">· Shift+←/→: 구간 확장 · Esc: 선택 해제</span></p>
    </div>
    <div class="transport">
      <div class="transport-buttons">
        <UTooltip text="처음으로"
          ><UButton
            icon="i-lucide-skip-back"
            aria-label="처음으로"
            color="neutral"
            variant="ghost"
            @click="seek(range?.start || 0)" /></UTooltip
        ><UButton
          :icon="playing ? 'i-lucide-pause' : 'i-lucide-play'"
          :aria-label="playing ? '일시 정지' : '재생'"
          variant="soft"
          :disabled="!canPlay"
          @click="toggle"
        /><UButton
          icon="i-lucide-square"
          aria-label="정지"
          color="neutral"
          variant="ghost"
          @click="transport.stop"
        />
      </div>
      <UTooltip :text="range ? '선택구간을 반복합니다. 비트 수정은 다음 반복부터 반영됩니다.' : '마디나 웨이브폼에서 재생 구간을 먼저 선택하세요.'">
        <UButton
          label="구간 반복"
          aria-label="구간 반복"
          :ui="{ label: 'hidden md:inline' }"
          icon="i-lucide-repeat"
          :aria-pressed="repeatEnabled"
          :color="repeatEnabled ? 'primary' : 'neutral'"
          :variant="repeatEnabled ? 'soft' : 'ghost'"
          size="xs"
          :disabled="!range"
          @click="repeatEnabled = !repeatEnabled"
        />
      </UTooltip>
      <PlaybackMixer
        v-model:audio-enabled="audioEnabled"
        v-model:drums-enabled="drumsEnabled"
        v-model:audio-volume="audioVolume"
        v-model:drum-volume="drumVolume"
      />
      <span class="transport-clock mono"
        >{{ formatTime(position)
        }}<small> / {{ formatTime(duration) }}</small></span
      >
      <div class="transport-right">
        <USelect
          v-model="rate"
          :items="[
            { label: '0.5×', value: 0.5 },
            { label: '0.75×', value: 0.75 },
            { label: '1×', value: 1 },
            { label: '1.25×', value: 1.25 },
          ]"
          aria-label="재생 속도"
          size="xs"
          class="w-20"
        />
      </div>
    </div>
    <div class="playback-status" :class="{ 'has-selection': range }" role="status">
      <div v-if="range" class="playback-selection" role="group" aria-label="선택구간">
        <span>{{ range.bar ? `${range.bar}마디 구간재생` : '선택구간 재생' }} · {{ formatTime(range.start) }}–{{ formatTime(range.end) }}</span>
        <UTooltip text="현재 위치에서 전체 곡 재생으로 돌아갑니다.">
          <UButton label="선택구간 해제" icon="i-lucide-x" variant="outline"
            size="xs" class="clear-playback-range" @click="clearSelection" />
        </UTooltip>
      </div>
      <span v-else>전체 재생</span>
      <span v-if="range && repeatEnabled">반복 켜짐</span>
      <span v-if="repeatEnabled && pendingEdits" class="text-primary">수정 내용은 다음 반복부터 반영됩니다.</span>
      <span v-if="!canPlay">재생할 트랙을 켜세요.</span>
      <span class="transport-clock-compact mono">{{ formatTime(position) }} / {{ formatTime(duration) }}</span>
    </div>
    <p v-if="playbackError" class="px-4 text-error text-xs" role="alert">
      {{ playbackError }}
    </p>
    <audio
      ref="player"
      :src="url"
      preload="metadata"
      @loadedmetadata="transport.mediaReady"
      @timeupdate="transport.mediaTime"
      @playing="transport.mediaPlaying"
      @waiting="transport.mediaWaiting"
      @seeked="transport.mediaSeeked"
      @pause="transport.mediaPaused"
      @ended="transport.mediaEnded"
      @error="transport.mediaError"
    />
  </section>
</template>
