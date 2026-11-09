<script setup lang="ts">
import type { Options } from "~/types/studio";
import { canonicalAlignment, shiftFirstBar } from "~/utils/alignment";

const props = defineProps<{
  options: Options;
  savedOffset: number | null;
  firstHit: number | null;
  pending?: boolean;
  disabled?: boolean;
}>();
defineEmits<{ shift: [beats: number]; reset: [] }>();
const format = (value: number) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 4 }).format(value);
const anchor = computed(() => props.options.bpm !== null && props.options.offset !== null &&
  Number.isFinite(props.options.bpm) && Number.isFinite(props.options.offset)
  ? format(props.options.offset * props.options.bpm / 60) : "확인 필요");
const effective = computed(() => {
  try { return canonicalAlignment(props.options, props.firstHit); }
  catch { return null; }
});
const actions = computed(() => [
  { beats: -1, label: "−1박 앞당김" }, { beats: -0.5, label: "−0.5박 앞당김" },
  { beats: 0.5, label: "+0.5박 늦춤" }, { beats: 1, label: "+1박 늦춤" },
].map((action) => {
  let reason = "";
  try {
    const shifted = shiftFirstBar(props.options, action.beats);
    canonicalAlignment({ ...props.options, offset: shifted.offset }, props.firstHit);
  }
  catch (error) {
    reason = (error as Error).message;
    if (reason.includes("-10–120초")) reason = "이 방향으로 더 이동할 수 없습니다.";
  }
  return { ...action, reason };
}));
const reason = computed(() => [...new Set(actions.value.map((action) => action.reason).filter(Boolean))].join(" "));
</script>

<template>
  <section class="first-bar-alignment" aria-label="첫 마디 박자 이동">
    <div class="flex items-center justify-between gap-2">
      <span class="text-[11px] text-default">첫 마디 시작</span>
      <span class="text-[10px] text-muted">{{ options.bpm === null ? 'BPM 확인 필요' : `${format(options.bpm)} BPM` }}</span>
    </div>
    <div class="mt-1.5 flex items-center justify-between gap-2">
      <output class="mono text-sm text-highlighted" aria-label="첫 마디 기준 박">{{ anchor }}{{ anchor === '확인 필요' ? '' : '박' }}</output>
      <UButton
        label="위치 되돌리기"
        color="neutral"
        variant="ghost"
        size="xs"
        :disabled="disabled || savedOffset === null || options.offset === savedOffset"
        @click="$emit('reset')"
      />
    </div>
    <p class="mt-1 text-[10px] leading-relaxed text-muted">1박 = 4분음표 · 6/8·7/8도 동일</p>
    <div class="mt-2 grid grid-cols-2 gap-1.5">
      <UTooltip v-for="action in actions" :key="action.beats" :text="action.reason || action.label">
        <UButton
          :label="action.label"
          color="neutral"
          variant="outline"
          size="xs"
          class="justify-center"
          :disabled="disabled || !!action.reason"
          @click="$emit('shift', action.beats)"
        />
      </UTooltip>
    </div>
    <p v-if="reason" class="mt-2 text-[10px] leading-relaxed text-warning" role="status">{{ reason }}</p>
    <p v-if="effective?.pickupBars && options.bpm" class="mt-2 text-[10px] leading-relaxed text-muted">
      저장 기준 <output class="mono" aria-label="저장할 마디 기준 박">{{ format(effective.offset * options.bpm / 60) }}박</output> ·
      첫 타격을 보존하도록 {{ effective.pickupBars }}마디 앞에 배치합니다.
    </p>
    <p v-else-if="options.offset !== null && options.offset < 0" class="mt-2 text-[10px] leading-relaxed text-muted">
      원음 시작 전 기준입니다. 앞선 타격은 못갖춘마디로 보존합니다.
    </p>
    <p class="mt-2 text-[10px] leading-relaxed text-muted" :role="pending ? 'status' : undefined">
      {{ pending ? '악보 재생성 후 전체 마디·재생·내보내기에 적용됩니다.' : '기준 위치는 원음 시작으로부터의 박 수입니다.' }}
    </p>
  </section>
</template>
