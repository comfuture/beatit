<script setup lang="ts">
import type { Job } from "~/types/studio";
import { statusLabels } from "~/types/studio";
const props = defineProps<{
  job: Job | null;
  uploading?: boolean;
  uploadProgress?: number;
  compact?: boolean;
  disabled?: boolean;
  colab?: boolean;
}>();
defineEmits<{ cancel: []; retry: [] }>();
const preparingRemote = computed(() =>
  ["allocating", "preparing", "uploading"].includes(props.job?.progress.stage || ""),
);
const stages = computed(() => [
  {
    label: preparingRemote.value ? "Colab GPU 준비" : "오디오 준비",
    detail: preparingRemote.value ? "GPU 연결 · 실행 환경 및 오디오 준비" : "원본에서 오디오 추출",
    icon: "i-lucide-file-audio",
    stages: ["allocating", "preparing", "uploading", "converting"],
  },
  {
    label: "드럼 분리",
    detail: "전체 믹스와 악기별 스템 분리",
    icon: "i-lucide-audio-lines",
    stages: ["separating"],
  },
  {
    label: "비트 분석",
    detail: "타격 · 템포 · 다운비트 검출",
    icon: "i-lucide-chart-no-axes-column",
    stages: ["transcribing", "rhythm", "refining"],
  },
  {
    label: "악보 생성",
    detail: "리듬 격자 적용 및 조판",
    icon: "i-lucide-file-music",
    stages: ["engraving", "downloading", "publishing"],
  },
]);
const index = computed(() =>
  props.job?.status === "completed"
    ? stages.value.length
    : stages.value.findIndex((s) =>
        s.stages.includes(props.job?.progress.stage || ""),
      ),
);
const active = computed(() =>
  ["running", "queued"].includes(props.job?.status || ""),
);
const percent = computed(() =>
  props.uploading
    ? props.uploadProgress || 0
    : props.job?.status === "completed"
      ? 100
      : Math.round((props.job?.progress.progress || 0) * 100),
);
</script>

<template>
  <section class="process-panel" :class="{ compact }" aria-label="분석 과정">
    <div class="section-label">
      <span>PROCESSING</span
      ><UBadge
        :color="
          job?.status === 'failed'
            ? 'error'
            : job?.status === 'completed'
              ? 'primary'
              : 'neutral'
        "
        variant="subtle"
        size="xs"
        >{{
          uploading ? "업로드 중" : job ? statusLabels[job.status] : "준비"
        }}</UBadge
      >
    </div>
    <div class="process-stages">
      <div
        v-for="(stage, i) in stages"
        :key="stage.label"
        class="process-stage"
        :class="{ done: i < index, active: i === index && active }"
      >
        <span class="stage-icon"
          ><UIcon
            :name="
              i < index
                ? 'i-lucide-check'
                : i === index && active
                  ? 'i-lucide-loader-circle'
                  : stage.icon
            "
            :class="{ 'animate-spin': i === index && active }"
        /></span>
        <div>
          <strong>{{ stage.label }}</strong>
          <p>{{ stage.detail }}</p>
        </div>
        <span class="stage-index">0{{ i + 1 }}</span>
      </div>
    </div>
    <div class="process-caption">
      <span>{{
        uploading
          ? "파일 전송 중"
          : job?.status === "cancelled"
            ? "분석이 취소되었습니다"
            : job?.progress.message || "소스 파일을 기다리고 있습니다"
      }}</span
      ><b class="mono">{{ percent }}%</b>
    </div>
    <UProgress
      :model-value="percent"
      size="xs"
      :color="job?.status === 'failed' ? 'error' : 'primary'"
      aria-label="분석 진행률"
    />
    <p v-if="job?.error" class="process-error">{{ job.error }}</p>
    <div class="process-actions">
      <small>{{
        job?.progress.elapsed_seconds
          ? `${Math.round(job.progress.elapsed_seconds)}초 경과`
          : colab ? "Colab GPU 연결 후 분석합니다" : "첫 실행 시 모델을 다운로드합니다"
      }}</small>
      <UButton
        v-if="active"
        label="취소"
        color="neutral"
        variant="ghost"
        size="xs"
        :disabled="disabled"
        @click="$emit('cancel')"
      />
      <UButton
        v-if="job && ['failed', 'cancelled'].includes(job.status)"
        label="다시 시도"
        icon="i-lucide-rotate-cw"
        variant="soft"
        size="xs"
        :disabled="disabled"
        @click="$emit('retry')"
      />
    </div>
  </section>
</template>
