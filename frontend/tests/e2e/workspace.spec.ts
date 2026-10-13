import { expect, test, type Page } from "@playwright/test";
import type { BeatReview, DrumEvent, ScoreMetadata } from "../../app/types/studio";
const id = "a".repeat(32);
const settings = {
  bpm: 120,
  offset: 0,
  meter: "4/4",
  grid: "16",
  device: "auto",
  renderer: "auto",
  source: "mix",
  sensitivity: 1,
  tom_refinement: false,
};
const initialEvents = [
  { time: 0, pitch: 35, strength: 0.8, velocity: 100 },
  { time: 0.5, pitch: 38, strength: 0.7, velocity: 90 },
];
function sampleJob() {
  return {
    id,
    filename: "Studio session.wav",
    status: "completed",
    created: 1791763200,
    updated: 1791763200,
    has_result: true,
    progress: { stage: "complete", progress: 1, message: "악보 생성 완료" },
    result: {
      generation: "render-1",
      beat_review: undefined as BeatReview | undefined,
      metadata: undefined as ScoreMetadata | undefined,
      metadata_source_generation: undefined as string | undefined,
      duration: 8,
      options: { ...settings },
      grid: {
        bpm: 120,
        offset: 0,
        meter: "4/4",
        bars: 4,
        bar_ticks: 48,
        step: 3,
        mean_quantization_error_ms: 12,
      },
      counts: { Kick: 1, Snare: 1 },
      event_count: 2,
      pages: ["score-1.svg"],
      pdf: true,
      renderer: "verovio",
      transcription_device: "cpu",
      warnings: ["원음과 비교하여 템포를 확인하세요."],
      stems: ["stem-kick.flac"],
    },
  };
}
async function setup(page: Page, { multiPage = false, layoutUnavailable = false, offset = 0, events = initialEvents as DrumEvent[], audioTone = false } = {}) {
  let job = sampleJob();
  job.result.grid.offset = offset;
  job.result.options.offset = offset;
  if (multiPage) job.result.pages.push("score-2.svg");
  let history = true;
  let saved: any = null;
  let heading: ScoreMetadata | null = null;
  let pendingHeading: ScoreMetadata | null = null;
  let rejectHeading = false;
  let jobEvents = structuredClone(events);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === "/api/system")
      return route.fulfill({
        json: {
          devices: ["cpu"],
          default_device: "cpu",
          ffmpeg: true,
          verovio: true,
          musescore: false,
          max_upload_bytes: 1073741824,
          max_duration_seconds: 1800,
        },
      });
    if (path === "/api/jobs" && method === "GET")
      return route.fulfill({ json: history ? [job] : [] });
    if (path === "/api/jobs" && method === "POST") {
      expect(route.request().postDataBuffer()?.toString()).toContain(
        'filename="input.wav"',
      );
      job.status = "running";
      job.progress = {
        stage: "separating",
        progress: 0.42,
        message: "드럼 분리 중",
      };
      (job as any).result = undefined;
      return route.fulfill({ status: 202, json: job });
    }
    if (path.endsWith("/waveform"))
      return route.fulfill({
        json: {
          duration: 8,
          peaks: Array.from({ length: 100 }, (_, i) => 0.2 + (i % 8) / 12),
        },
      });
    if (path.endsWith("/events.json"))
      return route.fulfill({ json: jobEvents });
    if (path.endsWith("/score-layout"))
      return route.fulfill({ json: {
        generation: job.result.generation,
        measures: layoutUnavailable ? [] : Array.from({ length: 4 }, (_, index) => ({
          bar: index + 1,
          page: multiPage ? Math.floor(index / 2) + 1 : 1,
          x: 0.1,
          y: multiPage ? (index % 2 === 0 ? 0.1 : 0.75) : 0.1 + index * 0.2,
          width: 0.8,
          height: 0.08,
        })),
      }});
    if (path.endsWith(".svg"))
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000"><rect width="800" height="1000" fill="white"/><text x="80" y="80">Drum score</text></svg>',
      });
    if (path.endsWith(".wav") || path.endsWith(".flac")) {
      const wav = Buffer.alloc(44 + 8000 * 8 * 2);
      wav.write("RIFF");
      wav.writeUInt32LE(wav.length - 8, 4);
      wav.write("WAVEfmt ", 8);
      wav.writeUInt32LE(16, 16);
      wav.writeUInt16LE(1, 20);
      wav.writeUInt16LE(1, 22);
      wav.writeUInt32LE(8000, 24);
      wav.writeUInt32LE(16000, 28);
      wav.writeUInt16LE(2, 32);
      wav.writeUInt16LE(16, 34);
      wav.write("data", 36);
      wav.writeUInt32LE(wav.length - 44, 40);
      if (audioTone) {
        for (let sample = 0; sample < 8000 * 8; sample++)
          wav.writeInt16LE(Math.round(Math.sin(sample / 8000 * Math.PI * 2 * 220) * 4000), 44 + sample * 2);
      }
      const range = route.request().headers().range?.match(/^bytes=(\d+)-(\d*)$/);
      if (range) {
        const start = Number(range[1]);
        const end = Math.min(Number(range[2] || wav.length - 1), wav.length - 1);
        return route.fulfill({
          status: 206,
          contentType: "audio/wav",
          headers: {
            "accept-ranges": "bytes",
            "content-range": `bytes ${start}-${end}/${wav.length}`,
          },
          body: wav.subarray(start, end + 1),
        });
      }
      return route.fulfill({
        contentType: "audio/wav",
        headers: { "accept-ranges": "bytes" },
        body: wav,
      });
    }
    if (path.endsWith("/score")) {
      saved = route.request().postDataJSON();
      if (saved.events) jobEvents = saved.events;
      job.result.options = saved.options;
      job.result.metadata = saved.metadata;
      job.result.beat_review = saved.beat_review;
      job.result.metadata_source_generation = undefined;
      job.result.generation += "-revised";
      job.result.grid.bpm = saved.options.bpm;
      return route.fulfill({ status: 202, json: job });
    }
    if (path.endsWith("/score-metadata")) {
      heading = route.request().postDataJSON();
      if (rejectHeading) return route.fulfill({ status: 500, json: { detail: "악보 정보 적용 실패" } });
      pendingHeading = heading;
      job.status = "queued";
      return route.fulfill({ status: 202, json: job });
    }
    if (path.endsWith("/cancel")) {
      job.status = "cancelled";
      return route.fulfill({ json: job });
    }
    if (path.endsWith("/retry")) {
      job.status = "running";
      return route.fulfill({ status: 202, json: job });
    }
    if (method === "DELETE") {
      history = false;
      return route.fulfill({ status: 204 });
    }
    if (pendingHeading) {
      job.result.metadata = pendingHeading;
      job.result.metadata_source_generation = job.result.generation;
      job.result.generation += "-metadata";
      job.status = "completed";
      pendingHeading = null;
    }
    return route.fulfill({ json: job });
  });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "새 세션", exact: true }),
  ).toBeVisible();
  return {
    errors,
    getSaved: () => saved,
    getHeading: () => heading,
    rejectHeading: (value: boolean) => { rejectHeading = value; },
    fail: () => {
      job.status = "failed";
      (job as any).error = "모델 실행 실패";
    },
  };
}
async function openSession(page: Page) {
  await page.getByRole("button", { name: /Studio session/ }).click();
  await expect(
    page.getByRole("region", { name: "드럼 비트 편집기" }),
  ).toBeVisible();
}

