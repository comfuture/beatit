# Architecture and upstream review

Reviewed on 2026-10-05 against the upstream repositories.

`media → FFmpeg PCM → Demucs drum stem → ADTOF activations → onset events → rhythm grid → MusicXML → MuseScore SVG`

The application uses FastAPI and a static, accessible browser UI. Python 3.12 and a
locked uv environment keep the web server and both inference models in one runtime.
Model work runs in a separate process, with one queued job at a time to bound GPU
memory. SQLite stores job metadata; each job has its own media and output directory.

## Source separation

- [adefossez/demucs](https://github.com/adefossez/demucs), PyPI 4.1.0, MIT.
- The original Meta repository is archived and points to this maintainer's fork.
- `htdemucs` is the default. Demucs 4.1 uses sphn/FFmpeg rather than torchaudio for
  inference decoding. NumPy is declared explicitly here because it is needed on
  every platform. Intel Macs use PyTorch 2.2.2 and NumPy 1.x.
- Exporting only the drums does not make the network itself a two-source model.

## Transcription

- [MZehren/ADTOF](https://github.com/MZehren/ADTOF) recommends
  [xavriley/ADTOF-pytorch](https://github.com/xavriley/ADTOF-pytorch) as an alternative.
- The PyTorch port is pinned to commit `85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9`.
  It bundles converted Frame_RNN weights and avoids TensorFlow, Keras and madmom.
- Classes, in model order: GM 35 kick, 38 snare, 47 tom, 42 hi-hat, 49 cymbal.
  Open/closed hi-hat, different toms, ride/crash and articulations are not separately
  inferred. Model activations are detection scores, not calibrated probabilities.
- The port has no explicit standalone LICENSE file in the reviewed commit. The
  original ADTOF repository is CC BY-NC-SA 4.0. Do not assume unrestricted commercial
  rights to the port or weights; obtain upstream clarification before distribution
  for commercial use. The app does not vendor upstream source or model weights.

## Hardware

PyTorch selects CUDA on compatible Linux hosts, MPS on Apple Silicon, otherwise CPU.
MPS may need CPU fallback for operations such as complex STFT; this is exposed in
the job diagnostics. CPU remains a supported full pipeline. MLX availability is
reported independently: neither selected upstream model ships a verified MLX backend,
so installing MLX does not enable an MLX inference path in this application.

## Engraving

- [MuseScore Studio CLI](https://handbook.musescore.org/appendix/command-line-usage)
  imports MusicXML and exports SVG/PDF/MS CZ via converter mode. It is an external
  GPL application. Linux uses the offscreen Qt platform.
- [Verovio](https://book.verovio.org/toolkit-reference/input-formats.html), LGPL, is
  an explicit fallback when MuseScore is absent. It converts the same MusicXML to
  printable SVG without a desktop installation. Results identify the actual renderer.
- MusicXML uses `unpitched`, percussion clef, separate stem directions/voices and
  General MIDI channel 10. Rhythm inference and engraving are separate steps.
