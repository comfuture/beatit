<script setup lang="ts">
import type { Job, SystemInfo } from "~/types/studio";
const props = defineProps<{
  file: File | null;
  system: SystemInfo | null;
  job: Job | null;
  busy: boolean;
  uploading: boolean;
  uploadProgress: number;
}>();
const emit = defineEmits<{
  choose: [file: File];
  clear: [];
  upload: [];
  cancel: [];
  retry: [];
  error: [message: string];
}>();
const input = useTemplateRef("input");
const dragging = ref(false);
function drop(event: DragEvent) {
  dragging.value = false;
  if (props.busy) return;
  if (event.dataTransfer?.files.length !== 1) {
    emit("error", "한 번에 하나의 파일을 선택하세요.");
    return;
  }
  emit("choose", event.dataTransfer.files[0]!);
}
function select(event: Event) {
  const target = event.target as HTMLInputElement;
  if (target.files?.[0]) emit("choose", target.files[0]);
  target.value = "";
}
</script>

<template>
  <section class="source-panel">
    <div class="panel-toolbar">
      <span><UIcon name="i-lucide-folder-input" /> 소스 가져오기</span
      ><span class="toolbar-caption">NEW SESSION</span>
    </div>
    <div class="source-body">
      <div class="source-heading">
        <span class="eyebrow">AUDIO TO NOTATION</span>
        <h1>새로운 드럼 세션</h1>
        <p>원본을 불러오고, 리듬을 분석하고, 악보를 다듬으세요.</p>
      </div>
      <input
        ref="input"
        type="file"
        class="sr-only"
        aria-label="소스 파일 선택"
        accept="audio/*,video/*,.mkv,.flac,.aiff,.opus,.wma"
        :disabled="busy"
        @change="select"
      />
      <button
        class="drop-zone"
        :class="{ dragging, 'has-file': file }"
        :disabled="busy"
        @click="input?.click()"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent.stop="drop"
      >
        <span class="drop-icon"
          ><UIcon :name="file ? 'i-lucide-file-audio' : 'i-lucide-import'"
        /></span>
        <strong>{{
          file ? file.name : "오디오 또는 비디오를 여기에 놓으세요"
        }}</strong>
        <span>{{
          file
            ? `${(file.size / 1024 ** 2).toFixed(1)} MB · 클릭하여 파일 변경`
            : "또는 클릭하여 파일 선택"
        }}</span>
        <small>WAV · MP3 · FLAC · M4A · MP4 · MOV</small>
      </button>
      <div class="source-actions">
        <span
          >최대
          {{
            ((system?.max_upload_bytes || 1024 ** 3) / 1024 ** 3).toFixed(1)
          }}
          GB ·
          {{ Math.round((system?.max_duration_seconds || 1800) / 60) }}분</span
        ><UButton
          v-if="file"
          label="선택 해제"
          variant="ghost"
          color="neutral"
          size="xs"
          :disabled="busy"
          @click="$emit('clear')"
        />
      </div>
      <UButton
        label="분석 시작"
        icon="i-lucide-play"
        :loading="uploading"
        :disabled="!file || busy || !system?.ffmpeg"
        block
        size="lg"
        @click="$emit('upload')"
      />
      <div class="source-notes">
        <UIcon name="i-lucide-hard-drive" />
        <p>오디오와 분석 결과는 실행 중인 호스트에 저장됩니다.</p>
      </div>
      <div class="workflow-overview">
        <div>
          <UIcon name="i-lucide-audio-lines" /><strong>01</strong
          ><span>분리 & 분석</span>
        </div>
        <UIcon name="i-lucide-chevron-right" />
        <div>
          <UIcon name="i-lucide-file-music" /><strong>02</strong
          ><span>악보 검토</span>
        </div>
        <UIcon name="i-lucide-chevron-right" />
        <div>
          <UIcon name="i-lucide-sliders-horizontal" /><strong>03</strong
          ><span>비트 편집</span>
        </div>
      </div>
    </div>
  </section>
</template>
