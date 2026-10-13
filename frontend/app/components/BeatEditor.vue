<script setup lang="ts">
import { kit, type BeatReview, type DrumEvent, type Grid } from "~/types/studio";
import { beatStart, reviewReasons } from "~/utils/beatReview";
import {
  eventTick,
  tickTime,
  canAddTick,
  playbackBar,
  playbackBeat,
} from "~/utils/rhythm";
const props = defineProps<{
  events: DrumEvent[];
  review: BeatReview;
  grid: Grid;
  duration: number;
  disabled?: boolean;
  playbackEnabled?: boolean;
  position: number;
}>();
const emit = defineEmits<{
  edit: [events: DrumEvent[]];
  seek: [time: number];
  resolve: [tick: number];
  playBar: [bar: number];
}>();
const bar = ref(1);
const velocity = ref(100);
const follow = ref(true);
const cells = computed(() =>
  Math.round(props.grid.bar_ticks / props.grid.step),
);
const barOffset = computed(() => (bar.value - 1) * props.grid.bar_ticks);
const pending = computed(() => props.review.beats.filter((beat) => !beat.resolved));
const pendingTicks = computed(() => new Set(pending.value.map((beat) => beat.tick)));
const barReviews = computed(() => props.review.beats.filter((beat) =>
  beat.tick >= barOffset.value && beat.tick < barOffset.value + props.grid.bar_ticks,
));
function reviewTick(cell: number) {
  return beatStart(barOffset.value + (cell - 1) * props.grid.step, props.grid);
}
function needsReview(cell: number) {
  return pendingTicks.value.has(reviewTick(cell));
}
function nextIssue() {
  const next = pending.value.find((beat) => beat.tick >= barOffset.value + props.grid.bar_ticks)
    || pending.value[0];
  if (!next) return;
  follow.value = false;
  setBar(Math.floor(next.tick / props.grid.bar_ticks) + 1);
  emit("seek", Math.max(0, tickTime(next.tick, props.grid)));
}
const hits = computed(() => {
  const map = new Map<string, DrumEvent>();
  for (const event of props.events) {
    const tick = eventTick(event, props.grid);
    if (
      tick >= barOffset.value &&
      tick < barOffset.value + props.grid.bar_ticks
    )
      map.set(`${event.pitch}:${tick}`, event);
  }
  return map;
});
const activeBeat = computed(() =>
  playbackBeat(props.position, props.grid, bar.value),
);
function revealPosition(time: number) {
  // Explicit navigation reveals the chosen bar without changing playback follow.
  bar.value = playbackBar(time, props.grid);
}
defineExpose({ revealPosition });
watch(
  [() => props.position, follow, () => props.grid],
  () => {
    if (follow.value) revealPosition(props.position);
  },
  { immediate: true },
);
watch(
  () => props.grid,
  () => {
    bar.value = Math.min(bar.value, props.grid.bars);
  },
);
function setBar(value: number) {
  follow.value = false;
  bar.value = Math.max(1, Math.min(props.grid.bars, Math.round(value) || 1));
}
function playBar() {
  follow.value = false;
  emit("playBar", bar.value);
}
function toggle(pitch: DrumEvent["pitch"], cell: number) {
  if (props.disabled) return;
  const tick = barOffset.value + cell * props.grid.step;
  if (hits.value.has(`${pitch}:${tick}`))
    emit(
      "edit",
      props.events.filter(
        (e) => e.pitch !== pitch || eventTick(e, props.grid) !== tick,
      ),
    );
  else if (canAddTick(tick, props.grid, props.duration))
    emit(
      "edit",
      [
        ...props.events,
        {
          pitch,
          time: tickTime(tick, props.grid),
          strength: 0.8,
          velocity: velocity.value,
        },
      ].sort((a, b) => a.time - b.time),
    );
}
function keyboard(event: KeyboardEvent) {
  const target = event.target as HTMLElement;
  if (!target.matches(".beat-cell")) return;
  const buttons = Array.from(
    target
      .closest(".beat-grid")!
      .querySelectorAll<HTMLButtonElement>(".beat-cell"),
  );
  const index = buttons.indexOf(target as HTMLButtonElement);
  const delta: Record<string, number> = {
    ArrowLeft: -1,
    ArrowRight: 1,
    ArrowUp: -cells.value,
    ArrowDown: cells.value,
  };
  if (event.key in delta) {
    event.preventDefault();
    buttons[
      Math.max(0, Math.min(buttons.length - 1, index + delta[event.key]!))
    ]?.focus();
  }
}
</script>