test("apply score headings refreshes the preview and exports while preserving beat drafts", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const form = page.getByRole("form", { name: "악보 정보" });
  await expect(form.getByLabel("악보 제목")).toHaveValue("Studio session");
  await expect(form.getByLabel("저자")).toHaveValue("Automatic drum transcription");
  const cell = page.getByRole("button", { name: "1마디 2번째 격자 Kick", exact: true });
  await cell.click();
  await page.getByRole("spinbutton", { name: "템포 BPM" }).fill("126");
  await form.getByLabel("악보 제목").fill("새 악보 & Live");
  await form.getByLabel("저자").fill("홍길동");
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await expect(page.locator(".score-paper")).toHaveAttribute("src", /v=render-1$/);
  await form.getByRole("button", { name: "적용", exact: true }).click();
  await expect(page.locator(".score-paper")).toHaveAttribute("src", /v=render-1-metadata$/);
  expect(state.getHeading()).toEqual({ title: "새 악보 & Live", author: "홍길동" });
  await expect(form.getByRole("button", { name: "적용", exact: true })).toBeDisabled();
  await expect(page.getByRole("spinbutton", { name: "템포 BPM" })).toHaveValue("126");
  await page.getByRole("button", { name: "내보내기" }).click();
  await expect(page.getByRole("menuitem", { name: "PDF 악보" })).toHaveAttribute("href", /v=render-1-metadata&download=true$/);
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "비트 편집" }).click();
  await expect(cell).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "실행 취소", exact: true }).click();
  await expect(cell).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("button", { name: "다시 실행", exact: true }).click();
  await page.getByRole("button", { name: "악보 재생성", exact: true }).click();
  await expect.poll(() => state.getSaved()?.metadata).toEqual(state.getHeading());
  expect(state.getSaved().events).toHaveLength(3);
  await expect(page.getByRole("button", { name: "악보 재생성", exact: true })).toBeDisabled();
  await page.reload();
  await expect(form.getByLabel("악보 제목")).toHaveValue("새 악보 & Live");
  await expect(page.getByRole("button", { name: /Studio session/ })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("heading drafts guard navigation and survive a failed apply", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  await page.getByLabel("악보 제목").fill("Draft title");
  await page.getByRole("button", { name: "새 세션", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "편집 내용을 버릴까요?" })).toBeVisible();
  await page.getByRole("button", { name: "편집 계속" }).click();
  state.rejectHeading(true);
  await page.getByRole("button", { name: "적용", exact: true }).click();
  await expect(page.getByText("악보 정보 적용 실패", { exact: true })).toBeVisible();
  await expect(page.getByLabel("악보 제목")).toHaveValue("Draft title");
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await expect(page.locator(".score-paper")).toHaveAttribute("src", /v=render-1$/);
  state.rejectHeading(false);
  await page.getByRole("button", { name: "적용", exact: true }).click();
  await expect(page.locator(".score-paper")).toHaveAttribute("src", /v=render-1-metadata$/);
  expect(state.errors).toEqual([]);
});

test("compact session properties can remove the author credit", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "속성 패널", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "세션 속성" });
  await dialog.getByLabel("저자").fill("");
  await dialog.getByRole("button", { name: "적용", exact: true }).click();
  await expect.poll(() => state.getHeading()).toEqual({ title: "Studio session", author: "" });
  await expect(dialog.getByRole("button", { name: "악보 재생성", exact: true })).toBeDisabled();
  await expect(dialog.getByLabel("저자")).toHaveValue("");
  expect(state.errors).toEqual([]);
});

test("edit, undo, redo, save, score and export use session data", async ({
  page,
}) => {
  const state = await setup(page);
  await openSession(page);
  const cell = page.getByRole("button", {
    name: "1마디 2번째 격자 Kick",
    exact: true,
  });
  await cell.click();
  await expect(cell).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "실행 취소", exact: true }).click();
  await expect(cell).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("button", { name: "다시 실행", exact: true }).click();
  await expect(cell).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("spinbutton", { name: "템포 BPM" }).fill("126");
  await page.getByRole("button", { name: "악보 재생성", exact: true }).click();
  await expect.poll(() => state.getSaved()?.events?.length).toBe(3);
  expect(state.getSaved().options.bpm).toBe(126);
  await expect(
    page.getByRole("button", { name: "악보 재생성", exact: true }),
  ).toBeDisabled();
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await expect(page.locator(".score-paper")).toBeVisible();
  await page.getByRole("button", { name: "내보내기" }).click();
  await expect(
    page.getByRole("menuitem", { name: "MusicXML" }),
  ).toHaveAttribute(
    "href",
    new RegExp(`/api/jobs/${id}/files/score.musicxml.*download=true`),
  );
  expect(state.errors).toEqual([]);
});

test("all exports start native downloads without navigation or losing editor state", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const originalUrl = page.url();
  const historyLength = await page.evaluate(() => history.length);
  const downloads: string[] = [];
  page.on("download", download => downloads.push(download.suggestedFilename()));
  const edited = page.getByRole("button", { name: "1마디 2번째 격자 Kick", exact: true });
  await edited.click();
  for (const [label, filename] of [
    ["전체 파일 (.zip)", "score-bundle.zip"],
    ["MusicXML", "score.musicxml"],
    ["MIDI · 악보 격자", "score.mid"],
    ["MIDI · 원시 타격", "performance.mid"],
    ["PDF 악보", "score.pdf"],
    ["타격 데이터 (.json)", "events.json"],
    ["분석 데이터 (.json)", "analysis.json"],
    ["1페이지", "score-1.svg"],
  ] as const) {
    await page.getByRole("button", { name: "내보내기", exact: true }).click();
    if (filename.endsWith(".svg"))
      await page.getByRole("menuitem", { name: "SVG 페이지", exact: true }).hover();
    const downloadStarted = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: label, exact: true }).click();
    const download = await downloadStarted;
    expect(download.suggestedFilename()).toBe(filename);
    expect(new URL(download.url()).pathname).toBe(`/api/jobs/${id}/files/${filename}`);
    // Native downloads bypass page API mocks. Check initiation here; actual
    // file delivery is exercised separately against the FastAPI artifact server.
    await download.cancel();
    await expect(page).toHaveURL(originalUrl);
    expect(await page.evaluate(() => history.length)).toBe(historyLength);
    await expect(edited).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("button", { name: "실행 취소", exact: true })).toBeEnabled();
  }
  expect(page.context().pages()).toHaveLength(1);
  await page.getByRole("button", { name: "실행 취소", exact: true }).click();
  await page.reload();
  await expect(page).toHaveURL(originalUrl);
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
  expect(downloads).toHaveLength(8);
  expect(state.errors).toEqual([]);
});

