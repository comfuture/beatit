# Multi-user storage and deployment

Beatit requires an explicitly configured MySQL 8+ database, private S3-compatible
object storage, and a Google web OAuth client. Missing configuration fails closed;
there is no local SQLite or anonymous-account fallback. The selected deployment
reuses the existing OCI MySQL service: chunsik connects over its private OCI path,
and the developer Mac has been verified through an SSH TCP forward on local port
13306. A database-specific Cloudflare Access hostname has not been established.
Media and generated artifacts use a dedicated private Cloudflare R2 bucket. The
public application origin is `https://beatit.changkyun.kim`.

## Database decision (issue #7)

| Candidate | Decision and operational implications |
| --- | --- |
| Existing OCI MySQL | Selected after direct chunsik connectivity and a Mac SSH-forwarded connection to the existing service were verified. InnoDB transactions, row locks, JSON, and `SKIP LOCKED` fit admission, leases and atomic revision publication. Use a dedicated database/user; never modify another application's tables. The combined application/object restore is verified below; provider-native backup retention remains uninspected. |
| Dedicated MySQL or PostgreSQL | Both fit the queue. A new instance or managed Supabase/Neon PostgreSQL adds provisioning and another operational dependency when an existing MySQL service is already available. Supabase was initially proposed before the existing connection was clarified; it is not required by this implementation. |
| Cloudflare D1 | Viable with a narrow authenticated Worker API, but it requires a different transactional coordination boundary for this Python application. A tunnel does not provide a D1 MySQL wire endpoint. Rejected here on integration complexity and existing MySQL availability, not on an unmeasured claim that D1 is slower or unreliable. |

