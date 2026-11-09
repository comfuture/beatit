import { expect, test } from "@playwright/test";
import { mockWorkspaceEvents } from "./workspaceEvents";

for (const mode of ["chunked", "direct"] as const) {
test(`a large video uses ${mode} transport, recovers an acknowledgement and shows publication progress`, async ({ page }) => {
  const id = "b".repeat(32);
  const chunkSize = 8 * 1024 ** 2;
  const source = Buffer.alloc(chunkSize + 7, 31);
  source.write("THE END", chunkSize);
  let offset = 0;
  let published = false;
  let completes = 0;
  const received: Buffer[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const job = {
    id, filename: "large-video.mp4", status: "queued", created: 1794225600, updated: 1794225600,
    has_result: false, result: null, request: { options: {} },
    progress: { stage: "queued", progress: 0, message: "분석 대기 중" },
  };
  await mockWorkspaceEvents(page, () => ({ jobs: published ? [job] : [], system: {
    ffmpeg: true, verovio: true, musescore: false, devices: ["cpu"], default_device: "cpu",
    max_upload_bytes: 1024 ** 3, upload_chunk_bytes: chunkSize, upload_mode: mode, max_duration_seconds: 1800,
  } }));
  const session = () => ({ id, size: source.length, offset, chunk_size: chunkSize,
    state: published ? "completed" : completes ? "finalizing" : "uploading" });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === "/api/auth/me") return route.fulfill({ json: { user: { id: "upload-user", name: "Upload User", email: "upload@example.test" } } });
    if (path === "/api/system") return route.fulfill({ json: {
      ffmpeg: true, verovio: true, musescore: false, devices: ["cpu"], default_device: "cpu",
      max_upload_bytes: 1024 ** 3, upload_chunk_bytes: chunkSize, upload_mode: mode, max_duration_seconds: 1800,
    } });
    if (path === "/api/jobs" && method === "GET") return route.fulfill({ json: published ? [job] : [] });
    if (path === `/api/jobs/${id}`) return route.fulfill({ json: job });
    if (path === "/api/uploads") {
      expect(route.request().postDataJSON()).toMatchObject({ filename: job.filename, size: source.length });
      return route.fulfill({ status: 201, json: session() });
    }
    if (path.endsWith("/complete")) {
      completes++;
      return route.fulfill({ status: 202, json: session() });
    }
    if (path === `/api/uploads/${id}` && method === "GET") return route.fulfill({ json: session() });
    if (path === `/api/uploads/${id}` && method === "PUT") {
      const data = route.request().postDataBuffer()!;
      expect(data).toEqual(source);
      received.push(data);
      offset = data.length;
      return route.fulfill({ status: 503, body: "upstream disconnected after upload" });
    }
    if (path === `/api/uploads/${id}` && method === "PATCH") {
      const data = route.request().postDataBuffer()!;
      expect(Number(route.request().headers()["upload-offset"])).toBe(offset);
      expect(data.length).toBeLessThanOrEqual(chunkSize);
      received.push(data);
      offset += data.length;
      // The first chunk was received, but its response was lost upstream.
      return route.fulfill(received.length === 1
        ? { status: 503, body: "upstream disconnected" }
        : { json: session() });
    }
    throw new Error(`Unexpected ${method} ${path}`);
  });
  await page.goto("/");
  await page.getByLabel("소스 파일 선택").setInputFiles({ name: job.filename, mimeType: "video/mp4", buffer: source });
  await page.getByRole("button", { name: "분석 시작" }).click();
  await expect(page.getByText("파일 저장 중", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "업로드 취소", exact: true })).toHaveCount(0);
  expect(Buffer.concat(received)).toEqual(source);
  expect(received).toHaveLength(mode === "direct" ? 1 : 2);
  expect(completes).toBe(1);
  published = true;
  await expect(page).toHaveURL(new RegExp(`/sessions/${id}$`));
  await expect(page.getByRole("heading", { name: "드럼 트랙을 분석하고 있습니다" })).toBeVisible();
  expect(errors).toEqual([]);
});

}
