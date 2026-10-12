<script setup lang="ts">
import { kit, type DrumEvent, type Grid } from "~/types/studio";
import { eventTick, tickTime, canAddTick } from "~/utils/rhythm";
const props = defineProps<{
  events: DrumEvent[];
  grid: Grid;
  duration: number;
  disabled?: boolean;
  position: number;
}>();
const emit = defineEmits<{
  edit: [events: DrumEvent[]];
  seek: [time: number];
}>();
const bar = ref(1);
const velocity = ref(100);
const follow = ref(false);
const cells = computed(() =>
  Math.round(props.grid.bar_ticks / props.grid.step),
);
const barOffset = computed(() => (bar.value - 1) * props.grid.bar_ticks);
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
const playbackTick = computed(
  () => (((props.position - props.grid.offset) * props.grid.bpm) / 60) * 12,
);
const playbackCell = computed(() =>
  Math.floor((playbackTick.value - barOffset.value) / props.grid.step),
);
watch(
  () => props.position,
  () => {
    if (follow.value)
      bar.value = Math.max(
        1,
        Math.min(
          props.grid.bars,
          Math.floor(playbackTick.value / props.grid.bar_ticks) + 1,
        ),
      );
  },
);
watch(
  () => props.grid,
  () => {
    bar.value = Math.min(bar.value, props.grid.bars);
  },
);
function setBar(value: number) {
  bar.value = Math.max(1, Math.min(props.grid.bars, Math.round(value) || 1));
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
      <USwitch v-model="follow" label="재생 따라가기" size="xs" />
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
            current: cell - 1 === playbackCell,
          }"
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
              current: cell - 1 === playbackCell,
            }"
            :style="{ '--track-color': instrument.color }"
            :aria-label="`${bar}마디 ${cell}번째 격자 ${instrument.name}`"
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
