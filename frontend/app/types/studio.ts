export type Pitch = 35 | 38 | 45 | 47 | 50 | 42 | 46 | 49;
export interface DrumEvent {
  time: number;
  pitch: Pitch;
  strength: number;
  velocity?: number | null;
  tom_source_pitch?: 47 | null;
  tom_match_score?: number | null;
}
export interface Options {
  bpm: number | null;
  offset: number | null;
  meter: string;
  grid: string;
  device: string;
  renderer: string;
  source: string;
  sensitivity: number;
  tom_refinement: boolean;
}
export interface Grid {
  bpm: number;
  offset: number;
  meter: string;
  bars: number;
  bar_ticks: number;
  step: number;
  mean_quantization_error_ms: number;
}
export interface Result {
  generation: string;
  duration: number;
  options: Options;
  grid: Grid;
  counts: Record<string, number>;
  event_count: number;
  pages: string[];
  pdf: boolean;
  renderer: string;
  transcription_device: string;
  stems?: string[];
  warnings: string[];
  tom_refinement?: { enabled: boolean };
}
export interface Job {
  id: string;
  filename: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  created: number;
  updated: number;
  error?: string;
  has_result: boolean;
  progress: {
    stage: string;
    progress: number;
    message: string;
    elapsed_seconds?: number;
  };
  result?: Result;
  request?: { options: Options };
}
export interface SystemInfo {
  devices: string[];
  default_device: string;
  ffmpeg: boolean;
  musescore: boolean;
  verovio: boolean;
  max_upload_bytes: number;
  max_duration_seconds: number;
}
export const defaultOptions = (): Options => ({
  bpm: null,
  offset: null,
  meter: "4/4",
  grid: "16",
  device: "auto",
  renderer: "auto",
  source: "mix",
  sensitivity: 1,
  tom_refinement: false,
});
export const kit: {
  pitch: Pitch;
  name: string;
  short: string;
  color: string;
}[] = [
  { pitch: 49, name: "Cymbal", short: "CY", color: "#c8ac72" },
  { pitch: 46, name: "Open hi-hat", short: "OH", color: "#bd9ce2" },
  { pitch: 42, name: "Hi-hat", short: "HH", color: "#9e8dcb" },
  { pitch: 50, name: "High tom", short: "HT", color: "#81adc8" },
  { pitch: 47, name: "Tom", short: "TM", color: "#78a1c4" },
  { pitch: 45, name: "Low tom", short: "LT", color: "#6589b0" },
  { pitch: 38, name: "Snare", short: "SN", color: "#df987c" },
  { pitch: 35, name: "Kick", short: "KD", color: "#62cbb3" },
];
export const statusLabels = {
  queued: "대기 중",
  running: "분석 중",
  completed: "완료",
  failed: "실패",
  cancelled: "취소됨",
};
