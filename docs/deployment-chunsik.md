# chunsik deployment

The authenticated multi-user service is available at
[https://beatit.changkyun.kim](https://beatit.changkyun.kim). It runs as a separate
Compose project from the retained legacy installation. Durable projects and edits
use the existing managed OCI MySQL service; uploaded media and generated files use
private Cloudflare R2. The application data volume is disposable scratch.

Current setup, migrations, backup/restore and TLS details are in
[multi-user deployment](multi-user-deployment.md). Sanitized measured evidence is
recorded in [the 2026-11-09 managed verification record](benchmarks/multiuser-managed-2026-11-09.json).

## Current multi-user deployment (2026-11-09)

| Item | Observed value |
| --- | --- |
| Checkout/deployment directory | `/home/ubuntu/services/beatit-multiuser` |
| Compose project | `beatit-multiuser` |
| Application image | `beatit:issue7-7c00a2c` |
| Observed image ID | `sha256:b62ae5bfbfd4aaedf21000420178fffc24e48766d55207c88e9fe939a095b543` |
| Platform/user | ARM64, UID 10001 |
| Private origin | `http://localhost:18001` |
| Public HTTPS origin | `https://beatit.changkyun.kim` |
| Health probe | `/api/health` (database reachability); container observed healthy |
| Inference | Private Modal GPU; local dependencies retained for score edits |
| Renderer | Verovio; MuseScore/PDF is unavailable in this image |
| Runtime limits | 2 CPUs, 12 GiB RAM; numerical library thread counts 2 |

The existing Cloudflare Tunnel routes the public hostname to the loopback origin.
The existing API proxy on port 10100 and the final HTTP 404 catch-all were
preserved. Public requests returned 200 for `/`, `/api/health` and `/api/auth/me`;
unauthenticated `/api/jobs` and `/api/projects` returned 401. The plain-HTTP legacy
URL is not the login origin: production session cookies require HTTPS.

The protected `.env` contains the application, OAuth, OCI and R2 configuration.
The MySQL CA is mounted as a secret readable by UID 10001 with mode `0400`.
chunsik connects directly to the private OCI database with `VERIFY_CA`, a pinned CA
and mandatory certificate-chain verification. The provider-generated certificate
does not match the connection hostname, so hostname matching is explicitly
omitted in this configuration. The Mac's measured connection uses SSH TCP
forwarding through chunsik on local port 13306. A database-specific Cloudflare
Access hostname was not verified; the application HTTPS tunnel is a separate path.

## Operate the correct Compose project

Use the explicit project, environment file and MySQL TLS overlay so commands do
not select the legacy service. Modal is selected by the protected environment
configuration (`BEATIT_EXECUTION_BACKEND=modal`). The following shell function is
local to the operator shell:

```sh
ssh chunsik
cd /home/ubuntu/services/beatit-multiuser
beatit_compose() {
  docker compose -p beatit-multiuser --env-file .env \
    -f compose.yaml -f compose.mysql-tls.yaml "$@"
}
beatit_compose ps
beatit_compose logs --tail 100 beatit
beatit_compose exec beatit drum-score doctor
beatit_compose exec beatit python -m transcription.data_admin probe
```

Let active jobs finish before a planned restart. Keep the previous image and a
combined database/object backup before updating. After preparing and validating a
new image, use `beatit_compose up -d --no-build --wait`. Recheck authenticated
history, media, edits and downloads: a healthy container establishes reachability,
not successful inference or complete owner isolation. Docker's `unless-stopped`
policy requires an explicit start after a deliberate stop.

See [backup and restore](multi-user-deployment.md#backup-and-restore) for commands
that stop the correct project and run the installed data-admin module with a
protected backup mount. SQL backups alone do not contain R2 media. Never point a
legacy single-user image at the managed schema or expose its shared data volume
through the public hostname.

## Google sign-in and migration evidence

One real personal Google account completed consent, registered a profile, received
a session cookie and loaded the browser workspace. UI logout returned HTTP 204 and
cleared the workspace. Routine Google account-selection login then succeeded
without repeating consent, and the user reopened saved history. The operator
skipped the work-account profile; a second real Google login has not been verified. Two
synthetic owners separately exercised 47 cross-owner/anonymous route assertions
against the actual OCI/R2 backend, including range downloads, queue fairness,
edits and empty-scratch recovery. Those tests do not substitute for a second
Google-provider login.

The operator explicitly assigned all seven completed legacy jobs to that signed-in
personal account. The offline source backup is:

```text
/home/ubuntu/services/beatit/backups/issue7-20261109-multiuser/legacy-data.tar.gz
224963486 bytes
sha256 f7cb7a3b461d24dd8d155b44d6961676077061a10b28eafc5a6a7ea602ad9967
```

The import preserved all seven result JSON values and 14 checked MusicXML/ZIP
hashes; an immediate rerun skipped all seven. Import ran from a separate extracted
copy. Correcting that copy's ownership did not modify the original data or backup.
The user then deleted one imported job through the UI (HTTP 204), leaving six
imported jobs. Do not rerun the import to recreate that deliberate deletion.

The actual managed backup/restore drill restored two synthetic owners, two
projects, three jobs, four revisions and 41 R2 objects into the then-empty dedicated
database. All 41 object hashes and both owners' artifact access were checked, then
the exact synthetic records and objects were removed. All 11 checked table counts
returned to zero before real users or legacy imports were added.

## Initial browser transcription and scratch recovery

The new original eight-second `Issue7-owner-A-8s.wav` submitted through the signed-in
browser completed on Modal with CUDA provenance. Reported processing time was
56.9 seconds; the initial result showed 120 BPM, 4/4, five bars and 29 events. This
is a short end-to-end functional check, not a transcription-accuracy benchmark.

The UI Kick toggle, undo and redo were exercised. Score regeneration published
30 events, and applying title/author metadata produced a third durable revision.
The browser saved the 61,546-byte ZIP with SHA-256
`160f72bd7754781deec1f5fd78b4d0a056bdaeaea94c15209c7e3648033c3ced`, matching
the SQL artifact manifest exactly. ZIP integrity passed; its 30 events and
MusicXML title were verified.

With zero active jobs, an idle worker slot and zero remote-call records, the
multi-user service was stopped. Only the isolated `beatit-multiuser_data` scratch
volume was cleared, from three top-level entries to zero. The container was then
force-recreated from `d8b8d328b334` to `87e3b49b1fb1` using the same image and
became healthy. The browser's auth cookie remained usable. The user manually
reopened the saved job from history: 30 events, metadata and SVG were restored;
audio ranges returned 206, and waveform/layout returned 200. Nine current artifact
sizes and SHA-256 values matched the R2-backed manifests, and all three revisions
remained in SQL.

At this initial `dcef260` checkpoint, a temporary auth connection failure cleared
the workspace. Reopening from saved history was verified before permalink and
reconnection support were added; the current behavior is described below. The final browser
screenshot is retained privately in `work/issue7/live-restored-score.png`.

The verified `dcef260` CI run passed 205 non-MySQL Python tests and 68 MySQL tests
(273 total), plus 45 frontend unit tests and 51 Playwright tests. The CI and managed
runtime checks establish different checkpoints; the second real Google account
and provider-native backup retention remain unverified.

## Permalinks, direct playback and quiet reconnects

The current origin image is `beatit:issue7-7c00a2c`; the R2 media Worker version is
`31fd3ed7-8682-4712-9f3b-882155bf0bd1`. Follow-up evidence is recorded in
[the deployed follow-up record](benchmarks/multiuser-followup-2026-11-09.json).

Loaded/uploaded sessions use `/sessions/{id}`. The URL restores the session on
reload and after Google login, without relying on browser storage or history-list
membership. Missing/deleted/foreign-owner IDs fail closed through the same API
owner checks. The real personal account completed the permalink logout/login
round trip. Medium screens (768–1279px) keep score settings inline and navigation
collapsed; mobile closes both panels, including desktop/tablet transitions.

Playback now uses the authenticated same-origin R2 Worker and SHA-validated AAC
96 kbps fast-start derivatives. All seven completed jobs received previews without
changing original manifests, results, timestamps, options or revisions. The long
recording's playback WAV was 48,529,490 bytes; its AAC preview is 3,365,915 bytes
(93.06% fewer bytes), with the same 275.11-second duration. A Mac Chrome sample
advanced 53.4 seconds with wall time and adequate buffered data; the operator also
confirmed uninterrupted streaming. Original WAV downloads remain unchanged.
See [media delivery](media-delivery.md) for authorization, fallback and measurement
boundaries.

The login/new-session screens retain core actions and required constraints.
Temporary auth/network/5xx failures preserve an already verified editor and retry
reads, while an initial unknown identity remains gated. Definitive null/401 still
clears the account; failed writes preserve drafts and require manual retry.
A 15-second origin stop showed only the neutral reconnect status, retained the
editor, and automatically recovered without raw signal/auth errors. Audio
continuity during that restart was not asserted because the player was paused.

## Stopped legacy installation

The former `beatit-beatit-1` container in `/home/ubuntu/services/beatit` is now
`exited`. This was a reversible stop of the private port-18000 service. Its image
`beatit:5a01d492327e` remains tagged `beatit:pre-multiuser-20261109`; the original
volumes, protected environment and offline backup are retained. The new
`beatit-multiuser` container remains healthy.

Restarting the old image restores a shared single-user workspace, not
authenticated access to the managed projects. Keep it private and preserve its
offline source backup; do not point it at the new MySQL schema.

## Historical CPU deployment verification (2026-10-26)

This section describes the former single-user image and its filesystem persistence.
It is not evidence for the current authentication or managed-storage behavior.

The ARM64 container ran as UID 10001 with PyTorch 2.8.0+cpu, FFmpeg and Verovio under
a 2-CPU/12-GiB limit. A 12-second Wake excerpt completed Demucs, DrumSep, ADTOF,
Beat This! and engraving in 290.3 seconds. MusicXML, MIDI, SVG, JSON, ZIP, waveform
and score-layout responses were checked. Container recreation preserved that
legacy volume's completed job/artifact bytes; title regeneration and offline model
cache loading also passed. This was one deployment qualification, not a new
accuracy evaluation or a full-song load test.

One DrumSep forward pass over an 11.877-second chunk took 45.419 seconds with four
PyTorch threads and 45.543 seconds with two; the output tensors matched exactly.
There was one observation per configuration, with no measured speed improvement.
The code-derived 66–82 minute DrumSep estimate for a four-to-five-minute song did
not measure a full-song run. The deployment now uses Modal GPU for inference; see
[the Modal qualification](benchmarks/modal-2026-11-09.md) for its separate evidence.
