import { createServer, type ServerResponse } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname } from "node:path";
import { expect, test } from "@playwright/test";

test("native EventSource applies progress and expiry without recurring snapshot requests", async ({ page }) => {
  const id = "c".repeat(32);
  const system = { devices: ["cpu"], default_device: "cpu", ffmpeg: true, verovio: true,
    musescore: false, max_upload_bytes: 1024 ** 3, max_duration_seconds: 1800 };
  const job = { id, filename: "Push progress.wav", status: "running", created: 1, updated: 1,
    has_result: false, result: null, progress: { stage: "separating", progress: 0.2, message: "Initial progress" } };
  const streams = new Set<ServerResponse>();
  const reads: Record<string, number> = {};
  const publicRoot = resolve(".output/public");
  const server = createServer(async (request, response) => {
    const path = new URL(request.url!, "http://localhost").pathname;
    const json = (value: unknown) => { response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value)); };
    if (path === "/api/events") {
      response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store", Connection: "keep-alive" });
      response.write(`event: snapshot\ndata: ${JSON.stringify({ jobs: [job], system })}\n\n`);
      streams.add(response);
      response.on("close", () => streams.delete(response));
      return;
    }
    if (path.startsWith("/api/")) reads[path] = (reads[path] || 0) + 1;
    if (path === "/api/auth/me") return json({ user: { id: "native-sse-user", name: "SSE User", email: "sse@example.test" } });
    if (path === "/api/system") return json(system);
    if (path === "/api/jobs") return json([job]);
    if (path === `/api/jobs/${id}`) return json(job);
    const file = resolve(publicRoot, path === "/" || path.startsWith("/sessions/") ? "index.html" : "." + path);
    if (!file.startsWith(publicRoot + "/")) { response.writeHead(404).end(); return; }
    try {
      const types: Record<string, string> = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };
      response.setHeader("Content-Type", types[extname(file)] || "application/octet-stream");
      response.end(await readFile(file));
    } catch { response.writeHead(404).end(); }
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const address = server.address() as { port: number };
  try {
    await page.clock.install();
    await page.goto(`http://127.0.0.1:${address.port}/sessions/${id}`);
    await expect(page.getByText("Initial progress", { exact: true })).toBeVisible();
    await expect.poll(() => streams.size).toBe(1);
    const baseline = { ...reads };
    await page.clock.runFor(120000);
    expect(reads).toEqual(baseline);
    const next = { ...job, updated: 2, progress: { stage: "transcribing", progress: 0.64, message: "Pushed progress" } };
    for (const stream of streams) stream.write(`event: job\ndata: ${JSON.stringify({ job: next })}\n\n`);
    await expect(page.getByText("Pushed progress", { exact: true })).toBeVisible();
    await expect(page.getByText("64%", { exact: true })).toBeVisible();
    expect(reads).toEqual(baseline);
    for (const stream of streams) stream.write("event: session-expired\ndata: {}\n\n");
    await expect(page.getByRole("link", { name: "Google로 로그인" })).toBeVisible();
    await expect(page.locator(".studio-shell")).toHaveCount(0);
    await expect.poll(() => streams.size).toBe(0);
  } finally {
    for (const stream of streams) stream.end();
    await new Promise<void>((done) => server.close(() => done()));
  }
});