test("draft guard and reversible navigation preserve unsaved work", async ({
  page,
}) => {
  await setup(page);
  await openSession(page);
  await page
    .getByRole("button", { name: "1마디 2번째 격자 Kick", exact: true })
    .click();
  await page.getByRole("button", { name: "새 세션", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "편집 내용을 버릴까요?" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "편집 계속" }).click();
  await expect(
    page.getByRole("button", { name: "1마디 2번째 격자 Kick", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "새 세션", exact: true }).click();
  await page.getByRole("button", { name: "변경 버리고 이동" }).click();
  await expect(
    page.getByRole("heading", { name: "새로운 드럼 세션" }),
  ).toBeVisible();
});

test("upload, progress, cancellation, failure and retry", async ({ page }) => {
  const state = await setup(page);
  await page
    .getByLabel("소스 파일 선택")
    .setInputFiles({
      name: "input.wav",
      mimeType: "audio/wav",
      buffer: Buffer.from("audio"),
    });
  await page.getByRole("button", { name: "분석 시작" }).click();
  await expect(
    page.getByRole("heading", { name: "드럼 트랙을 분석하고 있습니다" }),
  ).toBeVisible();
  await expect(page.getByText("드럼 분리 중", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "취소", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "분석이 취소되었습니다" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "다시 시도" }).click();
  state.fail();
  await expect(
    page.getByRole("heading", { name: "분석을 완료하지 못했습니다" }),
  ).toBeVisible({ timeout: 10000 });
  await expect(page.getByText("모델 실행 실패", { exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("real transport controls seek, play and retain position on source change", async ({
  page,
}) => {
  await setup(page);
  await openSession(page);
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "일시 정지", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("slider", { name: "오디오 재생 위치" })
    .press("ArrowRight");
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  const before = await page
    .locator("audio")
    .evaluate((audio: HTMLAudioElement) => audio.currentTime);
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "분리된 드럼" }).click();
  await expect(page.locator("audio")).toHaveAttribute("src", /drums.wav$/);
  await expect
    .poll(() =>
      page
        .locator("audio")
        .evaluate((audio: HTMLAudioElement) => audio.currentTime),
    )
    .toBeGreaterThanOrEqual(before - 0.1);
});

test("resizable panels and compact layout remain usable", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const before = await page.locator(".inspector").boundingBox();
  const handle = page.locator(".workspace-splitter > .studio-divider");
  const box = await handle.boundingBox();
  if (!box) throw new Error("Splitter handle missing");
  await page.mouse.move(box.x, box.y + 150);
  await page.mouse.down();
  await page.mouse.move(box.x - 90, box.y + 150);
  await page.mouse.up();
  const after = await page.locator(".inspector").boundingBox();
  expect(after!.width).toBeGreaterThan(before!.width + 40);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "속성 패널" }).click();
  await expect(page.getByRole("dialog", { name: "세션 속성" })).toBeVisible();
  await expect(
    page.getByRole("spinbutton", { name: "템포 BPM" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(state.errors).toEqual([]);
});

test('session deletion requires confirmation and updates history', async ({ page }) => {
  await setup(page);
  await openSession(page);
  await page.getByRole('button', { name: '세션 삭제', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '세션을 삭제할까요?' })).toBeVisible();
  await page.getByRole('button', { name: '취소', exact: true }).click();
  await expect(page.getByRole('region', { name: '드럼 비트 편집기' })).toBeVisible();
  await page.getByRole('button', { name: '세션 삭제', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '세션 삭제', exact: true }).click();
  await expect(page.getByRole('heading', { name: '새로운 드럼 세션' })).toBeVisible();
  await expect(page.getByText('아직 저장된 세션이 없습니다.')).toBeVisible();
});

test("beat editor follows playback, seeking and tab changes by default", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const bar = page.getByRole("spinbutton", { name: "편집 마디" });
  const follow = page.getByRole("switch", { name: "재생 따라가기" });
  const seek = page.getByRole("slider", { name: "오디오 재생 위치" });
  await expect(follow).toBeChecked();
  await seek.press("ArrowRight");
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect(bar).toHaveValue("2");
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();

  await follow.click();
  await seek.press("Home");
  await expect(bar).toHaveValue("1");
  await expect(follow).not.toBeChecked();
  await follow.click();
  await expect(bar).toHaveValue("1");

  await page.getByRole("tab", { name: "악보 보기" }).click();
  await seek.press("ArrowRight");
  await seek.press("ArrowRight");
  await seek.press("ArrowRight");
  await seek.press("ArrowRight");
  await expect(seek).toHaveAttribute("aria-valuenow", "4");
  await page.getByRole("tab", { name: "비트 편집" }).click();
  await expect(bar).toHaveValue("3");
  await page.getByRole("button", { name: "정지", exact: true }).click();
  await expect(bar).toHaveValue("1");
  expect(state.errors).toEqual([]);
});

test("beat highlighting holds all four subdivisions and preserves hit visibility", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const audio = page.locator("audio");
  await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.readyState)).toBeGreaterThanOrEqual(1);
  for (const time of [0, 0.125, 0.25, 0.375]) {
    await audio.evaluate((e: HTMLAudioElement, time) => { e.currentTime = time; }, time);
    await expect(page.locator(".beat-number.current")).toHaveText(["1", "·", "·", "·"]);
    await expect(page.locator(".beat-cell.current")).toHaveCount(32);
    await expect(page.locator(".velocity-cell.current")).toHaveCount(4);
  }
  const kick = page.getByRole("button", { name: "1마디 1번째 격자 Kick", exact: true });
  await expect(kick).toHaveAttribute("aria-pressed", "true");
  await expect(kick.locator(".hit")).toBeVisible();
  await expect.poll(async () => {
    const activeHit = await kick.evaluate(e => getComputedStyle(e).backgroundColor);
    const activeEmpty = await page.getByRole("button", { name: "1마디 2번째 격자 Kick", exact: true })
      .evaluate(e => getComputedStyle(e).backgroundColor);
    const inactive = await page.getByRole("button", { name: "1마디 5번째 격자 Kick", exact: true })
      .evaluate(e => getComputedStyle(e).backgroundColor);
    return activeHit === activeEmpty && activeHit !== inactive;
  }).toBe(true);
  await audio.evaluate((e: HTMLAudioElement) => { e.currentTime = 0.5; });
  await expect(page.locator(".beat-number.current")).toHaveText(["2", "·", "·", "·"]);
  await audio.evaluate((e: HTMLAudioElement) => { e.currentTime = 2; });
  await expect(page.getByRole("spinbutton", { name: "편집 마디" })).toHaveValue("2");
  await expect(page.locator(".beat-number.current")).toHaveText(["1", "·", "·", "·"]);
  expect(state.errors).toEqual([]);
});

test("playback follows the media clock even without native timeupdate events", async ({ page }) => {
  await page.addInitScript(() => {
    document.addEventListener("timeupdate", e => e.stopImmediatePropagation(), true);
  });
  const state = await setup(page);
  await openSession(page);
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect(page.locator(".beat-number.current")).toHaveText(["2", "·", "·", "·"]);
  await expect(page.getByRole("spinbutton", { name: "편집 마디" })).toHaveValue("2");
  await expect(page.locator(".beat-number.current")).toHaveText(["1", "·", "·", "·"]);
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  const paused = await page.locator("audio").evaluate((e: HTMLAudioElement) => e.currentTime);
  await expect(page.getByRole("slider", { name: "오디오 재생 위치" })).toHaveAttribute("aria-valuenow", String(paused));
  await page.getByRole("button", { name: "정지", exact: true }).click();
  await expect(page.locator(".beat-number.current")).toHaveText(["1", "·", "·", "·"]);
  expect(state.errors).toEqual([]);
});

