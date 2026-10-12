import { cp, mkdir, rm } from "node:fs/promises";
const destination = new URL(
  "../../src/transcription/static/web/",
  import.meta.url,
);
await mkdir(destination, { recursive: true });
await rm(destination, { recursive: true, force: true });
await cp(new URL("../.output/public/", import.meta.url), destination, {
  recursive: true,
});
console.log("Nuxt app installed in src/transcription/static/web");
