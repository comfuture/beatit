import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import ts from "typescript";

// Run the production synthesizer through Chromium's actual audio renderer.
// The class has no runtime imports; transpilation only removes TypeScript types.
const compiled = ts.transpileModule(
  readFileSync(new URL("../../app/utils/drumPlayback.ts", import.meta.url), "utf8"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } },
).outputText;
const source = compiled.slice(compiled.indexOf("export class DrumSynth"))
  .replace("export class DrumSynth", "class DrumSynth") + "\nreturn DrumSynth;";

test("synthesized kit renders distinct, finite sounds with velocity and hi-hat choking", async ({ page }) => {
  await page.goto("/");
  const rendered = await page.evaluate(async (source) => {
    const DrumSynth = new Function(source)();
    const render = async (pitch: number, velocity = 100, choke = false) => {
      const context = new OfflineAudioContext(1, 44100 * 2, 44100);
      const synth = new DrumSynth(context);
      synth.output.connect(context.destination);
      synth.hit({ pitch, velocity }, 0.05);
      if (choke) synth.hit({ pitch: 42, velocity: 100 }, 0.15);
      const buffer = await context.startRendering();
      const data = buffer.getChannelData(0);
      const rms = (from: number, to: number) => {
        const samples = data.slice(Math.round(from * 44100), Math.round(to * 44100));
        return Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
      };
      return {
        pitch, peak: data.reduce((peak, sample) => Math.max(peak, Math.abs(sample)), 0),
        early: rms(0.05, 0.1), tail: rms(0.25, 0.35), ending: rms(1.7, 2),
        finite: data.every(Number.isFinite),
      };
    };
    return {
      kit: await Promise.all([35, 38, 45, 47, 50, 42, 46, 49].map((pitch) => render(pitch))),
      quiet: await render(38, 32), loud: await render(38, 127),
      open: await render(46), choked: await render(46, 100, true),
    };
  }, source);
  for (const drum of rendered.kit) {
    expect(drum.finite).toBe(true);
    expect(drum.early).toBeGreaterThan(0.001);
    expect(drum.peak).toBeLessThanOrEqual(1);
    expect(drum.ending).toBe(0);
  }
  expect(new Set(rendered.kit.map((drum) => drum.early.toFixed(5))).size).toBe(8);
  expect(rendered.loud.early).toBeGreaterThan(rendered.quiet.early * 3);
  expect(rendered.choked.tail).toBeLessThan(rendered.open.tail * 0.1);
});