test("score follows playing bars, scrolls and switches pages at the audio position", async ({ page }) => {
  const state = await setup(page, { multiPage: true });
  await openSession(page);
  await page.getByRole("tab", { name: "악보 보기" }).click();
  const follow = page.getByRole("switch", { name: "악보 재생 따라가기" });
  const marker = page.locator(".score-measure-highlight");
  const canvas = page.locator(".score-canvas");
  const seek = page.getByRole("slider", { name: "오디오 재생 위치" });
  await expect(follow).toBeChecked();
  await expect(marker).toHaveAttribute("data-bar", "1");

  await seek.press("ArrowRight");
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect(marker).toHaveAttribute("data-bar", "2");
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  await expect.poll(() => canvas.evaluate(e => e.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => {
    const a = await marker.boundingBox();
    const b = await canvas.boundingBox();
    return !!a && !!b && a.y >= b.y && a.y + a.height <= b.y + b.height;
  }).toBe(true);

  await seek.press("End");
  await expect(page.locator(".score-paper")).toHaveAttribute("alt", /2페이지/);
  await expect(marker).toHaveAttribute("data-bar", "4");
  await page.getByRole("button", { name: "이전 페이지", exact: true }).click();
  await expect(follow).not.toBeChecked();
  await expect(page.locator(".score-paper")).toHaveAttribute("alt", /1페이지/);
  await expect(marker).toHaveCount(0);
  await follow.click();
  await expect(page.locator(".score-paper")).toHaveAttribute("alt", /2페이지/);
  await expect(marker).toHaveAttribute("data-bar", "4");

  await page.getByRole("combobox", { name: "악보 확대" }).click();
  await page.getByRole("option", { name: "150%", exact: true }).click();
  await expect.poll(async () => {
    const a = await marker.boundingBox();
    const b = await canvas.boundingBox();
    return !!a && !!b && a.y >= b.y && a.y + a.height <= b.y + b.height;
  }).toBe(true);
  await page.getByRole("tab", { name: "비트 편집" }).click();
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await expect(marker).toHaveAttribute("data-bar", "4");
  await page.getByRole("button", { name: "정지", exact: true }).click();
  await expect(marker).toHaveAttribute("data-bar", "1");
  await expect(page.locator(".score-paper")).toHaveAttribute("alt", /1페이지/);
  expect(state.errors).toEqual([]);
});

test("score remains readable when measure positions are unavailable", async ({ page }) => {
  const state = await setup(page, { layoutUnavailable: true });
  await openSession(page);
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await expect(page.getByRole("switch", { name: "악보 재생 따라가기" })).toBeDisabled();
  await expect(page.locator(".score-paper")).toBeVisible();
  await expect(page.locator(".score-measure-highlight")).toHaveCount(0);
  await expect(page.locator(".score-measure-target")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

for (const offset of [0.25, -0.5]) {
  test(`score measure clicks seek across zoomed pages and preserve playback (offset ${offset})`, async ({ page }) => {
    const state = await setup(page, { multiPage: true, offset });
    await openSession(page);
    await page.getByRole("tab", { name: "악보 보기" }).click();
    const audio = page.locator("audio");
    const seek = page.getByRole("slider", { name: "오디오 재생 위치" });
    const marker = page.locator(".score-measure-highlight");
    const follow = page.getByRole("switch", { name: "악보 재생 따라가기" });
    await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.readyState)).toBeGreaterThanOrEqual(1);

    await page.getByRole("button", { name: "2마디 시작으로 이동", exact: true }).click();
    await expect(seek).toHaveAttribute("aria-valuenow", String(2 + offset));
    await expect(marker).toHaveAttribute("data-bar", "2");
    expect(await audio.evaluate((e: HTMLAudioElement) => e.paused)).toBe(true);

    await page.getByRole("button", { name: "다음 페이지", exact: true }).click();
    await page.getByRole("combobox", { name: "악보 확대" }).click();
    await page.getByRole("option", { name: "150%", exact: true }).click();
    await page.getByRole("button", { name: "3마디 시작으로 이동", exact: true }).click();
    await expect(seek).toHaveAttribute("aria-valuenow", String(4 + offset));
    await expect(marker).toHaveAttribute("data-bar", "3");
    await expect(follow).not.toBeChecked();
    expect(await audio.evaluate((e: HTMLAudioElement) => e.paused)).toBe(true);

    await page.getByRole("button", { name: "이전 페이지", exact: true }).click();
    const first = page.getByRole("button", { name: "1마디 시작으로 이동", exact: true });
    await first.focus();
    await first.press("Space");
    await expect(seek).toHaveAttribute("aria-valuenow", String(Math.max(0, offset)));
    expect(await audio.evaluate((e: HTMLAudioElement) => e.paused)).toBe(true);
    await page.getByRole("button", { name: "재생", exact: true }).click();
    await page.getByRole("button", { name: "2마디 시작으로 이동", exact: true }).click();
    await expect(marker).toHaveAttribute("data-bar", "2");
    await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.currentTime)).toBeGreaterThan(2 + offset);
    expect(await audio.evaluate((e: HTMLAudioElement) => e.paused)).toBe(false);
    await page.getByRole("button", { name: "일시 정지", exact: true }).click();
    await page.getByRole("tab", { name: "비트 편집" }).click();
    await expect(page.getByRole("spinbutton", { name: "편집 마디" })).toHaveValue("2");
    expect(state.errors).toEqual([]);
  });
}

const suspiciousEvents: DrumEvent[] = [
  { time: 0, pitch: 38, strength: 0.8 },
  { time: 0, pitch: 42, strength: 0.8 },
  { time: 0, pitch: 49, strength: 0.8 },
  { time: 2, pitch: 42, strength: 0.8 },
  { time: 2.125, pitch: 49, strength: 0.8 },
];

test("suspicious beats support unchanged acceptance, undo, save and reload", async ({ page }) => {
  const state = await setup(page, { events: suspiciousEvents });
  await openSession(page);
  const issue = page.locator('[data-review-tick="0"]');
  const cell = page.getByRole("button", { name: "1마디 1번째 격자 Snare", exact: true });
  await expect(page.getByText("자동 전사는 초안입니다", { exact: true })).toBeVisible();
  await expect(issue).toContainText("손으로 치는 파트가 동시에 3개 이상");
  await expect(cell).toHaveClass(/needs-review/);
  await expect(cell).toHaveAttribute("aria-describedby", "beat-review-0");
  await expect(page.locator(".beat-cell.needs-review")).toHaveCount(32);
  await expect(page.locator(".beat-number.needs-review")).toHaveCount(4);
  await expect(page.locator(".velocity-cell.needs-review")).toHaveCount(4);
  // Playback and warning layers remain simultaneously visible.
  expect(await cell.evaluate((element) => getComputedStyle(element).backgroundImage)).toContain("linear-gradient");
  await page.screenshot({ path: "test-results/beat-review-desktop.png", fullPage: true });
  await issue.getByRole("button", { name: "해결", exact: true }).click();
  await expect(issue).toContainText("해결됨");
  await expect(cell).not.toHaveClass(/needs-review/);
  await expect(cell).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "실행 취소", exact: true }).click();
  await expect(cell).toHaveClass(/needs-review/);
  await page.getByRole("button", { name: "다시 실행", exact: true }).click();
  await expect(issue).toContainText("해결됨");
  await page.getByRole("button", { name: "악보 재생성", exact: true }).click();
  await expect.poll(() => state.getSaved()?.beat_review.beats[0].resolved).toBe(true);
  expect(state.getSaved().events).toBeNull();
  await expect(page.getByRole("button", { name: "악보 재생성", exact: true })).toBeDisabled();
  await page.reload();
  await expect(issue).toContainText("해결됨");
  await expect(cell).not.toHaveClass(/needs-review/);
  await cell.click();
  await expect(issue).toContainText("확인 필요");
  await page.getByRole("button", { name: "실행 취소", exact: true }).click();
  await expect(issue).toContainText("해결됨");
  await page.getByRole("button", { name: "다음 의심 박자", exact: true }).click();
  await expect(page.getByRole("spinbutton", { name: "편집 마디" })).toHaveValue("2");
  await expect(page.locator('[data-review-tick="48"]')).toContainText("오픈 하이햇을 잘못 인식");
  expect(state.errors).toEqual([]);
});

test("fixed beats remain pending until resolved, including after regeneration", async ({ page }) => {
  const state = await setup(page, { events: suspiciousEvents });
  await openSession(page);
  const issue = page.locator('[data-review-tick="0"]');
  await page.getByRole("button", { name: "1마디 1번째 격자 Cymbal", exact: true }).click();
  await expect(issue).toContainText("확인 필요");
  await page.getByRole("button", { name: "악보 재생성", exact: true }).click();
  await expect.poll(() => state.getSaved()?.events?.length).toBe(4);
  await expect(issue).toContainText("확인 필요");
  await issue.getByRole("button", { name: "해결", exact: true }).click();
  await expect(issue).toContainText("해결됨");
  await page.getByRole("tab", { name: "악보 보기" }).click();
  await page.getByRole("tab", { name: "비트 편집" }).click();
  await expect(issue).toContainText("해결됨");
  await page.getByRole("button", { name: "악보 재생성", exact: true }).click();
  await expect.poll(() => state.getSaved()?.beat_review.beats[0].resolved).toBe(true);
  await expect(page.getByRole("button", { name: "악보 재생성", exact: true })).toBeDisabled();
  await page.reload();
  await expect(issue).toContainText("해결됨");
  expect(state.errors).toEqual([]);
});

