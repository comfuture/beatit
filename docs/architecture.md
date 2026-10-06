# Architecture and upstream review

Reviewed on 2026-10-05 against the upstream repositories.

`media → FFmpeg PCM → Demucs drum stem → DrumSep kit stems → ADTOF activations (kit + stems) → onset events → Beat This! beats/downbeats → rhythm grid → MusicXML → MuseScore SVG`

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
- DrumSep: jarredou's MDX23C 5-stem model (kick, snare, toms, hh, cymbals). The network
  is a pure PyTorch port of the MIT-licensed MDX23C code in
  [xavriley/mdx23c-drum-separation](https://github.com/xavriley/mdx23c-drum-separation)
  (commit `80ae44e`) and matches it exactly on one model chunk. Weights come from the
  [xavriley/source_separation_mirror](https://huggingface.co/xavriley/source_separation_mirror)
  Hugging Face revision `da6dc5a` and are verified by SHA-256 and strict loading. The
  mirror is tagged MIT, but the model author has not published weight license terms;
  treat redistribution rights as unverified. The 6-stem crash/ride variant is no longer
  available from its original release and is not used.

## Transcription

- [MZehren/ADTOF](https://github.com/MZehren/ADTOF) recommends
  [xavriley/ADTOF-pytorch](https://github.com/xavriley/ADTOF-pytorch) as an alternative.
- The PyTorch port is pinned to commit `85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9`.
  It bundles converted Frame_RNN weights and avoids TensorFlow, Keras and madmom.
- Classes, in model order: GM 35 kick, 38 snare, 47 tom, 42 hi-hat, 49 cymbal.
  ADTOF runs on the full drum stem and on each DrumSep stem; each class averages the kit
  activation with its own stem's activation, using per-class threshold scales chosen on
  MDB Drums. Stems alone lowered onset F-measure (0.79 vs 0.86); the average raised it to
  0.88 under two-fold cross-validation.
- Open hi-hat (GM 46) comes from the hi-hat stem loudness decay. MIDI velocity comes from
  stem loudness relative to each instrument's loud hits. Toms, ride/crash and ghost notes
  are not separated. Model activations are detection scores, not calibrated probabilities.
- The port has no explicit standalone LICENSE file in the reviewed commit. The
  original ADTOF repository is CC BY-NC-SA 4.0. Do not assume unrestricted commercial
  rights to the port or weights; obtain upstream clarification before distribution
  for commercial use. The app does not vendor upstream source or model weights.

## Beat tracking

- [CPJKU/beat_this](https://github.com/CPJKU/beat_this) 1.1.0, code and weights MIT.
  The `final0` checkpoint is downloaded into the PyTorch hub cache and verified by
  SHA-256. Its published training annotations do not include MDB Drums.
- On MDB Drums, beat F-measure rose from 0.72 (Librosa on the drum stem) to 0.93. Librosa
  placed beats about 32 ms late; Beat This! about 10 ms late, then on-beat drum onsets
  correct the remaining phase.
- The mix and the drum stem are both tracked; the one whose beats fit a steady tempo
  best sets the BPM. Downbeats from the mix place bar 1, and earlier hits form a pickup.

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
