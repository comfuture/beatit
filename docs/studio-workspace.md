# Beatit Studio workspace

The frontend lives in `frontend/` (Nuxt 4.6, Nuxt UI 4.11). `npm run build` generates
a static application shell and copies it into FastAPI's packaged static directory.
Audio, model inference, job persistence and score generation keep the existing
Python API. No Node server or external icon/font service is required at runtime.
Processing still requires the local backend; this is not a PWA.

## Workspace

- `USidebar`: New, Open, session information and engine status. **불러오기**
  opens a searchable file-style session picker with selection and an Open action.
  The sidebar collapses to a rail on desktop and opens as a slideover on small
  screens. The collapsed **ⓘ** popover exposes session information on demand.
- `USplitter`: persistent widths for workspace/inspector and heights for audio/editor.
  The inspector moves into `USlideover` below 1100 px. The timeline and editor remain
  stacked, and the beat grid scrolls without overflowing the viewport.
  The audio pane reserves at least 72 px for the waveform plus the measured height
  of its source selector, transport and status rows. Its pixel minimum adapts to
  wrapped controls and range selection; the editor retains at least 240 px.
  Short windows scroll the workspace instead of hiding the waveform or controls.
- Import: validated single-file selection/drop, analysis settings, byte upload
  progress, actual queue status, cancellation and retry.
- Audio: actual peak envelope, click/keyboard seeking, original/drum/kit stem
  comparison at the same position, playback speed and volume.
  During playback, animation frames sample the audio element's media clock for
  view synchronization; native time updates remain as a background-tab fallback.
  The frame loop stops on pause, buffering, source changes and unmount.
- Editor: the published score grid, eight drum lanes, add/remove hits, new-hit
  velocity, per-bar navigation, playback follow, undo/redo and explicit save.
  Pickup cells before time zero and cells after audio end cannot create invalid hits.
  Playback highlights the entire numbered beat across the ruler, kit and velocity
  lanes (four sixteenth-note cells in 4/4), holding the band until the next beat.
  Hit colors remain visible inside the band; the edit grid stays unchanged.
- Score: page navigation, zoom, printing, SVG-backed preview, MusicXML/MIDI/PDF/ZIP
  exports. The saved score remains visible until regeneration publishes a new result.
  The current audio bar is highlighted using the saved grid's tempo, offset and
  meter. Playback follow scrolls the score panel and changes pages automatically;
  manual page navigation pauses follow, and the switch resumes it at the current
  audio position. Highlight overlays do not appear in print or exported files.
  All export links use native browser downloads, bypassing Nuxt routing so the
  workspace URL, browser history and unsaved edits remain unchanged.
  Clicking a measure (or focusing it and pressing Enter/Space) seeks the audio to
  its saved-grid start, clamped to zero for pickup bars, while preserving the
  playback and follow states. Measure targets scale with the page zoom.
- Inspector: rhythm settings, score title and author with an explicit Apply
  action, also on mobile.
  The title initially uses the source filename without its extension, then is stored
  independently so naming a score never renames its source session. The author can
  be cleared. Heading changes participate in the unsaved-change guard and the main
  save action. Applying headings alone preserves unsaved rhythm edits and undo history.
  The inspector also provides model warnings, the worker log and confirmed deletion.
  Unsaved changes require an
  explicit discard before switching sessions and trigger a browser leave warning.

The inspector keeps score headings and rhythm settings visible. The left sidebar
shows the saved tempo, meter, bar count and duration alongside the current draft's
total hit count. Its **통계** button opens per-instrument hit counts, also available
inside the collapsed information popover. In the editor, the bottom-right **?**
popover contains cell shortcuts and new-hit velocity. Suspect-beat navigation appears only while reviews are pending;
the grid colors and per-beat reasons carry the review state without a standing banner.

`/api/jobs/{id}/waveform?source=audio.wav` returns at most 1200 amplitude peaks.
It only reads allowlisted job audio, reads bounded blocks, and caches by file
modification time and size. Large source files are not decoded into browser memory.

`/api/jobs/{id}/score-layout?generation=...` maps generated SVG staff/barline
geometry to normalized measure bounds, including existing MuseScore and Verovio
results. It caches by file metadata and verifies the complete measure count against
the saved grid. Unsupported layouts remain viewable with follow disabled. Images
stay in `<img>` elements; score SVG markup is never injected into the app DOM.

`POST /api/jobs/{id}/score-metadata` queues a heading-only engraving from the saved
MusicXML. It preserves notation, tempo, MIDI and event data without audio analysis
or quantization. The worker publishes new SVG pages, MusicXML, analysis and ZIP
together after rendering succeeds; MuseScore also regenerates the PDF. Verovio
continues to offer SVG printing instead of PDF export. Failed rendering leaves the
published files intact and supports retry. Explicit MusicXML title/composer credits
make the heading visible in both renderers. Normal rhythm revisions retain metadata.

## Bar playback and drum audition

