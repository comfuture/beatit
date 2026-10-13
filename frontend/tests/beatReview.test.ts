import { describe, expect, it } from "vitest";
import type { DrumEvent, Grid } from "../app/types/studio";
import { beatStart, detectBeatIssues, mergeBeatReview, reviewAfterEdit } from "../app/utils/beatReview";
import { tickTime } from "../app/utils/rhythm";

const grid: Grid = {
  bpm: 120, offset: 0, meter: "4/4", bars: 4, bar_ticks: 48, step: 3,
  mean_quantization_error_ms: 0,
};
const hit = (pitch: DrumEvent["pitch"], tick = 0, saved = grid): DrumEvent => ({
  pitch, time: tickTime(tick, saved), strength: 0.8,
});
const reasons = (events: DrumEvent[], saved = grid) => detectBeatIssues(events, saved).flatMap((beat) => beat.reasons);

describe("explainable transcription review", () => {
  it("flags three simultaneous hand parts including toms, excluding the kick", () => {
    expect(reasons([hit(38), hit(42), hit(49)])).toContain("too-many-hands");
    expect(reasons([hit(38), hit(45), hit(50)])).toContain("too-many-hands");
    expect(reasons([hit(35), hit(38), hit(42)])).toEqual([]);
    // Three distinct hand parts over a beat are playable if not simultaneous.
    expect(reasons([hit(38), hit(45, 3), hit(50, 6)])).toEqual([]);
  });

  it.each([3, 6, 4])("flags nearby hat/cymbal hits in both orders with grid step %s", (step) => {
    const saved = { ...grid, step };
    for (const pair of [[42, 49], [49, 42]] as const) {
      expect(reasons([hit(pair[0], 0, saved), hit(pair[1], step, saved)], saved))
        .toEqual(["hat-cymbal-sequence"]);
    }
  });

  it("marks both beats across bar boundaries and ignores wide gaps or recognized open hats", () => {
    const issues = detectBeatIssues([hit(42, 45), hit(49, 48)], grid);
    expect(issues.map((beat) => beat.tick)).toEqual([36, 48]);
    expect(reasons([hit(42), hit(49, 12)])).toEqual([]);
    expect(reasons([hit(46), hit(49, 3)])).toEqual([]);
    expect(reasons([hit(42), hit(46, 3), hit(49, 6)])).toEqual([]);
  });

  it("detects conflicting hat states and same-instrument quantization collisions", () => {
    expect(reasons([hit(42), hit(46)])).toEqual(["conflicting-hats"]);
    expect(reasons([hit(38), { ...hit(38), time: 0.02 }])).toEqual(["duplicate-hit"]);
  });

  it("requires both substantial absolute and relative timing error", () => {
    expect(reasons([{ ...hit(38), time: 0.06 }])).toEqual(["off-grid"]);
    expect(reasons([{ ...hit(38), time: 0.03 }])).toEqual([]);
    // Close to the midpoint but too small in milliseconds to warrant a warning.
    expect(reasons([{ ...hit(38), time: 0.03 }], { ...grid, bpm: 240 })).toEqual([]);
    // Quiet dynamics are not classification confidence.
    expect(reasons([{ ...hit(38), strength: 0.01 }])).toEqual([]);
  });

  it("uses each bar's beat boundaries for odd meters and pickup offsets", () => {
    const odd = { ...grid, meter: "7/8", bar_ticks: 42, offset: -0.5 };
    expect(beatStart(39, odd)).toBe(36);
    expect(beatStart(42, odd)).toBe(42);
    expect(detectBeatIssues([hit(38, 42, odd), hit(42, 42, odd), hit(49, 42, odd)], odd)[0]?.tick).toBe(42);
    expect(detectBeatIssues([hit(38, 192), hit(42, 192), hit(49, 192)], grid)).toEqual([]);
  });

  it("ignores pre-score hits but retains the quantizer's exact half-step boundary", () => {
    const saved = { ...grid, offset: 1 };
    const cutoff = saved.offset - saved.step / 24 * 60 / saved.bpm;
    const dropped = [38, 38, 42, 46, 49].map((pitch) => ({
      ...hit(pitch as DrumEvent["pitch"]), time: cutoff - 0.001,
    }));
    // Excluded hits must not create duplicates, hand/hat conflicts, timing
    // warnings, or a hat/cymbal sequence with the first actual score note.
    expect(detectBeatIssues([...dropped, hit(49, 3, saved)], saved)).toEqual([]);
    expect(reasons([{ ...hit(42), time: cutoff - 0.001 }, hit(49, 3, saved)], saved)).toEqual([]);
    expect(reasons([{ ...hit(38), time: cutoff }], saved)).toEqual(["off-grid"]);
    expect(reasons([hit(38, 0, saved), hit(42, 0, saved), hit(49, 0, saved)], saved))
      .toEqual(["too-many-hands"]);
  });

  it("does not reopen an accepted beat when only dropped pre-score hits change", () => {
    const saved = { ...grid, offset: 1 };
    const scored = [hit(38, 0, saved), hit(42, 0, saved), hit(49, 0, saved)];
    const review = mergeBeatReview(scored, saved);
    review.beats[0]!.resolved = true;
    expect(reviewAfterEdit([...scored, hit(38)], scored, saved, review)).toEqual(review);
  });

  it("retains corrected issues until explicit confirmation and keeps accepted notes unchanged", () => {
    const events = [hit(38), hit(42), hit(49)];
    const review = mergeBeatReview(events, grid);
    const corrected = reviewAfterEdit(events, [hit(38), hit(42)], grid, review);
    expect(corrected.beats[0]?.resolved).toBe(false);
    expect(corrected.beats[0]?.reasons).toContain("too-many-hands");
    review.beats[0]!.resolved = true;
    expect(mergeBeatReview(events, grid, review)).toEqual(review);
    expect(events).toEqual([hit(38), hit(42), hit(49)]);
  });

  it("reopens only edited accepted beats and does not reuse acceptance after a grid change", () => {
    const events = [hit(38), hit(42), hit(49), hit(35, 12)];
    const review = mergeBeatReview(events, grid);
    review.beats[0]!.resolved = true;
    expect(reviewAfterEdit(events, [...events, hit(35, 15)], grid, review).beats[0]?.resolved).toBe(true);
    expect(reviewAfterEdit(events, events.slice(1), grid, review).beats[0]?.resolved).toBe(false);
    const changed = mergeBeatReview(events, { ...grid, step: 6 }, review);
    expect(changed.beats[0]?.resolved).toBe(false);
    expect(changed.grid_key).not.toBe(review.grid_key);
  });
});
