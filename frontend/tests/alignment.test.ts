import { describe, expect, it } from "vitest";
import { alignmentGrid, canonicalAlignment, resolvedRevision, scoreRevisionTiming, shiftFirstBar } from "../app/utils/alignment";
import { defaultOptions } from "../app/types/studio";
import { playbackBar } from "../app/utils/rhythm";

const options = { ...defaultOptions(), bpm: 120, offset: 0 };
describe("quarter-note first-bar corrections", () => {
  it.each([60, 120, 137.3])("moves half and whole beats using resolved %s BPM", (bpm) => {
    for (const beats of [-1, -0.5, 0.5, 1]) {
      const shifted = shiftFirstBar({ ...options, bpm, offset: 2 }, beats);
      expect(shifted.offset).toBeCloseTo(2 + beats * 60 / bpm, 4);
    }
  });
  it.each([
    ["4/4", 4], ["3/4", 3], ["6/8", 3], ["7/8", 3.5],
  ] as const)("keeps quarter-note beat units and the first hit in %s", (meter, quarters) => {
    for (const bpm of [60, 120, 137.3]) {
      const requested = shiftFirstBar({ ...options, meter, bpm }, 0.5);
      const shifted = canonicalAlignment({ ...options, meter, bpm, offset: requested.offset }, 0);
      expect(shifted.offset).toBeCloseTo((0.5 - quarters) * 60 / bpm, 4);
      expect(shifted.pickupBars).toBe(1);
      expect(shifted.offset).toBeLessThan(0);
      const grid = alignmentGrid({ ...options, meter, bpm, offset: shifted.offset }, 20);
      expect(grid.bar_ticks).toBe(quarters * 12);
      expect(playbackBar(shifted.offset + quarters * 60 / bpm + 1e-6, grid)).toBe(2);
    }
  });
  it("handles repeated moves and an inverse without mutating the input", () => {
    const start = { ...options, offset: -1.5 };
    const next = shiftFirstBar(start, 0.5);
    const twice = shiftFirstBar({ ...start, offset: next.offset }, 0.5);
    expect(twice.offset).toBe(-1);
    expect(shiftFirstBar({ ...start, offset: next.offset }, -0.5).offset).toBe(start.offset);
    expect(start.offset).toBe(-1.5);
  });
  it("keeps inverse draft moves exact while deriving a pickup-safe save without trimming intros", () => {
    const later = shiftFirstBar(options, 0.5);
    expect(later.offset).toBe(0.25);
    expect(canonicalAlignment({ ...options, offset: later.offset }, 0)).toEqual({ offset: -1.75, pickupBars: 1 });
    expect(shiftFirstBar({ ...options, offset: later.offset }, -0.5).offset).toBe(0);
    expect(canonicalAlignment({ ...options, offset: -6 }, 12)).toEqual({ offset: -6, pickupBars: 0 });
  });
  it("supports triplets without redefining half a beat", () => {
    const next = shiftFirstBar({ ...options, grid: "triplet", meter: "6/8" }, -0.5);
    expect(next.offset).toBe(-0.25);
    expect(alignmentGrid({ ...options, grid: "triplet", meter: "6/8", offset: next.offset }, 8).step).toBe(4);
    expect(() => shiftFirstBar({ ...options, grid: "triplet", meter: "7/8" }, 0.5)).toThrow();
  });
  it("rejects unresolved/invalid timing and offset bounds without clamping", () => {
    for (const bpm of [null, NaN, Infinity, 0, 29, 301])
      expect(() => shiftFirstBar({ ...options, bpm }, 0.5)).toThrow();
    expect(() => shiftFirstBar({ ...options, offset: null }, 0.5)).toThrow();
    expect(() => shiftFirstBar({ ...options, offset: -10 }, -0.5)).toThrow(/-10/);
    expect(() => shiftFirstBar({ ...options, offset: 120 }, 0.5)).toThrow(/120/);
    expect(shiftFirstBar({ ...options, offset: -9.75 }, -0.5).offset).toBe(-10);
    expect(shiftFirstBar({ ...options, offset: 119.75 }, 0.5).offset).toBe(120);
  });
  it("materializes legacy auto values against the same saved baseline", () => {
    const result = { options: { ...options, bpm: null, offset: null },
      grid: alignmentGrid({ ...options, bpm: 137.3, offset: -0.23 }, 8) };
    expect(resolvedRevision(result)).toEqual({ ...options, bpm: 137.3, offset: -0.23 });
    expect(result.options.bpm).toBeNull();
    expect(resolvedRevision({ ...result, options: { ...options, bpm: NaN } }).bpm).toBeNaN();
  });
  it.each([
    { bpm: 120, offset: null }, { bpm: null, offset: 0.25 }, { bpm: null, offset: null },
  ])("keeps automatic timing %j for analysis instead of explicit preservation", (automatic) => {
    const draft = { ...options, ...automatic, tom_refinement: false };
    const saved = scoreRevisionTiming(draft, true, 0);
    expect(saved.preserveEvents).toBe(false);
    expect(saved.options).toMatchObject({ ...automatic, tom_refinement: true });
    expect(draft).toEqual({ ...options, ...automatic, tom_refinement: false });
  });
  it("canonicalizes only explicit alignment drafts and retains their refinement option", () => {
    const explicit = { ...options, offset: 0.25, tom_refinement: false };
    expect(scoreRevisionTiming(explicit, true, 0)).toEqual({ preserveEvents: true,
      options: { ...explicit, offset: -1.75 } });
    expect(scoreRevisionTiming(explicit, false, 0)).toEqual({ preserveEvents: false,
      options: { ...explicit, tom_refinement: true } });
  });
  it("includes the terminal quantized bar when recomputing draft review hints", () => {
    const events = [{ time: 1.99, pitch: 35 as const, strength: 0.8 }];
    expect(alignmentGrid(options, 2, events).bars).toBe(2);
  });
});
