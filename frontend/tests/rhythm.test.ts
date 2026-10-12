import { describe, expect, it } from "vitest";
import {
  canAddTick,
  eventTick,
  formatTime,
  tickTime,
  validateOptions,
  playbackBar,
  playbackBeat,
} from "../app/utils/rhythm";
import type { Grid } from "../app/types/studio";
const grid: Grid = {
  bpm: 120,
  offset: -0.5,
  meter: "4/4",
  bars: 3,
  bar_ticks: 48,
  step: 3,
  mean_quantization_error_ms: 0,
};
describe("editor timeline boundaries", () => {
  it("lands on the first beat after exact-bar seeks with fractional tempo", () => {
    const saved = { ...grid, bpm: 131.014, offset: 0.393, bars: 150 };
    for (let bar = 1; bar <= saved.bars; bar++) {
      for (let beat = 0; beat < 4; beat++) {
        const time = tickTime((bar - 1) * saved.bar_ticks + beat * 12, saved);
        expect(playbackBar(time, saved)).toBe(bar);
        expect(playbackBeat(time, saved, bar)).toBe(beat);
      }
    }
  });
  it.each([3, 6, 4])("holds a whole beat regardless of grid step %s", (step) => {
    const saved = { ...grid, offset: 0, step };
    for (const time of [0, 0.125, 0.25, 0.375, 0.499])
      expect(playbackBeat(time, saved, 1)).toBe(0);
    expect(playbackBeat(0.5, saved, 1)).toBe(1);
    expect(playbackBeat(1.999, saved, 1)).toBe(3);
    expect(playbackBeat(2, saved, 1)).toBeNull();
    expect(playbackBeat(2, saved, 2)).toBe(0);
  });
  it("does not highlight another bar or the lead-in, and keeps partial beats", () => {
    expect(playbackBeat(0, { ...grid, offset: 0.5 }, 1)).toBeNull();
    expect(playbackBeat(0, grid, 1)).toBe(1);
    expect(playbackBeat(0, grid, 2)).toBeNull();
    const odd = { ...grid, offset: 0, meter: "7/8", bar_ticks: 42 };
    expect(playbackBeat(1.5, odd, 1)).toBe(3);
    expect(playbackBeat(1.749, odd, 1)).toBe(3);
    expect(playbackBeat(1.75, odd, 1)).toBeNull();
    expect(playbackBeat(1.75, odd, 2)).toBe(0);
  });
  it("shares playback bars across views including offsets and odd meters", () => {
    expect(playbackBar(0, { ...grid, offset: 1 })).toBe(1);
    expect(playbackBar(1.49, grid)).toBe(1);
    expect(playbackBar(1.5, grid)).toBe(2);
    expect(playbackBar(100, grid)).toBe(3);
    expect(playbackBar(1.75, { ...grid, offset: 0, meter: '7/8', bar_ticks: 42 })).toBe(2);
  });
  it("does not create negative events in pickup bars or beyond audio", () => {
    expect(canAddTick(0, grid, 4)).toBe(false);
    expect(canAddTick(12, grid, 4)).toBe(true);
    expect(canAddTick(108, grid, 4)).toBe(false);
    expect(tickTime(12, grid)).toBe(0);
  });
  it("matches the server quantization convention at half steps", () => {
    expect(eventTick({ time: 0.0625, pitch: 35, strength: 1 }, grid)).toBe(15);
    expect(eventTick({ time: 0.06249, pitch: 35, strength: 1 }, grid)).toBe(12);
  });
  it("retains triplet and odd-meter grids", () => {
    expect(
      eventTick(
        { time: 0.17, pitch: 38, strength: 1 },
        { ...grid, offset: 0, step: 4 },
      ),
    ).toBe(4);
    expect(
      canAddTick(39, { ...grid, meter: "7/8", bar_ticks: 42, offset: 0 }, 4),
    ).toBe(true);
  });
  it("validates options before enqueueing", () => {
    expect(() =>
      validateOptions({ bpm: NaN, offset: null, meter: "4/4", grid: "16" }),
    ).toThrow();
    expect(() =>
      validateOptions({
        bpm: 120,
        offset: -0.5,
        meter: "7/8",
        grid: "triplet",
      }),
    ).toThrow();
    expect(() =>
      validateOptions({
        bpm: null,
        offset: null,
        meter: "6/8",
        grid: "triplet",
      }),
    ).not.toThrow();
  });
  it("formats safe transport timecodes", () => {
    expect(formatTime(65.25)).toBe("01:05.2");
    expect(formatTime(NaN)).toBe("00:00.0");
  });
});
