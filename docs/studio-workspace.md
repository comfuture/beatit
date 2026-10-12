# Beatit Studio workspace

The frontend lives in `frontend/` (Nuxt 4.6, Nuxt UI 4.11). `npm run build` generates
a static application shell and copies it into FastAPI's packaged static directory.
Audio, model inference, job persistence and score generation keep the existing
Python API. No Node server or external icon/font service is required at runtime.
Processing still requires the local backend; this is not a PWA.

## Workspace

- `USidebar`: searchable session history, new session and engine status. Collapses
  to a rail on desktop and opens as a slideover on small screens.
- `USplitter`: persistent widths for workspace/inspector and heights for audio/editor.
  The inspector moves into `USlideover` below 1100 px. The timeline and editor remain
  stacked, and the beat grid scrolls without overflowing the viewport.
- Import: validated single-file selection/drop, analysis settings, byte upload
  progress, actual queue status, cancellation and retry.
- Audio: actual peak envelope, click/keyboard seeking, original/drum/kit stem
  comparison at the same position, playback speed and volume.
- Editor: the published score grid, eight drum lanes, add/remove hits, new-hit
  velocity, per-bar navigation, playback follow, undo/redo and explicit save.
  Pickup cells before time zero and cells after audio end cannot create invalid hits.
- Score: page navigation, zoom, printing, SVG-backed preview, MusicXML/MIDI/PDF/ZIP
  exports. The saved score remains visible until regeneration publishes a new result.
- Inspector: actual BPM, meter, bars and duration; draft hit counts; score settings;
  model warnings; worker log and confirmed deletion. Unsaved changes require an
  explicit discard before switching sessions and trigger a browser leave warning.

`/api/jobs/{id}/waveform?source=audio.wav` returns at most 1200 amplitude peaks.
It only reads allowlisted job audio, reads bounded blocks, and caches by file
modification time and size. Large source files are not decoded into browser memory.

## References

The design uses Nuxt UI's [Dashboard template](https://ui.nuxt.com/templates),
[Sidebar showcase](https://ui.nuxt.com/docs/components/sidebar) and
[nested Splitter example](https://ui.nuxt.com/docs/components/splitter).
Adobe's [shared panel workspace](https://helpx.adobe.com/audition/desktop/workspace-and-setup/customizing-workspaces.html)
and [multitrack editor](https://helpx.adobe.com/audition/desktop/mixing-multitrack-sessions/multitrack-editor-overview.html)
informed the docked browser, compact transport, persistent inspector and separate
score/beat views. Instrument colors connect detected counts with editable lanes.
The layout also follows the distinction between the document, tools and properties
in [Photoshop's workspace](https://helpx.adobe.com/uk/photoshop/desktop/get-started/learn-the-basics/workspace-overview.html),
and the time ruler/playhead arrangement in
[Premiere's timeline](https://helpx.adobe.com/premiere/desktop/edit-projects/change-clip-sequence/navigation-controls-in-the-timeline.html).

## Verification

Run `npm run typecheck`, `npm test`, `npm run build` and `npm run test:e2e` in
`frontend/`. Playwright covers import, queue progress, cancellation/retry/failure,
editing, undo/redo, regeneration payloads, draft navigation, score/export, audio
transport/source switching, resizing and compact layouts with isolated API fixtures.
The Python API suite checks real waveform transients, channel handling, cache
invalidation and source restrictions. Existing stored sessions can additionally be
opened for actual waveform/SVG inspection without re-running model inference.

The implementation was also checked against the generated static app on FastAPI:
an isolated eight-second session accepted a new hit and a 120-to-126 BPM revision,
ran the real worker/Verovio regeneration, published a new generation with 17 hits,
and served the updated SVG and MusicXML download. Existing Wake session playback,
waveforms and four-page A4 printing were inspected separately. This does not
re-evaluate the accuracy of the transcription models or the full inference pipeline.
