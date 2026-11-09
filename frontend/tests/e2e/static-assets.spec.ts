import { expect, test } from "@playwright/test";

test("the production shell loads without rejected speculative asset requests", async ({ page }) => {
  const assets: Array<{ url: string; status: number }> = [];
  await page.route("**/api/auth/me", (route) => route.fulfill({ json: { user: null } }));
  await page.route("**/_nuxt/**", (route) => {
    // Reproduce Cloudflare Speed Brain's refusal of ineligible prefetches.
    if (route.request().headers()["sec-purpose"]?.includes("prefetch"))
      return route.fulfill({ status: 503, headers: { "cf-speculation-refused": "prefetch refused: not eligible" }, body: "" });
    return route.continue();
  });
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/_nuxt/"))
      assets.push({ url: response.url(), status: response.status() });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Beatit", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /Google/ })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(assets.some((asset) => asset.url.endsWith(".css"))).toBe(true);
  expect(assets.some((asset) => asset.url.endsWith(".js"))).toBe(true);
  expect(assets.filter((asset) => asset.status >= 400)).toEqual([]);
});