test("suspicious beat review is usable on a compact screen", async ({ page }) => {
  const state = await setup(page, { events: suspiciousEvents });
  await openSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const issue = page.locator('[data-review-tick="0"]');
  await issue.scrollIntoViewIfNeeded();
  await expect(issue.getByRole("button", { name: "해결", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "test-results/beat-review-mobile.png", fullPage: true });
  await issue.getByRole("button", { name: "해결", exact: true }).click();
  await expect(issue).toContainText("해결됨");
  expect(state.errors).toEqual([]);
});

async function audioProbe(page: Page) {
  await page.addInitScript(() => {
    const state = { notes: [] as { frequency: number; time: number; media: number; when: number }[], levels: [] as number[][], analysers: [] as AnalyserNode[] };
    (window as any).__audioProbe = state;
    const connect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function (this: AudioNode, target: any, ...args: any[]) {
      if (this instanceof GainNode && target instanceof DynamicsCompressorNode) {
        const analyser = this.context.createAnalyser();
        analyser.fftSize = 1024;
        connect.call(this, analyser);
        state.analysers.push(analyser);
      }
      return (connect as any).call(this, target, ...args);
    };
    const create = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function () {
      const oscillator = create.call(this);
      let frequency = 0;
      const set = oscillator.frequency.setValueAtTime.bind(oscillator.frequency);
      oscillator.frequency.setValueAtTime = (value, time) => { frequency = value; return set(value, time); };
      const start = oscillator.start.bind(oscillator);
      oscillator.start = (when = 0) => {
        state.notes.push({ frequency, when, time: this.currentTime, media: document.querySelector('audio')?.currentTime || 0 });
        start(when);
      };
      return oscillator;
    };
    setInterval(() => {
      if (!state.analysers.length) return;
      state.levels.push(state.analysers.map((analyser) => {
        const data = new Float32Array(analyser.fftSize);
        analyser.getFloatTimeDomainData(data);
        return Math.sqrt(data.reduce((sum, value) => sum + value * value, 0) / data.length);
      }));
      if (state.levels.length > 500) state.levels.shift();
    }, 20);
  });
}

const auditionEvents: DrumEvent[] = [
  { time: 2, pitch: 35, strength: 0.8 },
  { time: 2.5, pitch: 38, strength: 0.8 },
  { time: 3, pitch: 42, strength: 0.8 },
  { time: 3.5, pitch: 46, strength: 0.8 },
  { time: 4, pitch: 49, strength: 0.8 },
];

test("bar playback mixes selected audio and draft drums then stops both at the boundary", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents, audioTone: true });
  await openSession(page);
  const audioTrack = page.getByRole("button", { name: "오디오", exact: true });
  const drumTrack = page.getByRole("button", { name: "드럼비트", exact: true });
  await expect(audioTrack).toHaveAttribute("aria-pressed", "true");
  await expect(drumTrack).toHaveAttribute("aria-pressed", "false");
  await drumTrack.click();
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "분리된 드럼", exact: true }).click();
  await page.getByRole("spinbutton", { name: "편집 마디" }).fill("2");
  await page.getByRole("button", { name: "2마디 구간재생", exact: true }).click();
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect(page.locator("audio")).toHaveAttribute("src", /drums.wav$/);
  await expect(page.locator(".playback-region")).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.some((level: number[]) => level[0]! > 0.01 && level[1]! > 0.01))).toBe(true);
  await page.screenshot({ path: "test-results/bar-playback-desktop.png", fullPage: true });
  await expect(page.getByRole("button", { name: "재생", exact: true })).toBeVisible();
  await expect(page.getByRole("slider", { name: "오디오 재생 위치" })).toHaveAttribute("aria-valuenow", "2");
  await expect(page.getByRole("spinbutton", { name: "편집 마디" })).toHaveValue("2");
  const notes = await page.evaluate(() => (window as any).__audioProbe.notes);
  expect(notes.filter((note: any) => note.frequency === 145)).toHaveLength(1);
  expect(notes.filter((note: any) => note.frequency === 185)).toHaveLength(1);
  expect(notes.some((note: any) => note.frequency === 3100)).toBe(false);
  const snare = notes.find((note: any) => note.frequency === 185);
  expect(snare.media + snare.when - snare.time).toBeCloseTo(2.5, 1);
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.slice(-2).every((level: number[]) => level.every((rms) => rms < 0.0001)))).toBe(true);
  expect(await page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.paused)).toBe(true);
  expect(state.errors).toEqual([]);
});

test("drum-only playback hears unsaved edits and track toggles preserve position", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents, audioTone: true });
  await openSession(page);
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await page.getByRole("spinbutton", { name: "편집 마디" }).fill("2");
  await page.getByRole("button", { name: "2마디 1번째 격자 Kick", exact: true }).click();
  await page.getByRole("button", { name: "2마디 1번째 격자 High tom", exact: true }).click();
  await page.getByRole("button", { name: "2마디 구간재생", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.notes.some((note: any) => note.frequency === 235))).toBe(true);
  expect(await page.evaluate(() => (window as any).__audioProbe.notes.some((note: any) => note.frequency === 145))).toBe(false);
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.some((level: number[]) => level[0]! > 0.01 && level[1]! < 0.0001))).toBe(true);
  expect(await page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.paused)).toBe(true);
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => !audio.paused && audio.currentTime >= 2 && audio.currentTime < 4)).toBe(true);
  await page.getByRole("button", { name: "정지", exact: true }).click();
  await expect(page.getByRole("slider", { name: "오디오 재생 위치" })).toHaveAttribute("aria-valuenow", "2");
  expect(state.getSaved()).toBeNull();
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await expect(page.getByRole("button", { name: "재생", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "2마디 구간재생", exact: true })).toBeDisabled();
  expect(state.errors).toEqual([]);
});

test("mixed playback preserves a region across source/rate changes and supports full-song seeking", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents, audioTone: true });
  await openSession(page);
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("spinbutton", { name: "편집 마디" }).fill("2");
  await page.getByRole("combobox", { name: "재생 속도" }).click();
  await page.getByRole("option", { name: "0.5×", exact: true }).click();
  await page.getByRole("button", { name: "2마디 구간재생", exact: true }).click();
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeGreaterThan(2.1);
  const before = await page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime);
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "Kick", exact: true }).click();
  await expect(page.locator("audio")).toHaveAttribute("src", /stem-kick.flac$/);
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeGreaterThanOrEqual(before);
  await expect(page.locator(".playback-status")).toContainText("2마디 구간재생");
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.playbackRate)).toBe(0.5);
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  await page.getByRole("button", { name: "선택구간 해제", exact: true }).click();
  await expect(page.locator(".playback-region")).toHaveCount(0);
  await page.getByRole("slider", { name: "오디오 재생 위치" }).press("Home");
  await expect(page.getByRole("slider", { name: "오디오 재생 위치" })).toHaveAttribute("aria-valuenow", "0");
  expect(state.errors).toEqual([]);
});

test("drums can play without a working audio source and fit a mobile toolbar", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents });
  await openSession(page);
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await page.route("**/files/drums.wav", (route) => route.fulfill({ status: 404, body: "missing" }));
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "분리된 드럼", exact: true }).click();
  await page.getByRole("spinbutton", { name: "편집 마디" }).fill("2");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "2마디 구간재생", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.some((level: number[]) => level[0]! > 0.01))).toBe(true);
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "test-results/bar-playback-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "정지", exact: true }).click();
  expect(state.errors).toEqual([]);
});

async function dragWaveform(page: Page, first: number, last: number) {
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  const bounds = await wave.boundingBox();
  if (!bounds) throw new Error("Waveform missing");
  const y = bounds.y + bounds.height / 2;
  await page.mouse.move(bounds.x + bounds.width * first, y);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * last, y, { steps: 6 });
  await page.mouse.up();
}

