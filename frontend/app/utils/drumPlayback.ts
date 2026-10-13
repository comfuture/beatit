import type { DrumEvent, Grid, Pitch } from "../types/studio";
import { eventTick, tickTime } from "./rhythm";

export interface PlaybackHit { time: number; pitch: Pitch; velocity: number }
export interface PlaybackRange { start: number; end: number; bar?: number }

export function timePlaybackRange(first: number, last: number, duration: number): PlaybackRange | null {
  if (![first, last, duration].every(Number.isFinite) || duration <= 0) return null;
  const start = Math.max(0, Math.min(duration, Math.min(first, last)));
  const end = Math.max(0, Math.min(duration, Math.max(first, last)));
  return end - start >= 0.01 ? { start, end } : null;
}

export function barPlaybackRange(bar: number, grid: Grid, duration: number): PlaybackRange | null {
  const selected = Math.max(1, Math.min(grid.bars, Math.round(bar)));
  const start = Math.max(0, tickTime((selected - 1) * grid.bar_ticks, grid));
  const end = Math.min(duration, tickTime(selected * grid.bar_ticks, grid));
  return end > start ? { start, end, bar: selected } : null;
}

// Match score.mid quantization and strongest-hit/hi-hat merging, but read the
// current draft so edits are audible without generating or downloading a MIDI.
export function playbackHits(events: DrumEvent[], grid: Grid, duration: number): PlaybackHit[] {
  const hits = new Map<string, { hit: PlaybackHit; strength: number }>();
  const halfStep = (grid.step / 24) * 60 / grid.bpm;
  for (const event of events) {
    if (event.time < grid.offset - halfStep) continue;
    const tick = eventTick(event, grid);
    const time = tickTime(tick, grid);
    if (time < 0 || time >= duration) continue;
    const key = `${tick}:${event.pitch === 46 ? 42 : event.pitch}`;
    if (!hits.has(key) || event.strength > hits.get(key)!.strength) {
      hits.set(key, {
        strength: event.strength,
        hit: { time, pitch: event.pitch, velocity: event.velocity ?? Math.round(event.strength * 110 + 17) },
      });
    }
  }
  return [...hits.values()].map(({ hit }) => hit).sort((a, b) => a.time - b.time || a.pitch - b.pitch);
}

export function firstPlaybackHit(hits: PlaybackHit[], position: number) {
  let low = 0;
  let high = hits.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (hits[middle]!.time < position - 0.001) low = middle + 1;
    else high = middle;
  }
  return low;
}

interface Voice {
  pitch: Pitch;
  start: number;
  gain: GainNode;
  sources: AudioScheduledSourceNode[];
}

/** Small local percussion kit; no samples, MIDI soundfont or network dependency. */
export class DrumSynth {
  readonly output: GainNode;
  private noise: AudioBuffer;
  private voices = new Set<Voice>();

  constructor(private context: BaseAudioContext) {
    this.output = context.createGain();
    this.output.gain.value = 0.65;
    this.noise = context.createBuffer(1, context.sampleRate * 2, context.sampleRate);
    const data = this.noise.getChannelData(0);
    // Repeatable noise makes rendered-audio regression checks reproducible.
    let seed = 123456789;
    for (let i = 0; i < data.length; i++) {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      data[i] = seed / 2147483648 - 1;
    }
  }

  hit(hit: PlaybackHit, when: number) {
    const context = this.context;
    const start = Math.max(context.currentTime, when);
    const length = ({ 35: 0.35, 38: 0.22, 45: 0.42, 47: 0.35, 50: 0.28, 42: 0.065, 46: 0.55, 49: 1.3 })[hit.pitch];
    const gain = context.createGain();
    const level = (Math.max(1, Math.min(127, hit.velocity)) / 127) ** 1.3;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(level, start + 0.002);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + length);
    gain.connect(this.output);
    const voice: Voice = { pitch: hit.pitch, start, gain, sources: [] };
    this.voices.add(voice);
    const tone = (frequency: number, end: number, amplitude: number, type: OscillatorType = "sine") => {
      const oscillator = context.createOscillator();
      oscillator.type = type;
      oscillator.frequency.setValueAtTime(frequency, start);
      oscillator.frequency.exponentialRampToValueAtTime(end, start + length * 0.7);
      const mix = context.createGain();
      mix.gain.value = amplitude;
      oscillator.connect(mix).connect(gain);
      oscillator.start(start);
      oscillator.stop(start + length);
      voice.sources.push(oscillator);
      oscillator.onended = () => { oscillator.disconnect(); mix.disconnect(); };
    };
    const noise = (frequency: number, amplitude: number, type: BiquadFilterType) => {
      const source = context.createBufferSource();
      source.buffer = this.noise;
      const filter = context.createBiquadFilter();
      filter.type = type;
      filter.frequency.value = frequency;
      const mix = context.createGain();
      mix.gain.value = amplitude;
      source.connect(filter).connect(mix).connect(gain);
      source.start(start);
      source.stop(start + length);
      voice.sources.push(source);
      source.onended = () => { source.disconnect(); filter.disconnect(); mix.disconnect(); };
    };
    switch (hit.pitch) {
      case 35: tone(145, 42, 0.85); break;
      case 38: tone(185, 95, 0.22, "triangle"); noise(1100, 0.75, "highpass"); break;
      case 45: tone(130, 65, 0.65); noise(800, 0.12, "lowpass"); break;
      case 47: tone(175, 90, 0.6); noise(1100, 0.12, "lowpass"); break;
      case 50: tone(235, 125, 0.55); noise(1400, 0.1, "lowpass"); break;
      case 42:
      case 46:
        // A later hat closes any ringing open hat, including scheduled voices.
        for (const earlier of this.voices) {
          if (earlier !== voice && earlier.pitch === 46 && earlier.start <= start) {
            earlier.gain.gain.cancelAndHoldAtTime(start);
            earlier.gain.gain.setTargetAtTime(0.0001, start, 0.003);
          }
        }
        noise(6500, hit.pitch === 42 ? 0.35 : 0.3, "highpass");
        break;
      case 49:
        noise(4200, 0.36, "highpass");
        tone(3100, 2600, 0.025, "square");
        tone(4373, 4100, 0.02, "square");
        break;
    }
    const last = voice.sources.at(-1)!;
    const cleanup = last.onended;
    last.onended = (event) => {
      cleanup?.call(last, event);
      gain.disconnect();
      this.voices.delete(voice);
    };
  }

  stop() {
    for (const voice of this.voices) {
      voice.gain.gain.cancelScheduledValues(this.context.currentTime);
      voice.gain.gain.setValueAtTime(0, this.context.currentTime);
      for (const source of voice.sources) source.stop();
      voice.gain.disconnect();
    }
    this.voices.clear();
  }

  dispose() { this.stop(); this.output.disconnect(); }
}
