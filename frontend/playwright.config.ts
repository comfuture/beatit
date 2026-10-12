import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/e2e",
  use: {
    baseURL: process.env.BEATIT_WEB_URL || "http://127.0.0.1:3100",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: process.env.BEATIT_WEB_URL
    ? undefined
    : {
        // CI builds first; local runs build before serving the same static output.
        // A ready dev-server URL does not mean its first client compile is ready.
        command: process.env.CI
          ? "npm run preview:e2e"
          : "npm run build && npm run preview:e2e",
        url: "http://127.0.0.1:3100",
        reuseExistingServer: false,
      },
  fullyParallel: true,
  reporter: "list",
});
