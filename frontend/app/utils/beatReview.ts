import type { BeatReview, BeatReviewItem, DrumEvent, Grid, ReviewReason } from "../types/studio";
import { eventTick, tickTime } from "./rhythm";

export const reviewReasons: Record<ReviewReason, string> = {
  "too-many-hands": "손으로 치는 파트가 동시에 3개 이상입니다. 불필요한 타격을 확인하세요.",
  "hat-cymbal-sequence": "하이햇과 심벌이 8분음표 이내로 이어집니다. 오픈 하이햇을 잘못 인식했을 수 있습니다.",
  "conflicting-hats": "닫힌 하이햇과 오픈 하이햇이 동시에 있습니다. 하이햇 종류를 확인하세요.",
  "duplicate-hit": "같은 악기의 여러 타격이 한 격자에 겹쳤습니다. 중복 검출이나 격자 설정을 확인하세요.",
  "off-grid": "타격이 격자에서 크게 벗어났습니다. 원음을 듣고 타격 위치·BPM·첫 박을 확인하세요.",
};

export function reviewGridKey(grid: Grid) {
  return JSON.stringify([grid.bpm, grid.offset, grid.meter, grid.bar_ticks, grid.step]);
}

export function beatStart(tick: number, grid: Grid) {
  const bar = Math.floor(tick / grid.bar_ticks) * grid.bar_ticks;
  return bar + Math.floor((tick - bar) / 12) * 12;
}

// These are review hints, never corrections or model-confidence estimates.
export function detectBeatIssues(events: DrumEvent[], grid: Grid): BeatReviewItem[] {
  const groups = new Map<number, DrumEvent[]>();
  const issues = new Map<number, Set<ReviewReason>>();
  const mark = (tick: number, reason: ReviewReason) => {
    const beat = beatStart(tick, grid);
    if (!issues.has(beat)) issues.set(beat, new Set());
    issues.get(beat)!.add(reason);
  };
  const stepSeconds = (grid.step / 12) * 60 / grid.bpm;
  for (const event of events) {
    const tick = eventTick(event, grid);
    if (tick >= grid.bars * grid.bar_ticks) continue;
    if (!groups.has(tick)) groups.set(tick, []);
    groups.get(tick)!.push(event);
    // Require both a perceptible absolute error and proximity to the midpoint.
    if (Math.abs(event.time - tickTime(tick, grid)) > Math.max(0.045, stepSeconds * 0.4))
      mark(tick, "off-grid");
  }
  let previous: { tick: number; pitches: Set<number> } | undefined;
  for (const [tick, hits] of [...groups].sort(([a], [b]) => a - b)) {
    const pitches = new Set(hits.map((event) => event.pitch));
    if ([...pitches].filter((pitch) => pitch !== 35).length >= 3)
      mark(tick, "too-many-hands");
    if (pitches.has(42) && pitches.has(46)) mark(tick, "conflicting-hats");
    if (pitches.size < hits.length) mark(tick, "duplicate-hit");
    if (![42, 46, 49].some((pitch) => pitches.has(pitch as DrumEvent["pitch"]))) continue;
    if (previous && tick - previous.tick <= 6 &&
      !pitches.has(46) && !previous.pitches.has(46) &&
      ((previous.pitches.has(42) && pitches.has(49)) ||
        (previous.pitches.has(49) && pitches.has(42)))) {
      mark(previous.tick, "hat-cymbal-sequence");
      mark(tick, "hat-cymbal-sequence");
    }
    previous = { tick, pitches };
  }
  return [...issues].sort(([a], [b]) => a - b).map(([tick, reasons]) => ({
    tick, reasons: [...reasons].sort(), resolved: false,
  }));
}

export function mergeBeatReview(events: DrumEvent[], grid: Grid, saved?: BeatReview | null): BeatReview {
  const grid_key = reviewGridKey(grid);
  const beats = new Map<number, BeatReviewItem>(
    (saved?.grid_key === grid_key ? saved.beats : [])
      .filter((beat) => beat.tick < grid.bars * grid.bar_ticks)
      .map((beat) => [beat.tick, { ...beat, reasons: [...beat.reasons] }]),
  );
  for (const issue of detectBeatIssues(events, grid)) {
    const prior = beats.get(issue.tick);
    beats.set(issue.tick, prior ? {
      ...prior,
      reasons: [...new Set([...prior.reasons, ...issue.reasons])].sort(),
      resolved: prior.resolved && issue.reasons.every((reason) => prior.reasons.includes(reason)),
    } : issue);
  }
  // Keep fixed-but-unconfirmed issues until the user explicitly resolves them.
  return { grid_key, beats: [...beats.values()].sort((a, b) => a.tick - b.tick) };
}

export function reviewAfterEdit(before: DrumEvent[], after: DrumEvent[], grid: Grid, review: BeatReview) {
  const signatures = (events: DrumEvent[]) => {
    const map = new Map<number, string[]>();
    for (const event of events) {
      const beat = beatStart(eventTick(event, grid), grid);
      if (!map.has(beat)) map.set(beat, []);
      map.get(beat)!.push(JSON.stringify(event));
    }
    return new Map([...map].map(([beat, hits]) => [beat, hits.sort().join("|")]));
  };
  const old = signatures(before);
  const next = signatures(after);
  return mergeBeatReview(after, grid, {
    ...review,
    beats: review.beats.map((beat) => ({
      ...beat,
      resolved: beat.resolved && old.get(beat.tick) === next.get(beat.tick),
    })),
  });
}
