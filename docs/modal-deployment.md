# Modal processing engine

Beatit defaults to **local** when `DRUM_EXECUTION_BACKEND` is unset. This deployment
explicitly selects Modal. The engine is a **service setting**, not a user-facing
selector. Existing local Mac/CPU/CUDA and Colab implementations remain available.

```sh
uv run drum-score serve                         # local (default)
uv run drum-score serve --engine modal
uv run drum-score serve --engine local          # Mac MPS or local CPU/CUDA
DRUM_EXECUTION_BACKEND=colab uv run drum-score serve
```

`--engine` overrides `DRUM_EXECUTION_BACKEND`. Invalid engine names fail at startup.
Colab still needs its existing controller/runtime configuration; see
[Colab deployment](colab-deployment.md). In Docker, set
`BEATIT_EXECUTION_BACKEND=modal|local|colab` in `.env`. `deploy.env.example` selects
Modal for this deployment; without that setting Compose also uses local. The Colab Compose overlay
continues to select Colab by default when explicitly included.

## Credentials and deployment

The [comfuture/main dashboard](https://modal.com/apps/comfuture/main?sort=modified_at&order=desc)
displayed Modal's initial `App`, remote Function and `local_entrypoint` starter on
2026-11-09. [`scripts/modal_app.py`](../scripts/modal_app.py) follows that structure.
The application uses private authenticated SDK calls; it creates no public HTTP endpoint.

For development, authenticate the CLI once and keep its profile on the operator's Mac:

```sh
uv sync --frozen
uv run modal setup
uv run modal deploy --env main scripts/modal_app.py
uv run modal run --env main scripts/modal_app.py  # prepare models on CPU, without a GPU
uv run drum-score serve --engine modal
```

Deployment and model preparation are separate. Preparing before the first upload
removes the initial model-download wait from the first transcription. The client
also calls the cached CPU preparation function before each GPU job, so a missing
cache is populated on CPU, and a failed download never schedules inference.

For a hosted service, inject **both** `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from
a protected environment file or secret manager. Use credentials for the intended
workspace/environment. The Modal CLI profile is used only when neither variable
nor secret file is configured. The token values are never returned by the system
API, copied into a task request, or injected into the GPU image.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DRUM_EXECUTION_BACKEND` | `local` | Native service engine |
| `BEATIT_EXECUTION_BACKEND` | `local` | Compose engine setting; deployment example selects `modal` |
| `MODAL_TOKEN_ID` | SDK profile | Modal client token ID |
| `MODAL_TOKEN_SECRET` | SDK profile | Modal client token secret |
| `MODAL_TOKEN_ID_FILE` | unset | Native service: file containing token ID |
| `MODAL_TOKEN_SECRET_FILE` | unset | Native service: file containing token secret |
| `BEATIT_MODAL_APP` | `beatit` | Deployed app and resource name prefix |
| `MODAL_ENVIRONMENT` | `main` | Deployment/client environment |
| `BEATIT_MODAL_GPU` | `T4` | GPU selection at deployment; match on the client for status |
| `BEATIT_MODAL_MODEL_STORAGE` | `volume` | Deployment-time `volume` or `image` |

Explicit environment values take precedence over `_FILE` values. Partial or empty
file credentials fail with a configuration error. No credential values appear in
that error. Restart the service after rotating credentials. Native services must
receive these variables through their process manager; Python does not implicitly
load `.env`.

For Docker with direct environment injection:

1. Copy `deploy.env.example` to `.env`, then set the two token values locally.
2. Restrict access: `chmod 600 .env`.
3. Run `docker compose --env-file .env up -d --build`.

For Docker secrets, leave the direct token variables empty and set
`BEATIT_MODAL_TOKEN_ID_FILE` and `BEATIT_MODAL_TOKEN_SECRET_FILE` to protected host
files containing the individual values. Files should be readable by the container's
UID 10001; for example use a protected directory and group-readable files for that
UID's group. Then:

```sh
docker compose --env-file .env -f compose.yaml -f compose.modal.yaml up -d --build
```

The overlay mounts them at `/run/secrets/modal_token_id` and
`/run/secrets/modal_token_secret`. `.env`, `.env.*`, `secrets/`, and `.modal.toml` are
ignored by Git. Do not put real tokens in `deploy.env.example`.

## Reusable image and model-cache decision

The image starts from `modal.Image.debian_slim(python_version="3.12")`, installs
FFmpeg and native audio libraries, then uses Modal's `Image.uv_sync` with the frozen
`uv.lock` and `cuda` extra. This installs the pinned Torch, NumPy, Demucs, DrumSep,
ADTOF and Beat This! dependencies during the cached build. Application source is
added **after** that dependency layer, so ordinary source edits do not reinstall
the ML environment. Tasks do not run pip or uv.

The default model store is the persistent v2 `beatit-models` Volume. Demucs,
DrumSep and Beat This! are downloaded by a CPU-only preparation function, into a
directory keyed by the model fingerprint. ADTOF weights already ship in its pinned
package and are verified during preparation. A successful preparation writes its
manifest last and commits the Volume. Subsequent jobs reuse that cache. GPU jobs
require the matching manifest and use Hugging Face offline mode.

The fingerprint includes model versions/revisions and the existing SHA-256 checks
for DrumSep and Beat This!. Model changes get a separate cache directory. Old model
versions are retained until the operator removes them; they are not job artifacts.

Modal's [model-weight guidance](https://modal.com/docs/guide/model-weights) states
that Volume and image storage have similar performance, and recommends Volumes
because changing an earlier image layer can otherwise cause repeated model
downloads. This is the reason for the default; it is not a measured Beatit latency
claim.

An image-baked alternative is implemented:

```sh
BEATIT_MODAL_MODEL_STORAGE=image uv run modal deploy --env main scripts/modal_app.py
```

This downloads weights in a cached **CPU build layer**, before mounting application
source. It offers a single immutable deployment artifact, but dependency changes or
changes to model-preparation code invalidate that layer. It also increases image
size. Switching mode requires redeploying; the client discovers the deployed mode
from preparation's response. It does not need the same storage variable.

| Strategy | Cold-start download | Rebuild/download frequency | Decision |
| --- | --- | --- | --- |
| Download on each new GPU container | Paid GPU waits for network | Every cold container | Rejected |
| Prepopulate a persistent model Volume on CPU | None after preparation | Per model fingerprint | Default |
| Bake weights into the image on CPU | None after image build | When that image layer changes | Optional |

### Cost comparison

Prices checked against [Modal pricing](https://modal.com/pricing) on 2026-11-09:
T4 `$0.000164/s`, CPU `$0.0000131/core/s`, memory `$0.00000222/GiB/s`.
Volumes include 1 TiB/month free; excess storage is `$0.09/GiB/month`. Included
workspace allowances and actual billing determine the final bill.

At the configured 2 CPU cores and 8 GiB requested memory, the resource-rate floor is:

```text
GPU worker: 0.000164 + 2 × 0.0000131 + 8 × 0.00000222 = $0.00020796/s
CPU preparation:      2 × 0.0000131 + 8 × 0.00000222 = $0.00004396/s
```

For an illustrative 60-second model download, downloading on every cold GPU costs
about `$0.01248` per cold start **before inference**. Preparing once on CPU costs
about `$0.00264` for the same 60 seconds, plus any applicable storage charges.
At 300 cold starts this avoids about `$3.74` of repeated GPU-download time before
subtracting the one CPU preparation and storage. These are calculations, not remote
benchmarks; network and initialization time still need measurement.

The current local caches contain DrumSep weights of 437,648,758 bytes and Beat This!
weights of 81,058,141 bytes. ADTOF's packaged weights are 3,617,805 bytes. These are
partial observed sizes, not the complete Modal image/Volume size; Demucs and runtime
libraries add more. Preparation reports its actual cache byte count in the manifest.
At this scale a model Volume is well below the published free storage allowance if
the workspace has sufficient allowance remaining.

Both functions have `max_containers=1`, inference has `min_containers=0`, and the
idle window is 2 seconds to limit paid idle time. Containers and cached image layers
can be reused, but models are currently loaded by the pipeline subprocess per job.
There is no claim that model RAM/VRAM is retained between jobs. A 120-second GPU
inference plus a 2-second idle tail is approximately `$0.02537` at the rate floor,
excluding CPU preparation, transfer and other initialization. Change the GPU only
after a real workload comparison.

## Job lifecycle and validation

1. Verify or prepare the model cache on CPU.
2. Pack the existing allowlisted input archive and upload it to `beatit-jobs` under
   a fresh job/attempt prefix. Large media is streamed through files, not RPC bytes.
3. Spawn the private GPU function. It forces CUDA and Verovio and runs the existing
   pipeline in a subprocess with the local engine setting to prevent recursive RPC.
4. Relay progress using `beatit-progress`, then stream back the output archive.
5. Validate archive paths, size limits, score XML/SVG, MIDI headers, bundle integrity
   and CUDA provenance using the shared Colab artifact validator before publishing.
6. Remove temporary remote job data and progress entries after success/failure.

SIGTERM/SIGINT cancels the remote FunctionCall with container termination. Persisted
call IDs allow a restarted service to cancel an interrupted call before processing
more work. A hard kill or a network outage can leave temporary remote files; inspect
the job Volume when recovering from such a failure. The local data directory keeps
original uploads and validated results. Result metadata identifies Modal, the model
fingerprint, storage mode, call ID and end-to-end client time.

Cached score edits and title/author changes remain local. Initial inference and
explicit tempo re-estimation use the configured service engine. There is no silent
fallback to Colab/local after a Modal failure. `/api/system` checks deployment and
cache metadata without starting a GPU, exposes safe connection status, and never
returns credentials. Its connection check has a two-second deadline.

Remote deployment, image-build success, cold/warm timings and an actual
upload-to-MusicXML/SVG/MIDI cycle must be verified with configured credentials before
calling a production rollout complete.

After deployment, run a real input through the selected backend:

```sh
uv run python scripts/smoke_pipeline.py --engine modal --input /path/to/owned.wav --output-dir work/modal-first
uv run python scripts/smoke_pipeline.py --engine modal --input /path/to/owned.wav --output-dir work/modal-reuse
```

Compare `execution_runtime.total_elapsed_seconds` in the two `result.json` files
and verify playable stems and the MusicXML/SVG/MIDI artifacts. A second invocation
reuses the model Volume and image; depending on timing it may still be a new GPU
container because the idle window is only two seconds.
