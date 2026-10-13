import { describe, expect, it } from "vitest";
import type { DrumEvent, Grid } from "../app/types/studio";
import { barPlaybackRange, firstPlaybackHit, playbackHits, timePlaybackRange } from "../app/utils/drumPlayback";

const grid: Grid = {
  bpm: 120, offset: 0, meter: "4/4", bars: 4, bar_ticks: 48, step: 3,
  mean_quantization_error_ms: 0,
};
const event = (time: number, pitch: DrumEvent["pitch"], strength = 0.8): DrumEvent => ({ time, pitch, strength });

describe("bar audition and draft notes", () => {
  it("normalizes dragged ranges in both directions and clamps to the audio", () => {
    expect(timePlaybackRange(3, 1, 8)).toEqual({ start: 1, end: 3 });
    expect(timePlaybackRange(-2, 12, 8)).toEqual({ start: 0, end: 8 });
    expect(timePlaybackRange(1.234, 2.789, 8)).toEqual({ start: 1.234, end: 2.789 });
    for (const [a, b, duration] of [[2, 2, 8], [1, 1.001, 8], [10, 12, 8], [0, 1, 0], [NaN, 2, 8], [0, Infinity, 8]])
      expect(timePlaybackRange(a!, b!, duration!)).toBeNull();
  });
  it("bounds a selected bar by its saved grid and audio duration", () => {
    expect(barPlaybackRange(2, grid, 8)).toEqual({ start: 2, end: 4, bar: 2 });
    expect(barPlaybackRange(4, grid, 6.4)).toEqual({ start: 6, end: 6.4, bar: 4 });
    expect(barPlaybackRange(4, grid, 5)).toBeNull();
    expect(barPlaybackRange(1, { ...grid, offset: -0.5 }, 8)).toEqual({ start: 0, end: 1.5, bar: 1 });
    expect(barPlaybackRange(1, { ...grid, offset: 0.5 }, 8)).toEqual({ start: 0.5, end: 2.5, bar: 1 });
  });
  it.each([
    ["3/4", 36, 1.5], ["6/8", 36, 1.5], ["7/8", 42, 1.75],
  ] as const)("uses %s bar length rather than assuming 4/4", (meter, bar_ticks, length) => {
    expect(barPlaybackRange(2, { ...grid, meter, bar_ticks }, 8))
      .toEqual({ start: length, end: length * 2, bar: 2 });
  });
  it("quantizes drafts, retains explicit velocity and merges strongest hits like score MIDI", () => {
    const events = [event(0.01, 35), { ...event(0.12, 38), velocity: 42 },
      event(0.5, 42, 0.5), event(0.51, 46, 0.9), event(0.52, 46, 0.2)];
    expect(playbackHits(events, grid, 8)).toEqual([
      { time: 0, pitch: 35, velocity: 105 },
      { time: 0.125, pitch: 38, velocity: 42 },
      { time: 0.5, pitch: 46, velocity: 116 },
    ]);
  });
  it("omits out-of-audio notes and aligns triplets", () => {
    expect(playbackHits([event(0, 35), event(0.5, 38), event(8, 49)], { ...grid, offset: 0.5 }, 8))
      .toEqual([{ time: 0.5, pitch: 38, velocity: 105 }]);
    expect(playbackHits([event(0.16, 38)], { ...grid, step: 4 }, 8)[0]?.time).toBeCloseTo(1 / 6);
  });
  it("seeks without replaying earlier hits, including empty patterns", () => {
    const notes = playbackHits([event(0, 35), event(0.5, 38), event(2, 49)], grid, 8);
    expect(firstPlaybackHit(notes, 0)).toBe(0);
    expect(firstPlaybackHit(notes, 0.25)).toBe(1);
    expect(firstPlaybackHit(notes, 2)).toBe(2);
    expect(firstPlaybackHit(notes, 3)).toBe(3);
    expect(firstPlaybackHit([], 0)).toBe(0);
  });
});
