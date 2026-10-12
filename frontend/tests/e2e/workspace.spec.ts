import { expect, test, type Page } from "@playwright/test";
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
async function setup(page: Page) {
  let job = sampleJob();
  let history = true;
  let saved: any = null;
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
      job.result.generation = "render-2";
      job.result.grid.bpm = saved.options.bpm;
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
    return route.fulfill({ json: job });
  });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "새 세션", exact: true }),
  ).toBeVisible();
  return {
    errors,
    getSaved: () => saved,
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
