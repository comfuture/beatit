<script setup lang="ts">
import type { Result } from "~/types/studio";
import { formatTime } from "~/utils/rhythm";

defineProps<{ result: Result; eventCount: number; dirty: boolean }>();
</script>

<template>
  <section class="session-summary" aria-label="세션 정보">
    <div class="section-label">
      세션 정보
      <UBadge size="xs" variant="subtle" color="neutral">{{ dirty ? "수정됨" : "저장됨" }}</UBadge>
    </div>
    <div class="session-metrics">
      <div><strong>{{ result.grid.bpm.toFixed(1) }}</strong><span>BPM</span></div>
      <div><strong>{{ result.grid.meter }}</strong><span>박자</span></div>
      <div><strong>{{ result.grid.bars }}</strong><span>마디</span></div>
    </div>
    <div class="metadata-row">
      <span>오디오 길이</span><span class="mono">{{ formatTime(result.duration) }}</span>
    </div>
    <div class="metadata-row">
      <span>전체 타격</span><span class="mono">{{ eventCount.toLocaleString() }}</span>
    </div>
  </section>
</template>