test("paused waveform navigation reveals the selected bar with playback follow off", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const bar = page.getByRole("spinbutton", { name: "편집 마디" });
  const follow = page.getByRole("switch", { name: "재생 따라가기" });
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  await follow.click();
  const bounds = (await wave.boundingBox())!;
  const click = () => page.mouse.click(bounds.x + bounds.width * 0.57, bounds.y + bounds.height / 2);
  await click();
  await expect(bar).toHaveValue("3");
  // A repeated selection must work even when the playhead time did not change.
  await bar.fill("1");
  await click();
  await expect(bar).toHaveValue("3");
  await dragWaveform(page, 0.3, 0.7);
  await expect(bar).toHaveValue("2");
  await bar.fill("4");
  await dragWaveform(page, 0.3, 0.7);
  await expect(bar).toHaveValue("2");
  await dragWaveform(page, 0.8, 0.55);
  await expect(bar).toHaveValue("3");
  // Cancelling a preview must retain both the committed range and editor bar.
  await page.mouse.move(bounds.x + bounds.width * 0.1, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.4, bounds.y + bounds.height / 2);
  await page.keyboard.press("Escape");
  await page.mouse.up();
  await expect(bar).toHaveValue("3");
  await wave.press("Shift+ArrowLeft");
  await expect(bar).toHaveValue("2");
  await wave.press("Home");
  await expect(bar).toHaveValue("1");
  await expect(follow).not.toBeChecked();
  await expect(page.getByRole("button", { name: "재생", exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("waveform navigation during playback reveals a bar without enabling automatic follow", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const bar = page.getByRole("spinbutton", { name: "편집 마디" });
  const follow = page.getByRole("switch", { name: "재생 따라가기" });
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  const audio = page.locator("audio");
  await follow.click();
  const bounds = (await wave.boundingBox())!;
  await page.mouse.click(bounds.x + bounds.width * 0.46, bounds.y + bounds.height / 2);
  await expect(bar).toHaveValue("2");
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.currentTime)).toBeGreaterThan(4.1);
  await expect(bar).toHaveValue("2");
  await page.mouse.click(bounds.x + bounds.width * 0.7, bounds.y + bounds.height / 2);
  await expect(bar).toHaveValue("3");
  await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.currentTime)).toBeGreaterThan(6.1);
  await expect(bar).toHaveValue("3");
  // Leave enough range after the next bar for the follow switch interaction.
  await dragWaveform(page, 0.3, 0.95);
  await expect(bar).toHaveValue("2");
  await expect(follow).not.toBeChecked();
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect.poll(() => audio.evaluate((e: HTMLAudioElement) => e.currentTime)).toBeGreaterThan(4.1);
  await expect(bar).toHaveValue("2");
  await follow.click();
  await expect(bar).toHaveValue("3");
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  expect(state.errors).toEqual([]);
});

test("contextual clear-selection button restores full playback without interrupting the mix", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents, audioTone: true });
  await openSession(page);
  const clear = page.getByRole("button", { name: "선택구간 해제", exact: true });
  await expect(clear).toHaveCount(0);
  await expect(page.locator(".playback-status")).toContainText("전체 재생");
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("spinbutton", { name: "편집 마디" }).fill("2");
  await page.getByRole("button", { name: "2마디 구간재생", exact: true }).click();
  await expect(clear).toBeEnabled();
  await expect(page.getByRole("group", { name: "선택구간", exact: true }).getByRole("button", { name: "선택구간 해제" })).toBeVisible();
  const repeat = page.getByRole("button", { name: "구간 반복", exact: true });
  await repeat.click();
  await expect(repeat).toHaveAttribute("aria-pressed", "true");
  const before = await page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime);
  await clear.click();
  await expect(page.locator(".playback-region")).toHaveCount(0);
  await expect(page.locator(".playback-status")).toContainText("전체 재생");
  await expect(clear).toHaveCount(0);
  await expect(page.getByRole("slider", { name: "오디오 재생 위치" })).toBeFocused();
  await expect(repeat).toBeDisabled();
  await expect(repeat).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeGreaterThanOrEqual(before);
  // Both the audio clock and the previously excluded cymbal pass the old bar end.
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeGreaterThan(4.1);
  expect(await page.evaluate(() => (window as any).__audioProbe.notes.some((note: any) => note.frequency === 3100))).toBe(true);
  await page.getByRole("button", { name: "일시 정지", exact: true }).click();
  expect(state.errors).toEqual([]);
});

test("waveform drag previews a range and auditions only the selected time", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { events: auditionEvents, audioTone: true });
  await openSession(page);
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  const clear = page.getByRole("button", { name: "선택구간 해제", exact: true });
  const bounds = (await wave.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * 0.25, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.375, bounds.y + bounds.height / 2, { steps: 6 });
  await expect(page.locator(".playback-region")).toBeVisible();
  await expect(clear).toHaveCount(0);
  await expect(wave).toHaveAttribute("aria-valuenow", "0");
  await page.mouse.up();
  await expect(clear).toBeEnabled();
  await expect(page.locator(".playback-status")).toContainText("선택구간 재생");
  expect(Number(await wave.getAttribute("aria-valuenow"))).toBeCloseTo(2, 1);
  expect(Number(await page.locator(".playback-region").getAttribute("data-end"))).toBeCloseTo(3, 1);
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.screenshot({ path: "test-results/waveform-selection-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "재생", exact: true }).click();
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "재생", exact: true })).toBeVisible();
  expect(Number(await wave.getAttribute("aria-valuenow"))).toBeCloseTo(2, 1);
  const notes = await page.evaluate(() => (window as any).__audioProbe.notes);
  expect(notes.some((note: any) => note.frequency === 185)).toBe(true);
  expect(notes.some((note: any) => note.frequency === 3100)).toBe(false);
  await clear.click();
  expect(Number(await wave.getAttribute("aria-valuenow"))).toBeCloseTo(2, 1);
  expect(state.errors).toEqual([]);
});

test("reverse drags, edge releases, clicks and cancelled gestures keep selection predictable", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  const region = page.locator(".playback-region");
  await dragWaveform(page, 0.75, 0.5);
  expect(Number(await region.getAttribute("data-start"))).toBeCloseTo(4, 1);
  expect(Number(await region.getAttribute("data-end"))).toBeCloseTo(6, 1);
  // Capture keeps the release even outside the waveform's right edge.
  await dragWaveform(page, 0.5, 1.02);
  expect(Number(await region.getAttribute("data-end"))).toBe(8);
  const oldStart = await region.getAttribute("data-start");
  const bounds = (await wave.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width * 0.2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.4, bounds.y + bounds.height / 2, { steps: 4 });
  await page.keyboard.press("Escape");
  await page.mouse.up();
  await expect(region).toHaveAttribute("data-start", oldStart!);
  await expect(region).toHaveAttribute("data-end", "8");
  await wave.press("Escape");
  await expect(region).toHaveCount(0);
  await wave.press("Home");
  await wave.press("Shift+ArrowRight");
  await expect(region).toHaveAttribute("data-start", "0");
  await expect(region).toHaveAttribute("data-end", "1");
  await page.mouse.click(bounds.x + bounds.width * 0.5, bounds.y + bounds.height / 2);
  await expect(region).toHaveCount(0);
  expect(Number(await wave.getAttribute("aria-valuenow"))).toBeCloseTo(4, 1);
  // A two-pixel jitter remains a seek, not a tiny playback range.
  await page.mouse.move(bounds.x + bounds.width * 0.25, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.25 + 2, bounds.y + bounds.height / 2);
  await page.mouse.up();
  await expect(region).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("touch drag selects a range and exposes clear selection on a compact screen", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const bar = page.getByRole("spinbutton", { name: "편집 마디" });
  const follow = page.getByRole("switch", { name: "재생 따라가기" });
  await bar.fill("4");
  await expect(follow).not.toBeChecked();
  const wave = page.getByRole("slider", { name: "오디오 재생 위치" });
  const bounds = (await wave.boundingBox())!;
  const client = await page.context().newCDPSession(page);
  const point = (fraction: number) => [{ x: bounds.x + bounds.width * fraction, y: bounds.y + bounds.height / 2 }];
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: point(0.2) });
  await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: point(0.6) });
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  const region = page.locator(".playback-region");
  expect(Number(await region.getAttribute("data-start"))).toBeCloseTo(1.6, 1);
  expect(Number(await region.getAttribute("data-end"))).toBeCloseTo(4.8, 1);
  await expect(bar).toHaveValue("1");
  await expect(follow).not.toBeChecked();
  const clear = page.getByRole("button", { name: "선택구간 해제", exact: true });
  await expect(clear).toBeVisible();
  const repeat = page.getByRole("button", { name: "구간 반복", exact: true });
  await expect(repeat).toBeVisible();
  await repeat.click();
  await expect(repeat).toHaveAttribute("aria-pressed", "true");
  const button = (await clear.boundingBox())!;
  const panel = (await page.locator(".audio-timeline").boundingBox())!;
  expect(button.y + button.height).toBeLessThanOrEqual(panel.y + panel.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "test-results/waveform-selection-mobile.png", fullPage: true });
  await clear.click();
  await expect(region).toHaveCount(0);
  await expect(page.locator(".playback-status")).toContainText("전체 재생");
  await client.detach();
  expect(state.errors).toEqual([]);
});

