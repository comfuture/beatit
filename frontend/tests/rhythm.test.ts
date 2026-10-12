import { describe, expect, it } from "vitest";
import {
  canAddTick,
  eventTick,
  formatTime,
  tickTime,
  validateOptions,
  playbackBar,
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
