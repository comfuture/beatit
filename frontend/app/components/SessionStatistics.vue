<script setup lang="ts">
import { kit, type DrumEvent } from "~/types/studio";

const props = defineProps<{ events: DrumEvent[] }>();
const instrumentCounts = computed(() =>
  kit.map((instrument) => ({
    ...instrument,
    count: props.events.filter((event) => event.pitch === instrument.pitch).length,
  })),
);
const maximumCount = computed(() =>
  Math.max(1, ...instrumentCounts.value.map((instrument) => instrument.count)),
);
</script>

<template>
  <UModal title="통계" :ui="{ content: 'sm:max-w-md' }">
    <UButton
      label="통계"
      icon="i-lucide-chart-no-axes-combined"
      color="neutral"
      variant="ghost"
      size="xs"
    />
    <template #body>
      <div class="metadata-row">
        <span>전체 타격</span><span class="mono">{{ events.length.toLocaleString() }}</span>
      </div>
      <USeparator class="my-5" />
      <div class="section-label">악기별 타격</div>
      <div class="kit-counts">
        <div
          v-for="instrument in instrumentCounts"
          :key="instrument.pitch"
          class="kit-count"
          :style="{ '--track-color': instrument.color }"
        >
          <span><i />{{ instrument.name }}</span><b class="mono">{{ instrument.count }}</b>
          <div :style="{ width: `${(instrument.count / maximumCount) * 100}%` }" />
        </div>
      </div>
    </template>
  </UModal>
</template>
