<script setup lang="ts">
import type { Job, SystemInfo } from "~/types/studio";
import { connectionUnavailable, isRemoteExecution, runtimeStatus } from "~/utils/execution";
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
const remoteExecution = computed(() => isRemoteExecution(props.system));
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
    <div class="source-body">
      <div class="source-heading">
        <h1>새 세션</h1>
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
          file ? file.name : "오디오·비디오 파일 선택"
        }}</strong>
        <span>{{
          file
            ? `${(file.size / 1024 ** 2).toFixed(1)} MB · 클릭하여 파일 변경`
            : "파일을 끌어다 놓을 수도 있습니다."
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
        :disabled="!file || busy || !system?.ffmpeg || connectionUnavailable(system)"
        block
        size="lg"
        @click="$emit('upload')"
      />
      <UButton
        v-if="uploading && uploadProgress < 100"
        label="업로드 취소"
        variant="ghost"
        color="neutral"
        size="sm"
        block
        @click="$emit('cancel')"
      />
      <p v-if="remoteExecution" class="source-notes" role="status">
        <UIcon name="i-lucide-cloud" />{{ runtimeStatus(system) }}
      </p>
    </div>
  </section>
</template>