MySQL documents [transactional locking reads and SKIP LOCKED](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html).
Cloudflare documents [external D1 access through a Worker](https://developers.cloudflare.com/d1/tutorials/build-an-api-to-access-d1/)
and [D1 limits](https://developers.cloudflare.com/d1/platform/limits/), including
bounded row payloads. Cloudflare's [TCP access guide](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/non-http/cloudflared-authentication/arbitrary-tcp/)
describes client-side `cloudflared access tcp` forwarding.

The [managed verification record](benchmarks/multiuser-managed-2026-11-09.json)
records an actual OCI server version of `26.10.0-cloud`, three applied migrations,
and a `VERIFY_CA` connection using `TLS_AES_128_GCM_SHA256`. One Mac SSH-path probe
measured 54.239 ms for the version/read operation, 138.317 ms for a locking
transaction, and 588.569 ms for a verified 4-KiB private R2 put/get/delete round trip.
These are individual observations, not latency distributions or a performance
comparison with PostgreSQL or D1.

Against the actual managed backend, two synthetic owners passed 47 cross-owner and
anonymous route assertions, simultaneous submissions, one-winner competing claims,
A-to-B-to-A fair service, cancellation/retry, persisted score/metadata/review edits,
and recovery with empty application scratch. Actual MusicXML, SVG, MIDI, ZIP,
range-media, waveform and layout responses were checked. The backup drill restored
three jobs, four revisions and 41 objects into the then-empty dedicated database,
verified every object hash and both owners' access, and removed only those synthetic
records and objects. All 11 checked table counts returned to zero before real login
and legacy import began. This drill used internal synthetic sessions and generated
scores; it did not exercise Google-provider login or model inference.

One real personal Google account completed consent, loaded the workspace, logged
out through the UI (HTTP 204, workspace cleared), and signed in again through
routine Google account selection without repeating consent. Saved history
reopened successfully. The operator skipped the work-account profile, so two real
Google accounts have not been verified.

A new original eight-second browser upload completed on Modal/CUDA in a reported
56.9 seconds, initially producing 29 events at 120 BPM, 4/4 and five bars. UI Kick
toggling with undo/redo, regeneration to 30 events, and title/author changes yielded
three durable revisions. A downloaded 61,546-byte ZIP matched its SQL manifest
SHA-256, passed ZIP integrity, and contained the expected 30 events and MusicXML
heading. After stopping the idle service, clearing only its isolated scratch
volume, and force-recreating the container with the same image, manual history
reopening restored the score and media. Nine current artifact sizes/hashes and all
three revisions were preserved. Audio range responses returned 206; waveform and
layout returned 200. Authentication fails closed during downtime and clears the
workspace; automatic restoration of the selected session was not claimed. The
[local regression record](benchmarks/multiuser-local-2026-11-09.json) is an earlier
MySQL/S3-emulator snapshot; its production-pending fields are historical and are
superseded by the managed record for the checkpoints verified above.

## Schema and durable state

Ordered, restart-safe SQL migrations live in `src/transcription/migrations/`.
Startup serializes migrations with a database-scoped MySQL named lock. Application
connections use a bounded SQLAlchemy QueuePool with PyMySQL, UTF-8, UTC and READ
COMMITTED isolation. Remote connections default to `VERIFY_IDENTITY`: TLS must
validate both the server certificate chain and its hostname. Supply a private CA
with `?ssl_ca=/absolute/path/to/mysql-ca.pem` when needed. For an OCI-generated
certificate whose name does not match the private or tunnel hostname, explicitly
select `?ssl_ca=/absolute/path/to/mysql-ca.pem&ssl_mode=VERIFY_CA`. This mode requires
the pinned CA and verifies the certificate chain while omitting hostname matching.
It does not allow unverified certificates. `DISABLED`, `PREFERRED`, `REQUIRED` and
unknown modes are rejected. See [MySQL connection verification modes](https://dev.mysql.com/doc/refman/8.4/en/connection-options.html).
Loopback without CA/mode settings remains permitted for disposable local databases;
adding either supported verification mode forces verified TLS there as well.

Users are keyed by Google's verified stable `sub`, not email. Projects and jobs
have mandatory owners and composite owner/project foreign keys. Requests/options,
progress, errors, score metadata, events, beat-review resolutions, revisions and
object manifests are durable SQL state. Logs are stored as bounded tails. JSON
files exist only as a subprocess interchange format in disposable scratch.

Objects have content hashes and immutable object keys. Uploading objects precedes
the SQL transaction that publishes the manifests and completed revision. A failed
upload or lost lease cannot replace the previous published score. Downloads,
media ranges, waveform and layout all require an authenticated owner check and
load only manifested objects. Browser responses are private/no-store. There are
no public bucket URLs or reusable bearer download links. Unreferenced uploads
after an aborted transaction can remain in storage; retain them until a
manifest-aware reconciliation, and do not apply an arbitrary age-based bucket
expiry rule to live artifacts.

One global SQL worker slot preserves the initial single inference worker. Claims,
heartbeats, cancellation and result publication use unique attempt tokens and
leases. Stale workers cannot publish. Expired running attempts become failed and
require explicit retry; the server does not silently duplicate interrupted work.
Owners rotate by last service time, with a default limit of three queued/running
jobs per owner (`BEATIT_MAX_PENDING_PER_USER`). `BEATIT_LEASE_SECONDS` defaults to
90 seconds. Different jobs may create private local scratch concurrently, but
only the leased worker may run inference.

## Private playback delivery

The optional [private media Worker](media-delivery.md) serves playback bytes from
R2 directly to the browser after a fresh same-origin API session/owner/manifest
check for every GET, HEAD or range request. The bucket stays private; there are no
signed bearer URLs or authorization caches. Logout revokes subsequent requests,
while already delivered or in-flight bytes cannot be recalled.

`BEATIT_MEDIA_BASE_URL` defaults to the authenticated API relay; `/media` is the
only enabled Worker value. The relay/waveform cache uses per-object download locks
so independent cold reads do not hold the global job lock. The player preloads
media and reports buffering while holding/resuming the matching drum clock. The
media document records range behavior, deployment/rollback, and the short-sample
cold/warm diagnostic limits. These source changes require their own application
and Worker deployment evidence; the earlier `dcef260` checkpoint above does not
establish their rollout.

## Prepare the environment

1. Create a dedicated database (for example `beatit`) and a dedicated app user on
   the existing managed MySQL service. Grant access only to `beatit.*`: SELECT,
   INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX and REFERENCES for the current
   automatic migration workflow. Do not use an account with privileges on other
   applications or global administration privileges.
2. Retain the existing OCI private network and pinned TLS CA. The verified Mac
   route is SSH forwarding through chunsik; substitute the private OCI host without
   placing credentials in the command:

   ```sh
   ssh -N -L 13306:PRIVATE_OCI_MYSQL_HOST:3306 chunsik
   ```

   Set the Mac connection URL to
   `mysql://beatit:URL_ENCODED_PASSWORD@127.0.0.1:13306/beatit?ssl_ca=/absolute/path/to/mysql-ca.pem&ssl_mode=VERIFY_CA`
   when the OCI-generated certificate is signed by that CA but names neither the
   loopback endpoint nor the private hostname. chunsik uses the private OCI address
   with the pinned CA. Prefer `VERIFY_IDENTITY` whenever the certificate matches
   the connection hostname.

   Cloudflare Access TCP remains an optional alternative once its actual database
   hostname and policy are verified. Do not assume that the existing application
   HTTPS tunnel exposes a MySQL endpoint. Do not put DB credentials in browser
   JavaScript or Cloudflare DNS records.
3. Prepare a private R2 bucket and a server-side S3 credential limited to that
   bucket. Keep public access disabled. The S3 adapter also supports other private
   S3-compatible providers. Set the five `BEATIT_S3_*` values in the template.
4. Follow [Google OAuth setup](google-oauth-setup.md). The canonical origin is
   `https://beatit.changkyun.kim`, with exact callback
   `https://beatit.changkyun.kim/api/auth/callback`. A local development callback
   can be added separately as `http://localhost:8000/api/auth/callback`.
5. Copy the placeholders in [.env.multiuser.example](../.env.multiuser.example)
   into the ignored `.env`; preserve the existing Modal settings. Keep `.env`
   private (`chmod 600 .env`). The native CLI does not load dotenv implicitly:

   ```sh
   uv run --env-file .env python scripts/manage_data.py probe
   npm --prefix frontend run build
   BEATIT_PUBLIC_URL=http://localhost:8000 uv run --env-file .env drum-score serve --engine local
   ```

The probe reports server version, connection/transaction timings and a verified
private object upload/download/delete round trip, without printing credentials.
Run it on the Mac SSH forward and chunsik private path; save the JSON output as
the deployment record. It writes only a random probe object and does not modify
project rows.

## Legacy migration and rollback

Stop the legacy application and make an offline backup of its entire data volume,
including `jobs.sqlite3`, input/media, JSON and every render directory. Keep the
old image and protected environment alongside that backup. Do not run the new
image over the only copy of the old volume.

The intended owner must first sign in with Google. Read that account's ID from
the owner-authenticated `/api/auth/me` response. Import using an explicit stable
source name and owner ID; source bytes are never modified:

```sh
uv run --env-file .env python scripts/manage_data.py import-legacy /backup/legacy-data \
  --owner-id GOOGLE_ACCOUNT_DATABASE_ID --source-name chunsik-before-issue7 --app-stopped
```

The source name plus legacy job ID determines the destination ID. Repeating the
same import skips existing rows, and assigning the same source to a different
owner is rejected. A deliberate deletion removes its import record with the job;
do not rerun the source import to recreate an item the owner intentionally deleted. Completed results/edits and all manifested artifacts survive;
interrupted legacy jobs become failed and are not automatically scheduled.

```sh
uv run --env-file .env python scripts/manage_data.py rollback-import \
  --owner-id GOOGLE_ACCOUNT_DATABASE_ID --source-name chunsik-before-issue7 --app-stopped
```

Rollback removes only that owner's imported jobs; the original offline source is
untouched. To return to the old application, restore its image, original data
backup and original environment together, and keep it private: it has no account
isolation. Never point the old image at the new database or export a shared
legacy volume to all Google accounts.

On 2026-11-09, the operator assigned all seven completed legacy jobs to the personal
Google account. The import preserved seven result JSON values and 14 checked
MusicXML/ZIP hashes; a rerun skipped seven rows. A separate extracted source copy
needed an ownership correction, while the original data and offline archive were
unchanged. The user then deleted one imported job through the UI (HTTP 204), leaving
six imported jobs. Its deletion must remain effective. The original backup's path,
byte count and SHA-256 are in [the deployment record](deployment-chunsik.md).

## Backup and restore

Managed SQL backups alone do not include media. Let active work finish, stop every
application/worker instance, reconcile unrecovered remote inference calls, and wait
for its lease to release/expire. Backups reject unresolved remote call records.
The `--app-stopped` flag is an operator assertion, not a stop command.

For the actual chunsik deployment, use its explicit project, protected `.env` and
MySQL TLS overlay. Modal is selected through `BEATIT_EXECUTION_BACKEND=modal` in
that environment. Do not select `/home/ubuntu/services/beatit`, which is the retained
legacy project:

```sh
ssh chunsik
cd /home/ubuntu/services/beatit-multiuser
beatit_compose() {
  docker compose -p beatit-multiuser --env-file .env \
    -f compose.yaml -f compose.mysql-tls.yaml "$@"
}
beatit_compose stop beatit
sudo install -d -o 10001 -g 10001 -m 0700 backups
beatit_compose run --rm --no-deps --volume "$PWD/backups:/backup" \
  --entrypoint python beatit -m transcription.data_admin \
  backup /backup/beatit-2026-11-09.tar.gz --app-stopped
beatit_compose up -d --no-build --wait
```

The image contains the installed `transcription.data_admin` module; it does not need
a source checkout's `scripts/` directory or `uv` inside the running image. Choose a
new archive name for each backup; existing files are never overwritten. The host
backup directory must be writable by container UID 10001. From a native checkout,
run the equivalent after stopping all application instances:

```sh
uv run --env-file .env python scripts/manage_data.py backup \
  /secure/beatit-backup.tar.gz --app-stopped
```

The mode-0600 archive contains a consistent durable-table snapshot and the exact
referenced object bytes with hashes. Sessions/OAuth state are omitted, so users
sign in again after restore. Keep this private media archive encrypted in an
independent backup location with defined retention. Unresolved remote call records
are never copied into a portable backup.

Restore into a fresh, empty dedicated database and private bucket configuration.
Use a separate protected environment file, such as `.env.restore-drill`, that points
to that empty destination. Never aim a restore drill at the live populated database:

```sh
uv run --env-file .env.restore-drill python scripts/manage_data.py restore \
  /secure/beatit-backup.tar.gz --app-stopped
```

Restore refuses nonempty durable/auth/runtime tables or a live worker slot,
validates archive paths and object hashes, and inserts owner/dependency rows
transactionally. New immutable keys avoid overwriting destination objects.
Interrupted running jobs restore as failed with no live lease. The source archive
is retained if an operation fails. Failed uploads may leave unreferenced objects
under their restore namespace after an outage; reconcile them against committed
manifests before deletion.

A real OCI/R2 restore was verified on 2026-11-09 while the newly provisioned database
was still entirely empty. It restored only the earlier synthetic smoke backup,
verified all 41 object hashes and both owners' access, then removed those exact
synthetic rows/objects and returned all checked tables to zero. This was completed
before real login or legacy import. Future drills should use a separate database;
that one-time empty-production window is no longer available. Keep the provider's
native MySQL backup/PITR policy as an additional layer; its configured retention
has not been inspected in this verification.

## Docker and HTTPS rollout

Compose passes required auth, database and object configuration explicitly.
`data` is now scratch; the model-cache volume remains a cache. If OCI uses its own
CA, include `compose.mysql-tls.yaml`, set `BEATIT_MYSQL_CA_FILE` and use
`ssl_ca=/run/secrets/mysql_ca` in the connection URL. Add `&ssl_mode=VERIFY_CA`
only for a CA-verified OCI certificate with a mismatched hostname; otherwise keep
`VERIFY_IDENTITY` (the default).

The observed deployment uses Compose project `beatit-multiuser` in
`/home/ubuntu/services/beatit-multiuser`, with loopback port 18001. The existing
Cloudflare Tunnel routes `beatit.changkyun.kim` to `http://localhost:18001`; its
existing port-10100 API proxy and final 404 catch-all are preserved. Set
`BEATIT_PUBLIC_URL=https://beatit.changkyun.kim` and
`BEATIT_ALLOWED_HOSTS=beatit.changkyun.kim,localhost`. The Google callback must be
`https://beatit.changkyun.kim/api/auth/callback`.

The production Google cookie is Secure and does not work on the former plain-HTTP
Tailscale URL. `/api/health` checks DB reachability without exposing project data;
`/api/jobs` requires a signed-in session and is no longer a health probe. At the
observed checkpoint, the container was healthy and anonymous project routes
returned 401. The previous private port-18000 `beatit-beatit-1` container was then
stopped reversibly and is `exited`. Its image remains tagged
`beatit:pre-multiuser-20261109`, and its original volumes, environment and offline
backup are retained. The new multi-user container is healthy.

The managed storage/queue/restore drill, one real Google account's login/logout/
login flow, actual Modal/CUDA inference, browser editing/downloads and deployed
container recreation are verified above. A second real Google account was skipped
by the operator. Provider-native backup retention remains uninspected. The
[managed record](benchmarks/multiuser-managed-2026-11-09.json) records these evidence
boundaries without treating synthetic users as additional real Google logins.

## Local regression checks

Use a disposable database whose name is exactly `beatit_test`. Fixtures refuse to
reset any other database. Never set this URL to the OCI production database.

```sh
export BEATIT_TEST_DATABASE_URL='mysql://beatit:TEST_PASSWORD@127.0.0.1:55438/beatit_test'
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run test:e2e -- --workers=2
```

`pytest -m 'not mysql'` runs model/unit tests without a DB. CI additionally runs
the marked integration tests against MySQL 8.4. Object protocol fixtures persist
outside application scratch; a live S3 round trip is separately exercised by
`manage_data.py probe`.
