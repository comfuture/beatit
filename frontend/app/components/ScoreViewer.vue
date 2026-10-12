<script setup lang="ts">
import type { Job } from "~/types/studio";
const props = defineProps<{
  job: Job;
  url: (name: string, download?: boolean) => string;
  stale?: boolean;
}>();
const page = ref(1);
const zoom = ref(80);
const failed = ref(false);
watch(
  () => props.job.result?.generation,
  () => {
    page.value = 1;
    failed.value = false;
  },
);
watch(page, () => {
  failed.value = false;
});
async function printScore() {
  await nextTick();
  const images = Array.from(
    document.querySelectorAll<HTMLImageElement>(".print-score img"),
  );
  try {
    await Promise.all(images.map((image) => image.decode()));
    window.print();
  } catch {
    failed.value = true;
  }
}
</script>

<template>
  <section class="score-viewer">
    <div class="score-controls">
      <div class="flex items-center gap-2">
        <UButton
          icon="i-lucide-chevron-left"
          aria-label="이전 페이지"
          color="neutral"
          variant="ghost"
          :disabled="page <= 1"
          @click="page--"
        /><span class="mono"
          >{{ page }} / {{ job.result?.pages.length || 0 }}</span
        ><UButton
          icon="i-lucide-chevron-right"
          aria-label="다음 페이지"
          color="neutral"
          variant="ghost"
          :disabled="page >= (job.result?.pages.length || 0)"
          @click="page++"
        />
      </div>
      <div class="flex items-center gap-2">
        <USelect
          v-model="zoom"
          :items="[
            { label: '화면에 맞춤', value: 80 },
            { label: '100%', value: 100 },
            { label: '125%', value: 125 },
            { label: '150%', value: 150 },
          ]"
          aria-label="악보 확대"
          size="xs"
          class="w-32"
        /><UButton
          icon="i-lucide-printer"
          aria-label="악보 인쇄"
          color="neutral"
          variant="ghost"
          @click="printScore"
        />
      </div>
    </div>
    <div v-if="stale" class="score-stale">
      <UIcon name="i-lucide-info" /> 저장된 악보입니다. 편집 내용은 악보 재생성
      후 반영됩니다.
    </div>
    <div class="score-canvas">
      <p v-if="failed" role="alert" class="text-error p-4">
        악보 이미지를 불러오지 못했습니다. 다시 시도하거나 MusicXML을
        내려받으세요.
      </p>
      <img
        v-if="job.result?.pages[page - 1]"
        :key="job.result.generation + page"
        :src="url(job.result.pages[page - 1]!)"
        :alt="`${job.filename} 드럼 악보 ${page}페이지`"
        :style="{
          width: zoom === 80 ? 'min(100% - 48px, 860px)' : `${zoom * 8.6}px`,
        }"
        class="score-paper"
        @error="failed = true"
      />
      <p v-else class="p-8 text-muted">표시할 악보 페이지가 없습니다.</p>
    </div>
    <div class="print-score">
      <img
        v-for="(file, index) in job.result?.pages"
        :key="file"
        :src="url(file)"
        :alt="`악보 ${index + 1}페이지`"
      />
    </div>
  </section>
</template>
