# Drum Score

A local web app that transcribes drums from audio or video into editable **MusicXML**,
printable **SVG**, and **A4 PDF** when MuseScore is installed. Drop a file into the
browser, start transcription, then preview, adjust, and download the score.
MIDI, detected onsets, a separated drum track, and per-instrument kit stems are also
available.

**Only use recordings you own the rights to, or have explicit permission to process
and transcribe.** You are responsible for obtaining the necessary rights to the input
recording and for any use or sharing of the resulting score.

## Pipeline

```text
Audio/video → FFmpeg → Demucs drum stem → DrumSep kit stems → ADTOF onsets
            → Beat This! beats/downbeats → optional high/low tom refinement
            → rhythm grid → MusicXML → MuseScore SVG/PDF
```

Nuxt 4 and Nuxt UI provide a dark audio workspace with a session sidebar, resizable
waveform/editor/inspector panels, source/stem playback, and score preview. The beat
editor can audition its selected bar once. Independent Audio and Drum Beat toggles
play the selected source, the current draft's synthesized drum pattern, or both
together, with volume buttons opening separate vertical sliders for comparison; unsaved edits
are audible without exporting MIDI. Drag across the waveform
to select a playback range, then use **선택구간 해제** beside the selected time range
to return to full-song playback. **구간 반복** repeats a selected range with both
enabled tracks; drum edits become audible on the next pass. Editing includes per-bar hits, velocity for new hits,
undo/redo, and explicit
score regeneration. Suspect beats have a translucent red background and an
explanation: review the recording, edit if needed, then resolve each beat.
Resolutions support undo/redo and are saved with score regeneration.
See [review rules](docs/studio-workspace.md#transcription-review) for detection limits.
FastAPI serves the generated UI and a single job queue.
Demucs uses `htdemucs`. The MDX23C
DrumSep model splits the drums into kick, snare, toms, hi-hat, and cymbal stems. ADTOF,
the pinned PyTorch Frame_RNN port, runs on the drum stem and each kit stem; their scores
are averaged per class. Hi-hat decay marks open hi-hats. Beat This! sets tempo, beat
phase, and the first downbeat.
Verovio provides SVG output when MuseScore is unavailable. Processing stays on your
host; the first run may download model weights.

## Install and run

Requires Git, Node.js 22.12+ (24 LTS recommended), FFmpeg, and
[uv](https://docs.astral.sh/uv/getting-started/installation/).
The project selects Python 3.12. MuseScore is optional for PDF export.

```sh
git clone https://github.com/comfuture/beatit.git
cd beatit
npm --prefix frontend ci
npm --prefix frontend run build
```

### macOS

```sh
brew install uv ffmpeg
brew install --cask musescore  # optional: SVG and PDF engraving
uv sync --frozen
uv run drum-score serve
```

### Linux CPU

Install uv using the link above, then:

```sh
sudo apt-get install ffmpeg git
uv sync --frozen --extra cpu
uv run --extra cpu drum-score serve
```

Linux ARM64 also needs a Rust toolchain and `build-essential pkg-config libopus-dev`
to build the audio dependency. On Linux x86_64 with a compatible NVIDIA driver,
replace `--extra cpu` with `--extra cuda` in both commands (CUDA 12.8 wheels).
Apple Silicon uses PyTorch MPS; Intel Macs use CPU. MLX inference is not implemented.

Open **http://127.0.0.1:8000**, import a source into a new session, and start analysis.
The build installs the static Nuxt app in `src/transcription/static/web`; rebuild
after frontend changes. Node is only needed for building, not serving. Docker builds
the frontend automatically. Build the frontend before packaging a Python wheel.
Jobs and media are saved in the ignored `data/` directory. Run
`uv run drum-score doctor` to check the host, or `uv run drum-score download-model`
to fetch the Demucs, DrumSep, and Beat This! weights before processing. Use the selected
extra on Linux.

## Limitations and development

This is an experimental transcription tool. Verify and edit every score against
the recording. The first bar follows detected downbeats, but the meter is the selected
value, and a fixed tempo/grid approximates timing. Experimental high/low tom refinement
can reuse cached kit stems after transcription; ambiguous hits retain the generic Tom
label. Individual drum sizes, ride/crash cymbals, rimshots, cross-sticks, and ghost notes
are not distinguished. Open hi-hat detection favours precision.
See [tom refinement](docs/tom-refinement.md) for its limits.

```sh
uv sync --frozen --extra dev
uv run pytest -q
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend exec playwright install chromium
npm --prefix frontend run test:e2e
```

For frontend development, keep FastAPI on port 8000 and run
`npm --prefix frontend run dev`. Nuxt proxies `/api` to FastAPI without changing the
browser origin. Set `BEATIT_API_URL` for a different backend port. The browser tests
use isolated API fixtures; they do not modify existing sessions or download models.
See [workspace design and verification](docs/studio-workspace.md).

See [detailed usage and Docker setup](docs/usage.md),
[persistent server deployment](docs/usage.md#서버에-계속-실행하기),
[architecture and upstream license notes](docs/architecture.md), and the
[first](docs/experiment.md) / [second](docs/experiment-02.md) /
[third](docs/experiment-03.md) experiment reports.
Review upstream code and model licensing before redistribution or commercial use.