test("looped audio and drums apply edits on the next pass, including across source and rate changes", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { audioTone: true, events: [
    { time: 2, pitch: 35, strength: 0.8 },
    { time: 3.5, pitch: 38, strength: 0.8 },
    { time: 4, pitch: 49, strength: 0.8 },
  ] });
  await openSession(page);
  const repeat = page.getByRole("button", { name: "구간 반복", exact: true });
  await expect(repeat).toBeDisabled();
  await expect(repeat).toHaveAttribute("aria-pressed", "false");
  await dragWaveform(page, 0.25, 0.5);
  await repeat.click();
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("combobox", { name: "재생 속도" }).click();
  await page.getByRole("option", { name: "0.75×", exact: true }).click();
  await page.getByRole("button", { name: "재생", exact: true }).click();
  const count = (frequency: number) => page.evaluate(frequency =>
    (window as any).__audioProbe.notes.filter((note: any) => note.frequency === frequency).length, frequency);
  await expect.poll(() => count(145)).toBe(1);
  await page.getByRole("button", { name: "2마디 13번째 격자 Snare", exact: true }).click();
  await page.getByRole("button", { name: "2마디 13번째 격자 High tom", exact: true }).click();
  await expect(page.locator(".playback-status")).toContainText("수정 내용은 다음 반복부터 반영됩니다.");
  for (const label of ["오디오", "드럼비트"]) {
    const slider = await openTrackVolume(page, label);
    await slider.press("PageDown");
    await slider.press("Escape");
  }
  await expect(page.locator(".playback-status")).toContainText("수정 내용은 다음 반복부터 반영됩니다.");
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "분리된 드럼", exact: true }).click();
  await page.getByRole("combobox", { name: "재생 속도" }).click();
  await page.getByRole("option", { name: "0.5×", exact: true }).click();
  await expect(page.locator("audio")).toHaveAttribute("src", /drums.wav$/);
  await expect.poll(() => page.locator("audio").evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeLessThan(3.2);
  await page.screenshot({ path: "test-results/loop-pending-edits.png", fullPage: true });
  // The not-yet-scheduled old snare still belongs to this pass; the new tom waits.
  await expect.poll(() => count(185)).toBe(1);
  expect(await count(235)).toBe(0);
  await expect.poll(() => count(235), { timeout: 8000 }).toBe(1);
  await expect(page.locator(".playback-status")).not.toContainText("수정 내용은 다음 반복부터 반영됩니다.");
  expect(await count(185)).toBe(1);
  expect(await count(145)).toBe(2);
  expect(await count(3100)).toBe(0);
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.some((level: number[]) => level[0]! > 0.01 && level[1]! > 0.01))).toBe(true);
  const tom = await page.evaluate(() => (window as any).__audioProbe.notes.find((note: any) => note.frequency === 235));
  expect(tom.media + (tom.when - tom.time) * 0.5).toBeCloseTo(3.5, 1);
  // Disabling repetition lets this pass finish without restarting or losing the selection.
  await repeat.click();
  await expect(page.getByRole("button", { name: "재생", exact: true })).toBeVisible();
  expect(Number(await page.getByRole("slider", { name: "오디오 재생 위치" }).getAttribute("aria-valuenow"))).toBeCloseTo(2, 1);
  expect(await count(145)).toBe(2);
  expect(state.getSaved()).toBeNull();
  expect(state.errors).toEqual([]);
});

for (const mode of ["mixed", "drums"] as const) {
  test(`${mode} playback repeats a range ending at the file boundary and stops on demand`, async ({ page }) => {
    await audioProbe(page);
    const state = await setup(page, { audioTone: true, events: [
      { time: 6, pitch: 35, strength: 0.8 }, { time: 7.5, pitch: 38, strength: 0.8 },
    ] });
    await openSession(page);
    await page.getByRole("button", { name: "드럼비트", exact: true }).click();
    if (mode === "drums") await page.getByRole("button", { name: "오디오", exact: true }).click();
    await dragWaveform(page, 0.75, 1);
    const repeat = page.getByRole("button", { name: "구간 반복", exact: true });
    await repeat.click();
    await page.getByRole("combobox", { name: "재생 속도" }).click();
    await page.getByRole("option", { name: "1.25×", exact: true }).click();
    await page.getByRole("button", { name: "재생", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.notes.filter((note: any) => note.frequency === 145).length)).toBe(2);
    await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "일시 정지", exact: true }).click();
    await expect(repeat).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.slice(-2).every((level: number[]) => level.every((rms) => rms < 0.0001)))).toBe(true);
    await page.getByRole("button", { name: "재생", exact: true }).click();
    await page.getByRole("button", { name: "정지", exact: true }).click();
    await expect(page.getByRole("button", { name: "재생", exact: true })).toBeVisible();
    expect(Number(await page.getByRole("slider", { name: "오디오 재생 위치" }).getAttribute("aria-valuenow"))).toBeCloseTo(6, 1);
    await page.getByRole("button", { name: "선택구간 해제", exact: true }).click();
    await expect(repeat).toBeDisabled();
    await expect(repeat).toHaveAttribute("aria-pressed", "false");
    expect(state.errors).toEqual([]);
  });
}

async function openTrackVolume(page: Page, label: string) {
  await page.getByRole("button", { name: `${label} 볼륨 조절`, exact: true }).click();
  const slider = page.getByRole("slider", { name: `${label} 볼륨`, exact: true });
  await expect(slider).toHaveAttribute("aria-orientation", "vertical");
  return slider;
}

async function setTrackVolume(page: Page, label: string, value: number) {
  const slider = await openTrackVolume(page, label);
  await slider.press("Home");
  for (let step = 0; step < value / 10; step++) await slider.press("PageUp");
  await expect(slider).toHaveAttribute("aria-valuenow", String(value));
  await slider.press("Escape");
  await expect(slider).not.toBeVisible();
  await expect(page.getByRole("button", { name: `${label} 볼륨 조절`, exact: true })).toBeFocused();
}