The transport has independent **Audio** and **Drum Beat** toggles. Audio plays the
selected SOURCE (original, separated drums or a kit stem); Drum Beat synthesizes
the current draft with Web Audio. Both can be enabled for comparison. Turning both
off pauses playback. Position, speed, pause and stop are shared. One shared mixer
button shows both percentages; disabled tracks are dimmed. On mobile, repeat and
mixer controls show icons only, retaining their accessible names.
Clicking it opens two side-by-side columns, each with a track toggle and a vertical
**오디오 볼륨** or **드럼비트 볼륨** slider (0–100%, initially 80%). Escape or an
outside click closes the popover, and keyboard/touch adjustment works on desktop
and mobile. Levels change immediately, including during repeat playback,
without restarting the pass or applying pending note edits early. Zero silences
that track while its toggle remains on. Toggling tracks, changing SOURCE and
repeating retain the chosen levels for the current session. Separate Web Audio
gains smooth level changes before the output gates and shared compressor.

**구간재생** in the beat editor plays the selected bar once by default and returns
to its start. It clamps pickup bars and the final partial bar to the available audio,
uses the saved grid's actual meter/tempo, and holds the editor on the selected bar.
The waveform highlights the active range. Play resumes inside that range; Stop
returns to its start. **선택구간 해제** appears only when a range is selected,
beside its time range in the highlighted selection row below the transport. It
restores full-song playback without moving the playhead or pausing an active
audition, then disappears and returns keyboard focus to the waveform. Without a
selection, the status explicitly says 전체 재생.
Seeking outside the range also clears it. Choosing a bar manually turns off playback-follow so
the selection is not immediately overwritten by the moving playhead.

Drag across the waveform to choose an arbitrary time range. The selection is
previewed while dragging and committed on release, with either drag direction
supported. Pointer capture clips an out-of-waveform release to the audio bounds;
touch uses the same selection behavior. A click or tiny pointer movement only
seeks. Selection moves the playhead to its start, retaining the previous playing
or paused state, and applies equally to both playback tracks. Escape cancels an
unfinished drag or clears a committed selection; Shift+Left/Right expands a
range by one second for keyboard users. The waveform includes a visible gesture
hint. Changing SOURCE retains the chosen time range.

Clicking or keyboard-seeking on the waveform reveals the corresponding editor
bar; committing a drag reveals the bar at the selected range's start, including
reverse drags. This explicit navigation works while paused or playing, even with
**재생 따라가기** off, and leaves that switch unchanged. Playback-follow controls
automatic bar changes as playback advances. Re-selecting the same time or range
also reveals its bar after manual editor navigation; a cancelled drag does not.

**구간 반복** in the playback toolbar repeats the selected bar or waveform range.
It is off by default and disabled without a selection. Both enabled tracks restart
at the same range start; the drums wait for media seeking to complete when audio
is enabled. Turning repetition off lets the current pass finish normally. Pause
holds the position and Stop returns to the range start; both retain the selection
and repeat setting. Clearing the range or seeking outside it also turns repeat off.

Each loop pass uses a snapshot of the draft drum notes. Edits and undo/redo during
that pass are marked **수정 내용은 다음 반복부터 반영됩니다.** and become audible
on the next pass, without saving or generating MIDI. Source, speed and track
changes preserve the current pass's notes. Explicit seeking or starting playback
again uses the latest draft, as does turning repeat off with pending edits.
Outside repeat mode, draft edits continue to update playback immediately.

With Audio enabled, the media element's clock drives both tracks. Buffering or
source changes suspend drum scheduling until the source resumes. With Audio off,
the AudioContext clock drives the drums, including when the source is unavailable.
Short lookahead scheduling keeps synthesized notes aligned through speed changes.
Both output gates are scheduled on the audio clock at the region end, cutting
source audio and drum tails even if browser UI timers run late. A shared compressor
controls peaks when both tracks are mixed. Pause, seek, edits and session teardown
cancel obsolete scheduled voices; unmounting closes the context.

The local synthesized kit covers all eight lanes, with velocity-sensitive kick,
snare, three tom pitches, closed/open hi-hat and cymbal sounds. Closed hats choke
ringing open hats. These are synthesized audition sounds, not realistic recorded
kit samples. The editor uses current events and the saved grid, including unsaved
edits and undo/redo. Note quantization and strongest-hit/hi-hat merging follow
`score.mid`. The exported MIDI remains a saved-score artifact for external players;
the editor does not load it for playback because it cannot contain pending edits.

Tests cover range bounds, pickup/odd meters, quantization, source switching, mixed
audio signals, drum-only playback with unavailable media, unsaved edits, synchronized
note timing, stop/restart, mobile controls and silent outputs after a range ends.
An OfflineAudioContext test renders the production kit and checks non-silent finite
waveforms, velocity response, different voices, release and hi-hat choking.

## Transcription review

The editor treats automatic transcription as a draft. It highlights suspect
numbered beats across the ruler, drum lanes and velocity lane with a translucent
red layer, shows the reasons below the grid, and offers navigation to the next
suspect bar. Both beats are marked when a pattern crosses a beat or bar boundary.
The beat grouping follows the existing editor's quarter-note numbering, including
the partial final beat in 7/8.

