import { expect, test, type Page, type Route } from "@playwright/test";

function jobFor(owner: string) {
  const options = {
    bpm: 120, offset: 0, meter: "4/4", grid: "16", device: "auto",
    renderer: "auto", source: "mix", sensitivity: 1, tom_refinement: true,
  };
  return {
    id: (owner === "A" ? "a" : "b").repeat(32), project_id: `project-${owner}`,
    filename: `${owner} private.wav`, status: "completed", created: 1, updated: 1,
    has_result: true, progress: { stage: "complete", progress: 1, message: "완료" },
    result: {
      generation: "render-1", duration: 8, options,
      grid: { bpm: 120, offset: 0, meter: "4/4", bars: 4, bar_ticks: 48, step: 3, mean_quantization_error_ms: 0 },
      counts: { Kick: 1 }, event_count: 1, pages: ["score-1.svg"], pdf: false,
      renderer: "verovio", transcription_device: "cpu", warnings: [], stems: [],
    },
  };
}
async function accountApi(page: Page, initial: string | null) {
  let owner = initial;
  let denyJobs = false;
  let historyRequests = 0;
  let hold: ((route: Route) => Promise<void>) | null = null;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({ json: { user: owner ? { id: owner, email: `${owner}@example.com`, name: `Account ${owner}` } : null } });
    if (path === "/api/auth/logout") {
      owner = null;
      return route.fulfill({ status: 204 });
    }
    if (path === "/api/system") return route.fulfill({ json: {
      devices: ["cpu"], default_device: "cpu", ffmpeg: true, verovio: true,
      musescore: false, max_upload_bytes: 1073741824, max_duration_seconds: 1800,
    } });
    if (path === "/api/jobs") {
      historyRequests++;
      return route.fulfill(denyJobs || !owner ? { status: 401, json: { detail: "Sign in" } } : { json: [jobFor(owner)] });
    }
    if (hold && path === `/api/jobs/${"a".repeat(32)}`) return hold(route);
    if (!owner || !path.startsWith(`/api/jobs/${jobFor(owner).id}`)) return route.fulfill({ status: 404 });
    if (path.endsWith("events.json")) return route.fulfill({ json: [{ time: 0, pitch: 35, strength: 0.8, velocity: 100 }] });
    if (path.endsWith("waveform")) return route.fulfill({ json: { duration: 8, peaks: [0.2, 0.5, 0.2] } });
    if (path.endsWith(".wav")) return route.fulfill({ contentType: "audio/wav", body: Buffer.alloc(44) });
    return route.fulfill({ json: jobFor(owner) });
  });
  return {
    switchTo: (next: string | null) => { owner = next; },
    expire: () => { owner = null; denyJobs = true; },
    denyHistory: () => { denyJobs = true; },
    historyRequests: () => historyRequests,
    holdDetail: (callback: (route: Route) => Promise<void>) => { hold = callback; },
  };
}
async function chooseSession(page: Page, owner: string) {
  await page.getByRole("button", { name: "불러오기", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "불러오기", exact: true });
  await dialog.getByRole("button", { name: new RegExp(`${owner} private`) }).click();
  await dialog.getByRole("button", { name: "불러오기", exact: true }).click();
}

test("signed-out visitors see Google signup and never request project history", async ({ page }) => {
  const api = await accountApi(page, null);
  await page.addInitScript(() => localStorage.setItem("beatit-session", "legacy-unowned-id"));
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Google로 시작하기" })).toHaveAttribute("href", "/api/auth/google");
  await expect(page.locator(".studio-shell")).toHaveCount(0);
  expect(api.historyRequests()).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem("beatit-session"))).toBeNull();
  await page.screenshot({ path: "test-results/auth-sign-in.png" });
});

test("separate account contexts retain only their own selection and logout releases media", async ({ browser }) => {
  const contextA = await browser.newContext();
  const contextB = await browser.newContext();
  const pageA = await contextA.newPage();
  const pageB = await contextB.newPage();
  try {
    await accountApi(pageA, "A");
    await accountApi(pageB, "B");
    await Promise.all([pageA.goto("/"), pageB.goto("/")]);
    await Promise.all([chooseSession(pageA, "A"), chooseSession(pageB, "B")]);
    await expect(pageA.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
    await expect(pageB.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
    expect(await pageA.evaluate(() => localStorage.getItem("beatit-session:A"))).toBe("a".repeat(32));
    expect(await pageA.evaluate(() => localStorage.getItem("beatit-session:B"))).toBeNull();
    expect(await pageB.evaluate(() => localStorage.getItem("beatit-session:B"))).toBe("b".repeat(32));
    await pageA.reload();
    await expect(pageA.locator(".header-filename")).toHaveText("A private.wav");
    await pageA.getByRole("button", { name: "로그아웃", exact: true }).click();
    await expect(pageA.getByRole("link", { name: "Google로 시작하기" })).toBeVisible();
    await expect(pageA.locator("audio, .audio-timeline, .beat-editor, .studio-shell")).toHaveCount(0);
    expect(await pageA.evaluate(() => localStorage.getItem("beatit-session:A"))).toBeNull();
    await expect(pageB.locator(".header-filename")).toHaveText("B private.wav");
  } finally {
    await contextA.close();
    await contextB.close();
  }
});

test("account switches discard delayed old-account responses and selected state", async ({ page }) => {
  const api = await accountApi(page, "A");
  let release!: () => void;
  let requested!: () => void;
  const incoming = new Promise<void>((resolve) => { requested = resolve; });
  const held = new Promise<void>((resolve) => { release = resolve; });
  api.holdDetail(async (route) => {
    requested();
    await held;
    await route.fulfill({ json: jobFor("A") }).catch(() => {});
  });
  await page.goto("/");
  await chooseSession(page, "A");
  await incoming;
  api.switchTo("B");
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("Account B", { exact: true })).toBeVisible();
  release();
  await expect(page.locator(".header-filename")).toHaveText("새 세션");
  await expect(page.locator("audio, .audio-timeline")).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("beatit-session:A"))).toBeNull();
  await page.getByRole("button", { name: "불러오기", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "불러오기", exact: true });
  await expect(dialog.getByText("B private.wav", { exact: true })).toBeVisible();
  await expect(dialog.getByText("A private.wav", { exact: true })).toHaveCount(0);
});

