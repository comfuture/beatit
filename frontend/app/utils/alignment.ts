import type { DrumEvent, Grid, Options, Result } from "../types/studio";
import { eventTick, validateOptions } from "./rhythm";

/** Old saved sessions may keep auto options beside their resolved grid. */
export function resolvedRevision(result: Pick<Result, "options" | "grid">): Options {
  return { ...result.options, bpm: result.options.bpm ?? result.grid.bpm,
    offset: result.options.offset ?? result.grid.offset };
}

function resolvedTiming(options: Pick<Options, "bpm" | "offset" | "meter" | "grid">) {
  validateOptions(options);
  if (options.bpm === null || options.offset === null)
    throw new Error("BPM과 첫 마디 기준을 확인한 뒤 이동하세요.");
  const meter = /^(3\/4|4\/4|6\/8|7\/8)$/.test(options.meter)
    ? options.meter.split("/").map(Number) : null;
  if (!meter) throw new Error("지원하는 박자를 선택하세요.");
  return { bpm: options.bpm, offset: options.offset, barBeats: meter[0]! * 4 / meter[1]! };
}

/** A beat always follows quarter-note BPM, also in compound and odd meters. */
export function shiftFirstBar(options: Pick<Options, "bpm" | "offset" | "meter" | "grid">, beats: number) {
  const timing = resolvedTiming(options);
  if (!Number.isFinite(beats)) throw new Error("이동할 박 수를 확인하세요.");
  const offset = Math.round((timing.offset + beats * 60 / timing.bpm) * 10000) / 10000;
  validateOptions({ ...options, offset });
  return { offset: Object.is(offset, -0) ? 0 : offset };
}

/** Preserve an intro/pickup by moving only whole bars before the first source hit. */
export function canonicalAlignment(options: Pick<Options, "bpm" | "offset" | "meter" | "grid">,
                                    firstHit: number | null = null) {
  const timing = resolvedTiming(options);
  const period = 60 / timing.bpm;
  const barSeconds = timing.barBeats * period;
  const pickupBars = firstHit !== null && Number.isFinite(firstHit)
    ? Math.max(0, Math.ceil((timing.offset - firstHit) / barSeconds - 1e-9)) : 0;
  const offset = Math.round((timing.offset - pickupBars * barSeconds) * 10000) / 10000;
  validateOptions({ ...options, offset });
  return { offset: Object.is(offset, -0) ? 0 : offset, pickupBars };
}

/** Recheck review hints for the draft without replacing the published display grid. */
export function alignmentGrid(options: Options, duration: number, events: DrumEvent[] = []): Grid {
  const timing = resolvedTiming(options);
  const bar_ticks = timing.barBeats * 12;
  const step = ({ "16": 3, "8": 6, triplet: 4 } as Record<string, number>)[options.grid];
  if (!step || bar_ticks % step) throw new Error("박자에 맞는 리듬 격자를 선택하세요.");
  const grid: Grid = { bpm: timing.bpm, offset: timing.offset, meter: options.meter,
    bars: Math.max(1, Math.ceil(Math.max(1, Math.ceil(Math.max(0, duration - timing.offset)
      * timing.bpm / 60 * 12)) / bar_ticks)), bar_ticks, step, mean_quantization_error_ms: 0 };
  for (const event of events) grid.bars = Math.max(grid.bars, Math.floor(eventTick(event, grid) / bar_ticks) + 1);
  return grid;
}
