import type { DrumEvent, Grid } from "../types/studio";
function playbackTick(position: number, grid: Grid) {
  const tick = (((position - grid.offset) * grid.bpm) / 60) * 12;
  // Seeking to an exact beat can land just below it through float round-off.
  const nearest = Math.round(tick);
  return Math.abs(tick - nearest) < 1e-8 ? nearest : tick;
}
export function playbackBar(position: number, grid: Grid) {
  const tick = playbackTick(position, grid);
  return Math.max(1, Math.min(grid.bars, Math.floor(tick / grid.bar_ticks) + 1));
}
// Beat numbers in the editor follow the quarter-note BPM (12 ticks per beat).
// Keep the last partial beat in odd meters, and hide playback outside this bar.
export function playbackBeat(position: number, grid: Grid, bar: number) {
  const tick = playbackTick(position, grid);
  const relativeTick = tick - (bar - 1) * grid.bar_ticks;
  return relativeTick >= 0 && relativeTick < grid.bar_ticks
    ? Math.floor(relativeTick / 12)
    : null;
}
export function eventTick(event: DrumEvent, grid: Grid) {
  return Math.max(
    0,
    Math.floor(
      ((((event.time - grid.offset) * grid.bpm) / 60) * 12) / grid.step + 0.5,
    ) * grid.step,
  );
}
export function tickTime(tick: number, grid: Grid) {
  return grid.offset + ((tick / 12) * 60) / grid.bpm;
}
export function canAddTick(tick: number, grid: Grid, duration: number) {
  const time = tickTime(tick, grid);
  return Number.isFinite(time) && time >= 0 && time < duration;
}
export function formatTime(seconds: number) {
  const time = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  return `${String(Math.floor(time / 60)).padStart(2, "0")}:${String(Math.floor(time % 60)).padStart(2, "0")}.${Math.floor((time % 1) * 10)}`;
}
export function validateOptions(options: {
  bpm: number | null;
  offset: number | null;
  meter: string;
  grid: string;
}) {
  if (
    options.bpm !== null &&
    (!Number.isFinite(options.bpm) || options.bpm < 30 || options.bpm > 300)
  )
    throw new Error("BPM은 30–300 범위로 입력하세요.");
  if (
    options.offset !== null &&
    (!Number.isFinite(options.offset) ||
      options.offset < -10 ||
      options.offset > 120)
  )
    throw new Error("첫 마디 시작은 -10–120초 범위로 입력하세요.");
  if (options.meter === "7/8" && options.grid === "triplet")
    throw new Error("7/8 박자에는 8분 또는 16분 격자를 선택하세요.");
}