<template>
  <section class="beat-editor" aria-label="드럼 비트 편집기">
    <div class="editor-controls">
      <div class="flex items-center gap-2">
        <span class="toolbar-caption">마디</span
        ><UButton
          icon="i-lucide-chevron-left"
          aria-label="이전 마디"
          color="neutral"
          variant="ghost"
          :disabled="bar <= 1"
          @click="setBar(bar - 1)"
        /><UInput
          :model-value="bar"
          type="number"
          :min="1"
          :max="grid.bars"
          aria-label="편집 마디"
          class="w-16"
          @update:model-value="setBar(Number($event))"
        /><span class="text-muted text-xs mono">/ {{ grid.bars }}</span
        ><UButton
          icon="i-lucide-chevron-right"
          aria-label="다음 마디"
          color="neutral"
          variant="ghost"
          :disabled="bar >= grid.bars"
          @click="setBar(bar + 1)"
        /><UTooltip text="이 마디부터 재생 위치 이동"
          ><UButton
            icon="i-lucide-locate"
            aria-label="현재 마디로 이동"
            color="neutral"
            variant="ghost"
            @click="emit('seek', Math.max(0, tickTime(barOffset, grid)))"
        /></UTooltip>
      </div>
      <div class="editor-playback-controls">
        <UTooltip text="선택한 마디를 재생합니다. 툴바의 구간 반복을 켜면 반복해서 들을 수 있습니다.">
          <UButton
            label="구간재생"
            icon="i-lucide-play"
            :aria-label="`${bar}마디 구간재생`"
            variant="soft"
            size="xs"
            :disabled="disabled || playbackEnabled === false"
            @click="playBar"
          />
        </UTooltip>
        <USwitch v-model="follow" label="재생 따라가기" size="xs" />
      </div>
    </div>
    <div class="review-summary">
      <div>
        <strong>자동 전사는 초안입니다</strong>
        <p>붉게 표시된 박자는 원음을 듣고 직접 확인하세요. 표시가 없는 박자에도 오류가 있을 수 있습니다.</p>
      </div>
      <div class="review-summary-actions">
        <span role="status">확인 필요 {{ pending.length }}박자</span>
        <UButton
          label="다음 의심 박자"
          icon="i-lucide-scan-search"
          color="neutral"
          variant="outline"
          size="xs"
          :disabled="!pending.length"
          @click="nextIssue"
        />
      </div>
    </div>
    <div class="beat-grid-scroll">
      <div
        class="beat-grid"
        :style="{
          gridTemplateColumns: `122px repeat(${cells}, minmax(28px, 1fr))`,
        }"
        @keydown="keyboard"
      >
        <span class="grid-corner">DRUM KIT <small>GM</small></span>
        <span
          v-for="cell in cells"
          :key="`beat-${cell}`"
          class="beat-number mono"
          :class="{
            downbeat: ((cell - 1) * grid.step) % 12 === 0,
            current: Math.floor(((cell - 1) * grid.step) / 12) === activeBeat,
            'needs-review': needsReview(cell),
          }"
          :title="needsReview(cell) ? '확인 필요 · 아래 판정 이유를 확인하세요' : undefined"
          >{{
            ((cell - 1) * grid.step) % 12 === 0
              ? ((cell - 1) * grid.step) / 12 + 1
              : "·"
          }}</span
        >
        <template v-for="instrument in kit" :key="instrument.pitch">
          <span
            class="instrument-label"
            :style="{ '--track-color': instrument.color }"
            ><i /><span>{{ instrument.name }}</span
            ><small>{{ instrument.pitch }}</small></span
          >
          <button
            v-for="cell in cells"
            :key="cell"
            class="beat-cell"
            :class="{
              on: hits.has(
                `${instrument.pitch}:${barOffset + (cell - 1) * grid.step}`,
              ),
              downbeat: ((cell - 1) * grid.step) % 12 === 0,
              current: Math.floor(((cell - 1) * grid.step) / 12) === activeBeat,
              'needs-review': needsReview(cell),
            }"
            :style="{ '--track-color': instrument.color }"
            :aria-label="`${bar}마디 ${cell}번째 격자 ${instrument.name}`"
            :aria-describedby="needsReview(cell) ? `beat-review-${reviewTick(cell)}` : undefined"
            :aria-pressed="
              hits.has(
                `${instrument.pitch}:${barOffset + (cell - 1) * grid.step}`,
              )
            "
            :disabled="
              disabled ||
              (!hits.has(
                `${instrument.pitch}:${barOffset + (cell - 1) * grid.step}`,
              ) &&
                !canAddTick(barOffset + (cell - 1) * grid.step, grid, duration))
            "
            @click="toggle(instrument.pitch, cell - 1)"
          >
            <span
              v-if="
                hits.has(
                  `${instrument.pitch}:${barOffset + (cell - 1) * grid.step}`,
                )
              "
              class="hit"
            />
          </button>
        </template>
        <span class="velocity-label">VELOCITY</span>
        <div
          v-for="cell in cells"
          :key="`velocity-${cell}`"
          class="velocity-cell"
          :class="{
            current: Math.floor(((cell - 1) * grid.step) / 12) === activeBeat,
            'needs-review': needsReview(cell),
          }"
        >
          <i
            :style="{
              height: `${Math.max(
                0,
                ...kit.map((k) => {
                  const hit = hits.get(
                    `${k.pitch}:${barOffset + (cell - 1) * grid.step}`,
                  );
                  return hit
                    ? ((hit.velocity || Math.round(hit.strength * 110 + 17)) /
                        127) *
                        100
                    : 0;
                }),
              )}%`,
            }"
          />
        </div>
      </div>
    </div>
    <div v-if="barReviews.length" class="beat-reviews" aria-label="이 마디의 의심 박자">
      <div
        v-for="beat in barReviews"
        :id="`beat-review-${beat.tick}`"
        :key="beat.tick"
        class="beat-review"
        :class="{ resolved: beat.resolved }"
        :data-review-tick="beat.tick"
      >
        <div>
          <strong>
            <UIcon :name="beat.resolved ? 'i-lucide-circle-check' : 'i-lucide-triangle-alert'" />
            {{ bar }}마디 {{ Math.floor((beat.tick - barOffset) / 12) + 1 }}박
            · {{ beat.resolved ? '해결됨' : '확인 필요' }}
          </strong>
          <p v-for="reason in beat.reasons" :key="reason">{{ reviewReasons[reason] }}</p>
        </div>
        <UButton
          v-if="!beat.resolved"
          label="해결"
          icon="i-lucide-check"
          color="neutral"
          variant="outline"
          size="xs"
          :disabled="disabled"
          @click="emit('resolve', beat.tick)"
        />
      </div>
      <p class="review-help">직접 수정한 뒤 또는 현재 타격이 맞으면 그대로 ‘해결’을 누르세요. 해결 상태는 악보 재생성 시 함께 저장되며, 실행 취소로 되돌릴 수 있습니다.</p>
    </div>
    <div class="editor-footer">
      <span
        >셀 클릭: 타격 추가 / 삭제
        <span class="hidden xl:inline">· 방향키: 셀 이동</span></span
      >
      <div>
        <span>새 타격 세기</span
        ><USlider
          v-model="velocity"
          :min="1"
          :max="127"
          class="w-20"
          size="xs"
          aria-label="새 타격 세기"
          :disabled="disabled"
        /><b class="mono">{{ velocity }}</b>
      </div>
    </div>
    <p class="editor-note">
      저장된 악보 격자를 기준으로 편집합니다. BPM·격자 변경은 악보 재생성 후
      반영됩니다.
    </p>
  </section>
</template>
