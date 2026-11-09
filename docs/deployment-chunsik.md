# chunsik deployment

Beatit runs in `/home/ubuntu/services/beatit` on chunsik with Docker Compose.
The ARM64 host runs the web service and local score editing. This deployment selects
the private Modal GPU engine for inference; local CPU/Mac and Colab remain supported
service configurations. The frontend is built into the Python image, with one
application container and one job queue.

- Private URL: <http://chunsik.emperor-micro.ts.net:18000>
- Private IPv4 URL: <http://100.71.242.57:18000>
- Container: `beatit-beatit-1`
- Data: Docker volume `beatit_data`
- Model and compilation caches: Docker volume `beatit_model-cache`
- Runtime limits: 2 CPUs, 12 GiB RAM; numerical library thread counts: 2
- Renderer: Verovio (SVG and MusicXML; MuseScore is not installed for PDF)

## Modal configuration

Use `COMPOSE_FILE=compose.yaml:compose.modal.yaml` and
`BEATIT_EXECUTION_BACKEND=modal` in the protected server `.env`. Set
`BEATIT_MODAL_APP=beatit`, `MODAL_ENVIRONMENT=main`, and the published
`BEATIT_MODAL_IMAGE` tag documented in [Modal deployment](modal-deployment.md).
The named image is built/published independently on the operator's machine; the
chunsik application image retains the local backend dependencies for score edits
and an explicitly configured local fallback.

Store the token ID and secret in separate protected files and configure
`BEATIT_MODAL_TOKEN_ID_FILE` / `BEATIT_MODAL_TOKEN_SECRET_FILE` with their host paths.
For file-backed Docker secrets, these files must be readable by container UID 10001;
setting their owner to `10001:10001` and mode `0400` is sufficient. Keep their parent
directory private. Do not print token values or include them in deployment records.

When moving from the former Colab deployment, check that no job/runtime is active,
then stop `colab-manager` and `colab-controller` using their existing Compose files.
Keep their containers, authentication volumes, images and source configuration for
rollback. Do not remove the data or model-cache volumes. Deploy the application with
the base and Modal secret overlay, rather than continuing to include Colab overlays.

Connect from a device on the same Tailscale network. The listener is bound only to
the server's Tailscale IPv4. For SSH access without a local Tailscale client:

```sh
ssh -N -L 8000:100.71.242.57:18000 chunsik
# Open http://127.0.0.1:8000
```

The server's `.env` holds the binding and permitted Host names. This file is not
in Git or the image. The service has no login and should stay on a private network.

## Operations

```sh
ssh chunsik
cd /home/ubuntu/services/beatit
docker compose ps
docker compose logs --tail 100 beatit
docker compose exec beatit drum-score doctor
docker compose restart beatit
```

Docker and Tailscale are enabled at boot. The container uses `unless-stopped`;
after a deliberate `docker compose stop`, start it again with `docker compose up -d`.
The HTTP health check probes `/api/jobs` every 30 seconds.

Before updating, let active jobs finish, retain the previous image, and back up the
data volume and SQLite database. Keep protected copies of `.env`, the Compose files
and deployment record with the backup. Then copy the new sources
without local data, virtual environments, node_modules, or `.env`, and rebuild:

```sh
docker tag beatit:local beatit:previous
docker compose build
docker compose up -d --wait
```

To restore the previous image without rebuilding:

```sh
BEATIT_IMAGE=beatit:previous docker compose up -d --no-build --wait
```

For an engine migration rollback, also restore the backed-up `.env` and its Compose
file selection; restarting the previous image with Modal settings is not a complete
rollback to the former Colab controller deployment.

After switching to Modal, `/api/system` must report `execution_backend=modal` and an
available remote runtime. Verify a fresh upload through the private browser/API URL,
then check the completed result's Modal/CUDA provenance, SVG preview, MusicXML/MIDI
downloads, waveform and score layout. A healthy container alone is not sufficient.
Confirm existing job IDs and artifact hashes are preserved, and that the remote
GPU scales back to zero after processing.

Stopping or recreating the container preserves both volumes. Do not use
`docker compose down -v` unless you intend to erase uploaded files, results, job
history, and downloaded models. Existing local Mac sessions are not copied by the
source deployment commands.

## Deployment verification (2026-10-26)

The ARM64 image was built on chunsik and passed its HTTP health check. The runtime
reports PyTorch 2.8.0+cpu, FFmpeg, and Verovio. The application runs as UID 10001;
the Docker CPU/memory limits and private-interface binding were inspected.

A 12-second excerpt from the existing Wake test input completed the real Demucs,
DrumSep, ADTOF, Beat This!, and engraving pipeline in 290.3 seconds. MusicXML, MIDI,
SVG, JSON, ZIP integrity, waveform, and score-layout responses were checked. This
is deployment validation, not a new transcription-accuracy evaluation or a
full-length load test. CPU processing is slow on this host.

After force-recreating the container, the completed job and downloaded artifact
bytes were preserved. An edited score title was regenerated successfully. All
three downloaded model groups were loaded with `HF_HUB_OFFLINE=1`, confirming the
cache survives recreation. Nine API tests also passed locally.

The verification session is named `deployment-check-wake-12s.wav` and its score
title is `배포 검증 · Wake 12초`. No existing Mac sessions were migrated.

Multi-user authentication, owner isolation, and replacing SQLite/JSON project
persistence are tracked in [issue #7](https://github.com/comfuture/beatit/issues/7).
The current service remains a shared single-user workspace.

### CPU performance probe

Under the same 2-CPU container limit, one real DrumSep forward pass over the same
11.877-second chunk took 45.419 seconds with 4 PyTorch threads and 45.543 seconds
with 2 threads. The two output tensors had a maximum absolute difference of zero.
This was one pass per configuration, not a statistical performance comparison;
there was no observed speed improvement from reducing threads. The pipeline now
respects the configured thread limit instead of unconditionally selecting four.

The separator advances by 2.969 seconds per chunk (overlap 4), including its current
edge padding. Its code schedules 5 chunks for 12 seconds, 87 for 4 minutes, and 108
for 5 minutes. At the measured per-chunk time, DrumSep alone would take roughly
66–82 minutes for a 4–5 minute song; Demucs and other stages add time. These are
code-derived estimates, not full-song measurements. Practical interactive use
needs a separate performance decision, such as GPU inference or a lighter mode
whose transcription quality is evaluated against the current pipeline.
