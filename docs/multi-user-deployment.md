# Multi-user storage and deployment

Beatit requires an explicitly configured MySQL 8+ database, private S3-compatible
object storage, and a Google web OAuth client. Missing configuration fails closed;
there is no local SQLite or anonymous-account fallback. The selected deployment
reuses the existing OCI MySQL service: chunsik connects over its private OCI path,
and the developer Mac connects through the existing Cloudflare Access TCP tunnel.
Media and generated artifacts use a dedicated private Cloudflare R2 bucket.

## Database decision (issue #7)

| Candidate | Decision and operational implications |
| --- | --- |
| Existing OCI MySQL | Selected after the operator confirmed that chunsik already has direct access and the Mac has a cloudflared path. InnoDB transactions, row locks, JSON, and `SKIP LOCKED` fit admission, leases and atomic revision publication. Use a dedicated database/user; never modify another application's tables. Existing provider backups and restore retention must be verified before rollout. |
| Dedicated MySQL or PostgreSQL | Both fit the queue. A new instance or managed Supabase/Neon PostgreSQL adds provisioning and another operational dependency when an existing MySQL service is already available. Supabase was initially proposed before the existing connection was clarified; it is not required by this implementation. |
| Cloudflare D1 | Viable with a narrow authenticated Worker API, but it requires a different transactional coordination boundary for this Python application. A tunnel does not provide a D1 MySQL wire endpoint. Rejected here on integration complexity and existing MySQL availability, not on an unmeasured claim that D1 is slower or unreliable. |

MySQL documents [transactional locking reads and SKIP LOCKED](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html).
Cloudflare documents [external D1 access through a Worker](https://developers.cloudflare.com/d1/tutorials/build-an-api-to-access-d1/)
and [D1 limits](https://developers.cloudflare.com/d1/platform/limits/), including
bounded row payloads. Cloudflare's [TCP access guide](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/non-http/cloudflared-authentication/arbitrary-tcp/)
describes client-side `cloudflared access tcp` forwarding.

Evidence boundary: local MySQL 8.4 integration tests exercise simultaneous claims,
owner fairness, stale fencing, cancellation, durable artifact recovery, import and
backup/restore. The actual OCI connection and R2 latency must be recorded using
the probe below once the dedicated credentials and tunnel hostname are supplied.
No production MySQL, D1 or Supabase latency comparison has been measured. A
read-only chunsik inspection confirmed a MySQL client but no local 3306 listener;
that does not test the remote OCI database. Do not treat local tests as production
connectivity or backup verification.

The [local verification record](benchmarks/multiuser-local-2026-11-09.json)
includes the measured MySQL transaction and S3-emulator round trip and explicitly
lists the unverified production checkpoints.

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

## Prepare the environment

1. Create a dedicated database (for example `beatit`) and a dedicated app user on
   the existing managed MySQL service. Grant access only to `beatit.*`: SELECT,
   INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX and REFERENCES for the current
   automatic migration workflow. Do not use an account with privileges on other
   applications or global administration privileges.
2. Retain the existing OCI private network/TLS and Cloudflare Access policy.
   From the Mac, start the existing tunnel, substituting its real hostname:

   ```sh
   cloudflared access tcp --hostname MYSQL_ACCESS_HOST --url 127.0.0.1:13306
   ```

   Set the Mac connection URL to
   `mysql://beatit:URL_ENCODED_PASSWORD@127.0.0.1:13306/beatit?ssl_ca=/absolute/path/to/mysql-ca.pem&ssl_mode=VERIFY_CA`
   when the OCI-generated certificate is signed by that CA but names neither the
   loopback endpoint nor the private hostname. chunsik should use the private OCI
   address with the same pinned CA. Prefer `VERIFY_IDENTITY` whenever the
   certificate matches the connection hostname.
   Do not put DB credentials in browser JavaScript or Cloudflare DNS records.
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
Run it on both the Mac tunnel and chunsik private path; save the JSON output as
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
owner is rejected. Completed results/edits and all manifested artifacts survive;
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

## Backup and restore

Managed SQL backups alone do not include media. Stop every application/worker
instance, reconcile any unrecovered remote inference calls, and wait for its
lease to release/expire. Backups reject unresolved remote call records. Use the
combined archive tool:

```sh
uv run --env-file .env python scripts/manage_data.py backup /secure/beatit-backup.tar \
  --app-stopped
```

The archive contains a consistent durable-table snapshot and the exact referenced
object bytes with hashes. Sessions/OAuth state are omitted, so users sign in again
after restore. The archive contains private user media; keep it encrypted in an
independent backup location with defined retention. The `--app-stopped` flag is an
operator assertion, not a command that stops running containers.

Restore into a fresh, empty dedicated database with a configured private bucket:

```sh
uv run --env-file .env python scripts/manage_data.py restore /secure/beatit-backup.tar \
  --app-stopped
```

Restore refuses nonempty application tables, validates archive paths and object
hashes, and inserts owner/dependency rows transactionally. New object keys avoid
overwriting the destination's objects. Interrupted running jobs restore as failed
with no live worker lease. Test a restore periodically in a separate database
and check both owners, score edits, media range requests and downloaded hashes.
Keep the provider's native MySQL backup/PITR policy as an additional layer.

## Docker and HTTPS rollout

Compose passes required auth, database and object configuration explicitly.
`data` is now scratch; the model-cache volume remains a cache. If OCI uses its own
CA, include `compose.mysql-tls.yaml`, set `BEATIT_MYSQL_CA_FILE` and use
`ssl_ca=/run/secrets/mysql_ca` in the connection URL. Add `&ssl_mode=VERIFY_CA`
only for a CA-verified OCI certificate with a mismatched hostname; otherwise keep
`VERIFY_IDENTITY` (the default).

Prepare the `beatit.changkyun.kim` DNS/Cloudflare Tunnel HTTPS route to the private
chunsik listener and set `BEATIT_ALLOWED_HOSTS=beatit.changkyun.kim,localhost`.
Keep the original private network restriction until two-account acceptance passes.
The production Google cookie is Secure and will not work on the former plain-HTTP
Tailscale URL. `/api/health` checks DB reachability without exposing project data;
`/api/jobs` now requires a signed-in session and is no longer a health probe.

Before enabling users: run the deployed DB/storage probe; sign in as two real
Google users; complete a short clip for each while competing for the single worker;
verify cross-owner URLs deny access; regenerate an edited score; recreate the
container with empty scratch; check both retained results/downloads; and verify
backup/restore in a fresh database. Mocked-provider browser tests are useful
regressions, but do not establish the live provider/deployed-storage checkpoint.

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
