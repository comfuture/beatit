# Drum Score

A local web app that transcribes drums from audio or video into editable **MusicXML**,
printable **SVG**, and **A4 PDF** when MuseScore is installed. Drop a file into the
browser, start transcription, then preview, adjust, and download the score.
MIDI, detected onsets, and a separated drum track are also available.

**Only use recordings you own the rights to, or have explicit permission to process
and transcribe.** You are responsible for obtaining the necessary rights to the input
recording and for any use or sharing of the resulting score.

## Pipeline

```text
Audio/video → FFmpeg → Demucs drum stem → DrumSep kit stems → ADTOF onsets
            → Beat This! beats/downbeats + rhythm grid → MusicXML → MuseScore SVG/PDF
```

FastAPI serves the web UI and a single job queue. Demucs uses `htdemucs`. The MDX23C
DrumSep model splits the drums into kick, snare, toms, hi-hat, and cymbal stems. ADTOF,
the pinned PyTorch Frame_RNN port, runs on the drum stem and each kit stem; their scores
are averaged per class. Hi-hat decay marks open hi-hats. Beat This! sets tempo, beat
phase, and the first downbeat.
Verovio provides SVG output when MuseScore is unavailable. Processing stays on your
host; the first run may download model weights.

## Install and run

Requires Git, FFmpeg, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The project selects Python 3.12. MuseScore is optional for PDF export.

```sh
git clone https://github.com/comfuture/beatit.git
cd beatit
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

Open **http://127.0.0.1:8000**, upload a file, and click the start button.
Jobs and media are saved in the ignored `data/` directory. Run
`uv run drum-score doctor` to check the host, or `uv run drum-score download-model`
to fetch the Demucs, DrumSep, and Beat This! weights before processing. Use the selected
extra on Linux.

## Limitations and development

This is an experimental transcription tool. Verify and edit every score against
the recording. The first bar follows detected downbeats, but the meter is the selected
value, and a fixed tempo/grid approximates timing. Individual toms, ride/crash cymbals,
and ghost notes are not distinguished; open hi-hat detection favours precision.

```sh
uv sync --frozen --extra dev
uv run pytest -q
```

See [detailed usage and Docker setup](docs/usage.md),
[architecture and upstream license notes](docs/architecture.md), and the
[first](docs/experiment.md) / [second](docs/experiment-02.md) /
[third](docs/experiment-03.md) experiment reports.
Review upstream code and model licensing before redistribution or commercial use.
