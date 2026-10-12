import process from "node:process";

export default defineNuxtConfig({
  compatibilityDate: "2026-10-01",
  ssr: false,
  modules: ["@nuxt/ui"],
  css: ["~/assets/css/main.css"],
  devtools: { enabled: false },
  ui: { fonts: false },
  colorMode: { preference: "dark", fallback: "dark" },
  icon: {
    provider: "none",
    clientBundle: { scan: true, includeCustomCollections: true },
  },
  app: {
    head: {
      title: "Beatit — Drum Studio",
      htmlAttrs: { lang: "ko", class: "dark" },
      meta: [
        { name: "theme-color", content: "#141518" },
        {
          name: "description",
          content: "오디오에서 드럼 악보까지. 로컬 드럼 분석 및 편집 스튜디오.",
        },
      ],
    },
  },
  vite: {
    server: {
      proxy: {
        "/api": {
          target: process.env.BEATIT_API_URL || "http://127.0.0.1:8000",
          changeOrigin: false,
        },
      },
    },
  },
});
