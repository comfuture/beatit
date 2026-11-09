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
function silentWave() {
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
  return wav;
}
async function accountApi(page: Page, initial: string | null) {
  let owner = initial;
  let denyJobs = false;
  let historyRequests = 0;
  let hideHistory = false;
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
      return route.fulfill(denyJobs || !owner ? { status: 401, json: { detail: "Sign in" } } : { json: hideHistory ? [] : [jobFor(owner)] });
    }
    if (!owner || !path.startsWith(`/api/jobs/${jobFor(owner).id}`)) return route.fulfill({ status: 404 });
    if (hold && path === `/api/jobs/${"a".repeat(32)}`) return hold(route);
    if (path.endsWith("events.json")) return route.fulfill({ json: [{ time: 0, pitch: 35, strength: 0.8, velocity: 100 }] });
    if (path.endsWith("waveform")) return route.fulfill({ json: { duration: 8, peaks: [0.2, 0.5, 0.2] } });
    if (path.endsWith(".wav")) return route.fulfill({ contentType: "audio/wav", body: silentWave() });
    return route.fulfill({ json: jobFor(owner) });
  });
  return {
    switchTo: (next: string | null) => { owner = next; },
    expire: () => { owner = null; denyJobs = true; },
    denyHistory: () => { denyJobs = true; },
    historyRequests: () => historyRequests,
    hideHistory: () => { hideHistory = true; },
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

test("a session permalink restores directly after storage loss and root remains blank", async ({ page }) => {
  const api = await accountApi(page, "A");
  api.hideHistory();
  const path = `/sessions/${"a".repeat(32)}`;
  await page.goto(path);
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
  await expect(page.locator(".header-filename")).toHaveText("A private.wav");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
  await page.getByRole("button", { name: "새로 만들기", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("audio")).toHaveCount(0);
  await page.evaluate(() => localStorage.setItem("beatit-session:A", "a".repeat(32)));
  await page.reload();
  await expect(page.locator(".header-filename")).toHaveText("새 세션");
  await expect(page.locator("audio")).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
});

for (const scenario of [
  { owner: "B", id: "a".repeat(32), label: "another owner's" },
  { owner: "A", id: "c".repeat(32), label: "a missing or deleted" },
]) {
  test(`${scenario.label} permalink never loads private media or a fallback selection`, async ({ page }) => {
    const api = await accountApi(page, scenario.owner);
    api.hideHistory();
    await page.addInitScript(({ owner, id }) => localStorage.setItem(`beatit-session:${owner}`, id), { owner: scenario.owner, id: jobFor(scenario.owner).id });
    await page.goto(`/sessions/${scenario.id}`);
    await expect(page.getByText("세션을 찾을 수 없습니다. 삭제되었거나 접근 권한이 없는 주소입니다.")).toBeVisible();
    await expect(page.locator("audio, .audio-timeline")).toHaveCount(0);
    await expect(page.locator(".header-filename")).toHaveText("새 세션");
  });
}

test("a signed-out permalink keeps its canonical target through Google login", async ({ page }) => {
  const api = await accountApi(page, null);
  const path = `/sessions/${"a".repeat(32)}`;
  await page.route("**/api/auth/google?*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("return_to")).toBe(path);
    api.switchTo("A");
    return route.fulfill({ status: 302, headers: { location: path } });
  });
  await page.goto(path);
  const login = page.getByRole("link", { name: "Google로 시작하기" });
  await expect(login).toHaveAttribute("href", `/api/auth/google?return_to=${encodeURIComponent(path)}`);
  expect(api.historyRequests()).toBe(0);
  await login.click();
  await expect(page).toHaveURL(new RegExp(`${path}$`));
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
});

for (const viewport of [
  { width: 1440, height: 900, band: "desktop" },
  { width: 1024, height: 768, band: "medium" },
  { width: 820, height: 1180, band: "medium" },
  { width: 390, height: 844, band: "mobile" },
]) {
  test(`${viewport.band} ${viewport.width}px defaults preserve the requested navigation and settings layout`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await accountApi(page, "A");
    await page.goto(`/sessions/${"a".repeat(32)}`);
    await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
    const sidebar = page.locator('aside[data-slot="root"]');
    await expect(sidebar).toHaveAttribute("data-state", viewport.band === "desktop" ? "expanded" : "collapsed");
    if (viewport.band === "mobile") {
      await expect(page.locator(".inspector")).toHaveCount(0);
      await expect(page.getByRole("button", { name: "속성 패널" })).toBeVisible();
      await page.getByRole("button", { name: "속성 패널" }).click();
      await expect(page.getByRole("dialog", { name: "악보 설정" })).toBeVisible();
    } else {
      await expect(page.locator(".inspector")).toBeVisible();
      await expect(page.locator(".inspector").getByRole("spinbutton", { name: "템포 BPM" })).toBeVisible();
      await expect(page.getByRole("button", { name: "속성 패널" })).toHaveCount(0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/session-layout-${viewport.width}.png`, fullPage: true });
  });
}

async function expectCollapsedMobileWorkspace(page: Page) {
  await expect(page.getByRole("region", { name: "드럼 비트 편집기" })).toBeVisible();
  await expect(page.locator(".inspector")).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "워크스페이스", exact: true })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "악보 설정", exact: true })).toHaveCount(0);
  await expect(page.locator('aside[data-slot="root"]')).toHaveAttribute("data-state", "collapsed");
}

test("desktop to tablet to mobile resize never opens navigation without a toggle", async ({ page }) => {
  await accountApi(page, "A");
  await page.goto(`/sessions/${"a".repeat(32)}`);
  await expect(page.locator('aside[data-slot="root"]')).toHaveAttribute("data-state", "expanded");
  await page.setViewportSize({ width: 820, height: 1180 });
  await expect(page.locator(".inspector")).toBeVisible();
  await expect(page.locator('aside[data-slot="root"]')).toHaveAttribute("data-state", "collapsed");
  await page.setViewportSize({ width: 390, height: 844 });
  await expectCollapsedMobileWorkspace(page);
  await page.getByRole("button", { name: "사이드바 토글" }).click();
  await expect(page.getByRole("dialog", { name: "워크스페이스", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expectCollapsedMobileWorkspace(page);
});

test("signing in again on tablet then resizing mobile keeps both panels closed", async ({ page }) => {
  const api = await accountApi(page, "A");
  const path = `/sessions/${"a".repeat(32)}`;
  await page.setViewportSize({ width: 820, height: 1180 });
  await page.route("**/api/auth/google?*", async (route) => {
    api.switchTo("A");
    return route.fulfill({ status: 302, headers: { location: path } });
  });
  await page.goto(path);
  await expect(page.locator(".inspector")).toBeVisible();
  await page.getByRole("button", { name: "로그아웃", exact: true }).click();
  await page.getByRole("link", { name: "Google로 시작하기" }).click();
  await expect(page.locator(".inspector")).toBeVisible();
  await expect(page.getByRole("dialog", { name: "워크스페이스", exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expectCollapsedMobileWorkspace(page);
  await page.screenshot({ path: "test-results/session-layout-auth-transition-mobile.png", fullPage: true });
});
