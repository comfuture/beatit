import { expect, test, type Page } from "@playwright/test";
import type { ScoreMetadata } from "../../app/types/studio";
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
async function setup(page: Page, { multiPage = false, layoutUnavailable = false, offset = 0 } = {}) {
  let job = sampleJob();
  job.result.grid.offset = offset;
  job.result.options.offset = offset;
  if (multiPage) job.result.pages.push("score-2.svg");
  let history = true;
  let saved: any = null;
  let heading: ScoreMetadata | null = null;
  let pendingHeading: ScoreMetadata | null = null;
  let rejectHeading = false;
  let jobEvents = structuredClone(initialEvents);
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
      job.result.metadata_source_generation = undefined;
      job.result.generation = "render-2";
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
  await expect(bar).toHaveValue("2");
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
