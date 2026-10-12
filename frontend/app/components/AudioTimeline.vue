<script setup lang="ts">
import type { Job } from "~/types/studio";
import { formatTime } from "~/utils/rhythm";
const props = defineProps<{ job: Job }>();
const emit = defineEmits<{ time: [value: number] }>();
const player = useTemplateRef("player");
const source = ref("audio.wav");
const position = ref(0);
const playing = ref(false);
const volume = ref(80);
const rate = ref(1);
const peaks = ref<number[]>([]);
const waveLoading = ref(false);
const waveError = ref("");
const playbackError = ref("");
let restoreTime = 0;
let resume = false;
let request: AbortController | undefined;
let playbackFrame: number | undefined;
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
  restoreTime = position.value;
  resume = playing.value;
  playing.value = false;
  stopPlaybackClock();
  playbackError.value = "";
  loadWaveform();
});
watch(volume, (value) => {
  if (player.value) player.value.volume = value / 100;
});
watch(rate, (value) => {
  if (player.value) player.value.playbackRate = value;
});
async function play() {
  try {
    await player.value?.play();
    playbackError.value = "";
  } catch {
    playbackError.value = "오디오를 재생할 수 없습니다. 파일을 확인하세요.";
  }
}
function toggle() {
  if (player.value?.paused) play();
  else player.value?.pause();
}
function seek(value: number) {
  if (!player.value || !Number.isFinite(value)) return;
  const next = Math.max(0, Math.min(duration.value, value));
  player.value.currentTime = next;
  position.value = next;
  emit("time", next);
}
function waveSeek(event: MouseEvent) {
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
  seek(((event.clientX - bounds.left) / bounds.width) * duration.value);
}
function ready() {
  if (!player.value) return;
  player.value.volume = volume.value / 100;
  player.value.playbackRate = rate.value;
  seek(restoreTime);
  if (resume) play();
  resume = false;
}
function updateTime() {
  position.value = player.value?.currentTime || 0;
  emit("time", position.value);
}
function stopPlaybackClock() {
  if (playbackFrame !== undefined) cancelAnimationFrame(playbackFrame);
  playbackFrame = undefined;
}
function startPlaybackClock() {
  stopPlaybackClock();
  function sample() {
    updateTime();
    const audio = player.value;
    playbackFrame =
      audio && !audio.paused && !audio.ended
        ? requestAnimationFrame(sample)
        : undefined;
  }
  // Read the media clock, including seeks/rate changes, instead of accumulating
  // elapsed wall time. Native timeupdate still covers background tabs.
  sample();
}
function pausePlayback() {
  playing.value = false;
  stopPlaybackClock();
  updateTime();
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
  window.addEventListener("keydown", shortcut);
});
onBeforeUnmount(() => {
  stopPlaybackClock();
  request?.abort();
  window.removeEventListener("keydown", shortcut);
  player.value?.pause();
});
defineExpose({ seek });
</script>

<template>
  <section class="audio-timeline" aria-label="오디오 타임라인">
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
        class="wave-track"
        :class="{ drums: source !== 'audio.wav' }"
        role="slider"
        tabindex="0"
        aria-label="오디오 재생 위치"
        :aria-valuemin="0"
        :aria-valuemax="duration"
        :aria-valuenow="position"
        :aria-valuetext="formatTime(position)"
        @click="waveSeek"
        @keydown.right.prevent="seek(position + 1)"
        @keydown.left.prevent="seek(position - 1)"
        @keydown.home.prevent="seek(0)"
        @keydown.end.prevent="seek(duration)"
      >
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
    </div>
    <div class="transport">
      <div class="transport-buttons">
        <UTooltip text="처음으로"
          ><UButton
            icon="i-lucide-skip-back"
            aria-label="처음으로"
            color="neutral"
            variant="ghost"
            @click="seek(0)" /></UTooltip
        ><UButton
          :icon="playing ? 'i-lucide-pause' : 'i-lucide-play'"
          :aria-label="playing ? '일시 정지' : '재생'"
          variant="soft"
          @click="toggle"
        /><UButton
          icon="i-lucide-square"
          aria-label="정지"
          color="neutral"
          variant="ghost"
          @click="
            player?.pause();
            seek(0);
          "
        />
      </div>
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
        /><UIcon name="i-lucide-volume-2" class="volume-icon" /><USlider
          v-model="volume"
          :min="0"
          :max="100"
          aria-label="볼륨"
          class="volume-slider"
          size="xs"
        />
      </div>
    </div>
    <p v-if="playbackError" class="px-4 text-error text-xs" role="alert">
      {{ playbackError }}
    </p>
    <audio
      ref="player"
      :src="url"
      preload="metadata"
      @loadedmetadata="ready"
      @timeupdate="updateTime"
      @play="playing = true"
      @playing="startPlaybackClock"
      @waiting="stopPlaybackClock"
      @emptied="stopPlaybackClock"
      @seeked="updateTime"
      @pause="pausePlayback"
      @ended="pausePlayback"
      @error="
        pausePlayback();
        playbackError = '오디오 파일을 불러오지 못했습니다.';
      "
    />
  </section>
</template>