The detector in `frontend/app/utils/beatReview.ts` uses named, independently
testable rules rather than treating note strength as model confidence:

| Rule | Trigger |
| --- | --- |
| Simultaneous hand parts | At least three distinct non-kick lanes on one quantized step, including toms. |
| Hi-hat/cymbal sequence | Adjacent closed-hi-hat/cymbal onsets in either order within an eighth note; a recognized open hi-hat interrupts this pattern. |
| Conflicting hats | Closed and open hi-hat on the same step. |
| Duplicate hit | Multiple events for one instrument quantize to the same step. |
| Timing mismatch | Onset is more than both 45 ms and 40% of one grid step away from the nearest step. |

These are explainable heuristics, not proof of an error. In particular, intentional
cymbal changes, flams, swing and loose timing can be flagged. The source model has
a generic Cymbal lane; it cannot distinguish a crash from a ride here. No notes
are automatically removed or changed, and unmarked beats still need listening.
Hits earlier than the score offset minus half a grid step are excluded, matching
score quantization and playback. Changes to those dropped hits do not reopen an
accepted beat; hits exactly at the cutoff remain eligible for review.

Editing a suspect beat keeps it pending until **Resolve** is pressed. Resolve can
also accept the notes unchanged. Undo/redo includes both note edits and review
decisions. Editing a resolved beat reopens it; editing other beats leaves it
resolved. Resolved rows stay visible in a compact neutral style.

Review state participates in the existing unsaved-change guard and is published
atomically with a successful score regeneration (`beat_review` in the result and
bundle analysis). A resolution-only save also works. Failed rendering does not
publish the draft. Reloading and switching views retain saved decisions;
heading-only updates preserve them and any current review drafts. Existing
sessions without review metadata are checked on load. BPM, offset, meter or grid
changes trigger fresh checking so old approvals are not applied to different
beat locations. Future rules can add reason codes without replacing this workflow.

Unit tests cover rule thresholds, negative cases, triplets, odd meters, cross-bar
patterns, edited approvals and grid changes. Browser tests cover red bands,
reasons, unchanged acceptance, edit-then-confirm, undo/redo, navigation, regeneration,
reload and compact layout. The Python test runs a real Verovio regeneration to
verify review and unchanged-event persistence, metadata updates and ZIP output.

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
`frontend/`. Playwright serves the generated `.output/public` files through a local
Vite preview server, avoiding Nuxt dev-server compilation during the first test.
Local E2E runs build automatically; CI uses its preceding build step. Set
`BEATIT_WEB_URL` to test an existing server instead. CI retains failed-test traces
and error context as artifacts for seven days.
Playwright covers import, queue progress, cancellation/retry/failure,
editing, undo/redo, regeneration payloads, draft navigation, score/export, audio
transport/source switching, resizing and compact layouts with isolated API fixtures.
Score-follow coverage includes real audio playback, seeking across pages, zoom,
manual follow suspension/resumption, tab remounting and unavailable layout data.
Beat-follow checks cover whole-beat highlighting, playback without native
`timeupdate` events, and exact beat/bar seeks at fractional tempos.
Score-click checks cover positive/negative offsets, zoomed pages, keyboard
activation, paused/playing transport and synchronization back to the beat editor.
Export UI checks initiate native ZIP, MusicXML, MIDI, PDF, JSON and SVG downloads,
verify filenames, retain editor/history state, and refresh without another download.
Native downloads bypass Playwright API mocks, so actual payload delivery is checked
separately against FastAPI with stored session artifacts.
Heading-edit checks cover legacy defaults, mobile editing, author removal, failure,
reload persistence, preview/export refresh, and preservation of rhythm drafts and
undo/redo. An isolated MuseScore worker also regenerated a Korean title and author:
the SVG preview and downloaded PDF showed both, all four measures remained mapped,
and MIDI/event hashes were unchanged. Existing user sessions were not edited.
The Python API suite checks real waveform transients, channel handling, cache
invalidation and source restrictions, plus SVG measure bounds, final barline
deduplication, Verovio viewport transforms and generation consistency.
Existing stored sessions can additionally be
opened for actual waveform/SVG inspection without re-running model inference.

The implementation was also checked against the generated static app on FastAPI:
an isolated eight-second session accepted a new hit and a 120-to-126 BPM revision,
ran the real worker/Verovio regeneration, published a new generation with 17 hits,
and served the updated SVG and MusicXML download. Existing Wake session playback,
waveforms and four-page A4 printing were inspected separately. This does not
re-evaluate the accuracy of the transcription models or the full inference pipeline.

Measure mapping was checked on four stored MuseScore scores (83, 84, 150 and 150
bars) and an existing five-bar Verovio result. Real Wake playback advanced the
highlight from bar 66 to 67; seeking to bar 49 opened page 2, and out-of-view bars
scrolled into the score panel. A 390px viewport and four-page A4 print were checked;
the playback highlight is hidden in print.