test("independent track levels change the rendered loop mix without restarting playback", async ({ page }) => {
  await audioProbe(page);
  const state = await setup(page, { audioTone: true, events: Array.from({ length: 64 }, (_, index) => ({
    time: index * 0.125, pitch: 50, strength: 0.8,
  })) });
  await openSession(page);
  const audioVolume = page.getByRole("button", { name: "오디오 볼륨 조절", exact: true });
  const drumVolume = page.getByRole("button", { name: "드럼비트 볼륨 조절", exact: true });
  await expect(audioVolume).toHaveText("80%");
  await expect(drumVolume).toHaveText("80%");
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await dragWaveform(page, 0.25, 0.5);
  const repeat = page.getByRole("button", { name: "구간 반복", exact: true });
  await repeat.click();
  await page.getByRole("button", { name: "재생", exact: true }).click();
  const measure = async () => {
    await page.evaluate(() => { (window as any).__audioProbe.levels = []; });
    await expect.poll(() => page.evaluate(() => (window as any).__audioProbe.levels.length)).toBeGreaterThanOrEqual(55);
    return page.evaluate(() => [0, 1].map(index => Math.max(...(window as any).__audioProbe.levels.slice(-40).map((level: number[]) => level[index]))));
  };
  const original = await measure();
  expect(original[0]).toBeGreaterThan(0.01);
  expect(original[1]).toBeGreaterThan(0.01);
  // Measure each pre-compressor output: halving audio leaves the synth unchanged.
  await setTrackVolume(page, "오디오", 40);
  const quietAudio = await measure();
  expect(quietAudio[1]! / original[1]!).toBeCloseTo(0.5, 1);
  expect(quietAudio[0]! / original[0]!).toBeGreaterThan(0.8);
  expect(quietAudio[0]! / original[0]!).toBeLessThan(1.2);
  await expect(drumVolume).toHaveText("80%");
  const openVolume = await openTrackVolume(page, "오디오");
  await page.screenshot({ path: "test-results/track-volumes-desktop.png", fullPage: true });
  await openVolume.press("Escape");
  await setTrackVolume(page, "드럼비트", 40);
  const quietBoth = await measure();
  // Percussion envelopes vary with the sampled phase; allow 20% of the expected
  // ratio, while the continuous source tone can be compared more precisely.
  expect(quietBoth[0]! / original[0]!).toBeGreaterThan(0.4);
  expect(quietBoth[0]! / original[0]!).toBeLessThan(0.6);
  expect(quietBoth[1]! / quietAudio[1]!).toBeCloseTo(1, 1);
  await setTrackVolume(page, "오디오", 0);
  const drumsOnly = await measure();
  expect(drumsOnly[1]).toBeLessThan(0.0001);
  expect(drumsOnly[0]).toBeGreaterThan(0.01);
  await setTrackVolume(page, "드럼비트", 0);
  const silent = await measure();
  expect(silent.every(value => value < 0.0001)).toBe(true);
  // Zero volume does not switch off a track, pause playback or cancel the range.
  await expect(page.getByRole("button", { name: "오디오", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "드럼비트", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  await expect(repeat).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".playback-region")).toHaveAttribute("data-start", "2");
  await expect(page.locator(".playback-status")).not.toContainText("수정 내용은 다음 반복부터 반영됩니다.");
  await setTrackVolume(page, "오디오", 30);
  await setTrackVolume(page, "드럼비트", 60);
  await page.getByRole("combobox", { name: "재생 소스" }).click();
  await page.getByRole("option", { name: "분리된 드럼", exact: true }).click();
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await page.getByRole("button", { name: "오디오", exact: true }).click();
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await page.getByRole("button", { name: "드럼비트", exact: true }).click();
  await expect(audioVolume).toHaveText("30%");
  await expect(drumVolume).toHaveText("60%");
  await expect(page.getByRole("button", { name: "일시 정지", exact: true })).toBeVisible();
  const restored = await measure();
  expect(restored[1]! / original[1]!).toBeCloseTo(30 / 80, 1);
  expect(restored[0]! / original[0]!).toBeGreaterThan(0.6);
  expect(restored[0]! / original[0]!).toBeLessThan(0.9);
  await page.getByRole("button", { name: "정지", exact: true }).click();
  expect(state.errors).toEqual([]);
});

test("track volume popovers open on demand and support vertical touch adjustment on a compact screen", async ({ page }) => {
  const state = await setup(page);
  await openSession(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const audioVolume = page.getByRole("slider", { name: "오디오 볼륨", exact: true });
  const drumVolume = page.getByRole("slider", { name: "드럼비트 볼륨", exact: true });
  await expect(audioVolume).toHaveCount(0);
  await expect(drumVolume).toHaveCount(0);
  await openTrackVolume(page, "오디오");
  const dialog = page.getByRole("dialog", { name: "오디오 볼륨 조절" });
  const popup = (await dialog.boundingBox())!;
  expect(popup.x).toBeGreaterThanOrEqual(0);
  expect(popup.y).toBeGreaterThanOrEqual(0);
  expect(popup.x + popup.width).toBeLessThanOrEqual(390);
  expect(popup.y + popup.height).toBeLessThanOrEqual(844);
  const bounds = (await dialog.locator(".track-volume-slider").boundingBox())!;
  expect(bounds.height).toBeGreaterThan(100);
  const client = await page.context().newCDPSession(page);
  const thumb = (await audioVolume.boundingBox())!;
  const point = (fraction: number) => [{ x: bounds.x + bounds.width / 2,
    y: bounds.y + bounds.height - thumb.height / 2 - (bounds.height - thumb.height) * fraction }];
  await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: point(0.8) });
  for (const fraction of [0.799, 0.7, 0.6, 0.5, 0.4, 0.3])
    await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: point(fraction) });
  await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  const touchedVolume = Number(await audioVolume.getAttribute("aria-valuenow"));
  expect(Math.abs(touchedVolume - 30)).toBeLessThanOrEqual(2);
  await audioVolume.press("ArrowUp");
  await expect(audioVolume).toHaveAttribute("aria-valuenow", String(touchedVolume + 1));
  await audioVolume.press("ArrowDown");
  await expect(audioVolume).toHaveAttribute("aria-valuenow", String(touchedVolume));
  await page.screenshot({ path: "test-results/track-volumes-mobile.png", fullPage: true });
  await audioVolume.press("Escape");
  await expect(audioVolume).not.toBeVisible();
  await expect(page.getByRole("button", { name: "오디오 볼륨 조절", exact: true })).toBeFocused();
  // Opening the other popover dismisses the first without changing either toggle.
  await openTrackVolume(page, "오디오");
  await openTrackVolume(page, "드럼비트");
  await expect(audioVolume).not.toBeVisible();
  await expect(drumVolume).toHaveAttribute("aria-valuenow", "80");
  await drumVolume.press("Escape");
  await setTrackVolume(page, "드럼비트", 50);
  await expect(page.getByRole("button", { name: "드럼비트", exact: true })).toHaveAttribute("aria-pressed", "false");
  await openTrackVolume(page, "오디오");
  await page.getByRole("slider", { name: "오디오 재생 위치" }).click();
  await expect(audioVolume).not.toBeVisible();
  await expect(page.getByRole("button", { name: "오디오 볼륨 조절", exact: true })).toHaveText(`${touchedVolume}%`);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await client.detach();
  expect(state.errors).toEqual([]);
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 900, height: 500 }]) {
  test(`audio splitter preserves the waveform and controls at minimum height (${viewport.width}x${viewport.height})`, async ({ page }) => {
    const state = await setup(page);
    await openSession(page);
    await page.setViewportSize(viewport);
    const audio = page.locator(".audio-timeline");
    const handle = page.locator('[data-panel-group-id="studio-editor"] > [data-slot="handle"]');
    const usable = () => audio.evaluate(element => {
      const area = element.getBoundingClientRect();
      const wave = element.querySelector(".wave-track")!.getBoundingClientRect();
      const controls = element.querySelector(".transport")!.getBoundingClientRect();
      const status = element.querySelector(".playback-status")!.getBoundingClientRect();
      return wave.height >= 71 && wave.top >= area.top && wave.bottom <= controls.top + 1
        && status.bottom <= area.bottom + 1 && element.scrollHeight <= element.clientHeight + 2;
    });
    await expect.poll(usable).toBe(true);
    const bounds = (await handle.boundingBox())!;
    const panel = (await audio.boundingBox())!;
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2, panel.y + 5, { steps: 8 });
    await page.mouse.up();
    await expect.poll(usable).toBe(true);
    // A newly selected range adds a contextual row; it must grow the minimum.
    await dragWaveform(page, 0.2, 0.5);
    await expect(page.getByRole("button", { name: "선택구간 해제", exact: true })).toBeVisible();
    await handle.press("Home");
    await expect.poll(usable).toBe(true);
    await page.screenshot({ path: `test-results/audio-minimum-${viewport.width}.png`, fullPage: true });
    await page.getByRole("button", { name: "선택구간 해제", exact: true }).click();
    await handle.press("Home");
    await expect.poll(usable).toBe(true);
    // Restoring a saved small pane must respect its content's current minimum.
    await page.reload();
    if (viewport.width < 1024) await page.getByRole("button", { name: "세션 브라우저 토글" }).click();
    await openSession(page);
    await expect.poll(usable).toBe(true);
    expect(state.errors).toEqual([]);
  });
}