test("an unauthorized project response immediately closes the private workspace", async ({ page }) => {
  const api = await accountApi(page, "A");
  await page.goto("/");
  await chooseSession(page, "A");
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
  api.expire();
  await expect(page.getByRole("link", { name: "Google로 시작하기" })).toBeVisible();
  await expect(page.locator("audio, .studio-shell")).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("beatit-session:A"))).toBeNull();
});

test("a pending logout keeps a second tab signed out through polling and focus", async ({ browser }) => {
  const context = await browser.newContext();
  const first = await context.newPage();
  const second = await context.newPage();
  try {
    const firstApi = await accountApi(first, "A");
    const secondApi = await accountApi(second, "A");
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    await first.route("**/api/auth/logout", async (route) => {
      await held;
      firstApi.switchTo(null);
      secondApi.switchTo(null);
      await route.fulfill({ status: 204 });
    });
    await Promise.all([first.goto("/"), second.goto("/")]);
    await chooseSession(second, "A");
    await expect(second.locator("audio")).toHaveCount(1);
    await first.getByRole("button", { name: "로그아웃", exact: true }).click();
    await expect(second.getByRole("status")).toHaveText("로그아웃 중");
    const requestsWhileBlocked = secondApi.historyRequests();
    await second.evaluate(() => window.dispatchEvent(new Event("focus")));
    // An unrelated completion must not unblock the current logout attempt.
    await first.evaluate(() => localStorage.setItem("beatit-auth-change", JSON.stringify({
      ownerId: "A", nonce: "unrelated-old-attempt", phase: "completed",
    })));
    await second.waitForTimeout(2200);
    await expect(second.locator(".studio-shell, audio")).toHaveCount(0);
    await expect(second.getByRole("status")).toHaveText("로그아웃 중");
    expect(secondApi.historyRequests()).toBe(requestsWhileBlocked);
    await expect(second.getByRole("link", { name: "Google로 시작하기" })).toHaveAttribute("aria-disabled", "true");
    release();
    await expect(first.getByRole("link", { name: "Google로 시작하기" })).not.toHaveAttribute("aria-disabled", "true");
    await expect(second.getByRole("link", { name: "Google로 시작하기" })).not.toHaveAttribute("aria-disabled", "true");
    await expect(second.locator(".studio-shell")).toHaveCount(0);
  } finally {
    await context.close();
  }
});

test("failed logout keeps both tabs cleared until an explicit retry", async ({ browser }) => {
  const context = await browser.newContext();
  const first = await context.newPage();
  const second = await context.newPage();
  try {
    const firstApi = await accountApi(first, "A");
    const secondApi = await accountApi(second, "A");
    await first.route("**/api/auth/logout", (route) => route.fulfill({ status: 503 }));
    await Promise.all([first.goto("/"), second.goto("/")]);
    await expect(second.getByRole("button", { name: "로그아웃", exact: true })).toBeVisible();
    await first.getByRole("button", { name: "로그아웃", exact: true }).click();
    await expect(first.getByRole("button", { name: "로그아웃 다시 시도" })).toBeVisible();
    await expect(second.getByRole("button", { name: "로그아웃 다시 시도" })).toBeVisible();
    const firstRequests = firstApi.historyRequests();
    const secondRequests = secondApi.historyRequests();
    await Promise.all([first, second].map((page) => page.evaluate(() => window.dispatchEvent(new Event("focus")))));
    await second.waitForTimeout(2200);
    await expect(first.locator(".studio-shell")).toHaveCount(0);
    await expect(second.locator(".studio-shell")).toHaveCount(0);
    expect(firstApi.historyRequests()).toBe(firstRequests);
    expect(secondApi.historyRequests()).toBe(secondRequests);
    await second.route("**/api/auth/logout", async (route) => {
      firstApi.switchTo(null);
      secondApi.switchTo(null);
      await route.fulfill({ status: 204 });
    });
    await second.getByRole("button", { name: "로그아웃 다시 시도" }).click();
    await expect(first.getByRole("button", { name: "로그아웃 다시 시도" })).toHaveCount(0);
    await expect(second.getByRole("button", { name: "로그아웃 다시 시도" })).toHaveCount(0);
    await expect(first.locator(".studio-shell")).toHaveCount(0);
    await expect(second.locator(".studio-shell")).toHaveCount(0);
  } finally {
    await context.close();
  }
});
