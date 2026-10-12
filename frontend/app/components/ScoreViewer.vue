<script setup lang="ts">
import type { Job, ScoreMeasure } from "~/types/studio";
import { playbackBar } from "~/utils/rhythm";
const props = defineProps<{
  job: Job;
  url: (name: string, download?: boolean) => string;
  stale?: boolean;
  position: number;
}>();
const page = ref(1);
const zoom = ref(80);
const failed = ref(false);
const follow = ref(true);
const measures = ref<ScoreMeasure[]>([]);
const loadingLayout = ref(false);
const canvas = useTemplateRef("canvas");
const paper = useTemplateRef("paper");
const highlight = useTemplateRef("highlight");
const currentBar = computed(() =>
  props.job.result ? playbackBar(props.position, props.job.result.grid) : 1,
);
const activeMeasure = computed(() => measures.value[currentBar.value - 1]);
const visibleMeasure = computed(() =>
  activeMeasure.value?.page === page.value ? activeMeasure.value : undefined,
);
watch(
  [() => props.job.id, () => props.job.result?.generation],
  async ([id, generation], _, onCleanup) => {
    const controller = new AbortController();
    onCleanup(() => controller.abort());
    measures.value = [];
    page.value = 1;
    failed.value = false;
    if (!generation) return;
    loadingLayout.value = true;
    try {
      const layout = await $fetch<{
        generation: string;
        measures: ScoreMeasure[];
      }>(`/api/jobs/${id}/score-layout`, {
        query: { generation },
        signal: controller.signal,
      });
      if (!controller.signal.aborted && layout.generation === generation)
        measures.value = layout.measures;
    } catch {
      // The score stays readable when its engraver's layout cannot be mapped.
    } finally {
      if (!controller.signal.aborted) loadingLayout.value = false;
    }
  },
  { immediate: true },
);
watch(page, () => {
  failed.value = false;
});
function browsePage(target: number) {
  follow.value = false;
  page.value = target;
  canvas.value?.scrollTo({ top: 0, left: 0 });
}
async function revealMeasure() {
  if (!follow.value || !activeMeasure.value) return;
  page.value = activeMeasure.value.page;
  await nextTick();
  const container = canvas.value;
  const marker = highlight.value;
  if (!follow.value || !container || !marker || !paper.value?.naturalWidth)
    return;
  const frame = container.getBoundingClientRect();
  const rect = marker.getBoundingClientRect();
  const top = rect.top - frame.top;
  const left = rect.left - frame.left;
  // Scroll only the score panel, and only when the active bar leaves its viewport.
  if (top < 16 || top + rect.height > container.clientHeight - 16)
    container.scrollTop +=
      top - Math.max(16, (container.clientHeight - rect.height) * 0.25);
  if (left < 16 || left + rect.width > container.clientWidth - 16)
    container.scrollLeft += left - 16;
}
watch([activeMeasure, follow, zoom], revealMeasure, { flush: "post" });
let observer: ResizeObserver | undefined;
onMounted(() => {
  observer = new ResizeObserver(() => {
    void revealMeasure();
  });
  if (canvas.value) observer.observe(canvas.value);
});
onBeforeUnmount(() => observer?.disconnect());
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
          @click="browsePage(page - 1)"
        /><span class="mono"
          >{{ page }} / {{ job.result?.pages.length || 0 }}</span
        ><UButton
          icon="i-lucide-chevron-right"
          aria-label="다음 페이지"
          color="neutral"
          variant="ghost"
          :disabled="page >= (job.result?.pages.length || 0)"
          @click="browsePage(page + 1)"
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
    <div class="score-follow-controls">
      <USwitch
        v-model="follow"
        label="재생 따라가기"
        aria-label="악보 재생 따라가기"
        size="xs"
        :disabled="!measures.length"
      />
      <span v-if="measures.length" class="mono">
        {{ currentBar }} 마디 / {{ job.result?.grid.bars }}
      </span>
      <span v-else>
        {{ loadingLayout ? '마디 위치 불러오는 중' : '이 악보는 자동 따라가기를 지원하지 않습니다.' }}
      </span>
    </div>
    <div v-if="stale" class="score-stale">
      <UIcon name="i-lucide-info" /> 저장된 악보입니다. 편집 내용은 악보 재생성
      후 반영됩니다.
    </div>
    <div ref="canvas" class="score-canvas">
      <p v-if="failed" role="alert" class="text-error p-4">
        악보 이미지를 불러오지 못했습니다. 다시 시도하거나 MusicXML을
        내려받으세요.
      </p>
      <div
        v-if="job.result?.pages[page - 1]"
        :style="{
          width: zoom === 80 ? 'min(100% - 48px, 860px)' : `${zoom * 8.6}px`,
        }"
        class="score-sheet"
      >
        <img
          ref="paper"
          :key="job.id + job.result.generation + page"
          :src="url(job.result.pages[page - 1]!)"
          :alt="`${job.filename} 드럼 악보 ${page}페이지`"
          class="score-paper"
          @load="revealMeasure"
          @error="failed = true"
        />
        <div
          v-if="visibleMeasure && !failed"
          ref="highlight"
          class="score-measure-highlight"
          role="img"
          :aria-label="`${currentBar}마디 재생 위치`"
          :data-bar="currentBar"
          :style="{
            left: `${visibleMeasure.x * 100}%`,
            top: `${visibleMeasure.y * 100}%`,
            width: `${visibleMeasure.width * 100}%`,
            height: `${visibleMeasure.height * 100}%`,
          }"
        />
      </div>
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
