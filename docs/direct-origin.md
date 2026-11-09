# Direct HTTPS origin on chunsik

Beatit keeps the canonical origin `https://beatit.changkyun.kim`, Google OAuth
callback, Secure HttpOnly session cookie, MySQL state and private R2 objects.
The DNS-only A record points to `144.24.83.69`; Nginx terminates trusted HTTPS on
chunsik and streams requests to the private container at `127.0.0.1:18001`.
The application port remains bound to loopback. No browser object-storage key or
second login/upload hostname is introduced.

## Isolated Nginx and existing Certbot

Use the existing `/usr/sbin/nginx`, `/usr/bin/certbot` and Let's Encrypt account.
The global Nginx service has unrelated sites and conflicts with Tailscale's 443
listener. Beatit's separate unit binds only the public NIC, `10.0.0.254:80/443`,
and leaves the Tailscale listener at `100.71.242.57:443` intact.

Render [the template](../deploy/nginx/beatit.conf.template) into
`/etc/beatit-nginx/nginx.conf`, replacing `__BIND_ADDRESS__` with `10.0.0.254`,
`__HOSTNAME__` with `beatit.changkyun.kim` and `__UPSTREAM_PORT__` with `18001`.
Install the unit as `/etc/systemd/system/beatit-nginx.service`; install the two
executable renewal hooks in `/etc/beatit-nginx/`. Create the `body`, `proxy` and
`acme/.well-known/acme-challenge` directories under `/var/lib/beatit-nginx`.
The body/proxy directories must be writable by `www-data`.

```sh
sudo nginx -t -c /etc/beatit-nginx/nginx.conf
sudo systemctl daemon-reload
sudo systemctl enable --now beatit-nginx.service
sudo systemctl status beatit-nginx.service
sudo journalctl -u beatit-nginx.service --since '15 minutes ago'
sudo tail -n 30 /var/log/beatit-nginx/access.log
```

The template permits 1100 MiB requests while the application retains its 1 GiB
file limit. `proxy_request_buffering off` avoids a second whole-file Nginx spool;
`proxy_buffering off` flushes SSE and media. A 15-second worker shutdown timeout
drains old SSE connections during graceful reload. The browser reconnects with a
fresh snapshot. Access logs omit cookies, authorization and OAuth query strings.

The certificate uses `/etc/letsencrypt/live/beatit.changkyun.kim/{fullchain,privkey}.pem`.
Initial issuance can run while Cloudflare Tunnel still serves the hostname:

```sh
sudo certbot certonly --manual --preferred-challenges http \
  --manual-auth-hook /etc/beatit-nginx/acme-hook.py \
  --manual-cleanup-hook '/etc/beatit-nginx/acme-hook.py cleanup' \
  --deploy-hook /etc/beatit-nginx/renew-deploy.sh \
  --non-interactive --cert-name beatit.changkyun.kim -d beatit.changkyun.kim
```

The HTTP-01 auth hook writes a public challenge into Nginx's persistent webroot
and also copies it into the running app for the initial Tunnel route. Once DNS is
direct, renewal uses public port 80 independently of application availability.
The existing snap-managed `snap.certbot.renew.timer` renews this certificate and its stored deploy hook
reloads only `beatit-nginx.service`. Validate the direct route after DNS cutover:

```sh
sudo certbot renew --cert-name beatit.changkyun.kim --dry-run
systemctl list-timers snap.certbot.renew.timer
curl -I http://beatit.changkyun.kim/
curl https://beatit.changkyun.kim/api/health
```

Before changing DNS, verify the trusted certificate and candidate origin from the
client without disabling certificate validation:

```sh
curl --resolve beatit.changkyun.kim:443:144.24.83.69 \
  https://beatit.changkyun.kim/api/health
```

## Upload and media delivery

Set `BEATIT_UPLOAD_MODE=direct` in the protected deployment environment and
recreate the existing Compose project with its MySQL TLS overlay. `/api/system`
advertises the mode. The browser creates an authenticated staging session,
streams the whole File in one `PUT /api/uploads/{id}`, then requests publication
into MySQL/R2. The upload timeout is 30 minutes with a 30-second no-progress
deadline. The UI displays bytes during transfer and a separate saving state.
Only this bounded publication phase uses transient status reads.

The backend authenticates before reading the body, checks declared size and disk
reservation, and streams into private staging without a second full-sized copy.
An incomplete PUT rolls back to offset zero; a lost acknowledgement is reconciled
with the session offset. The existing 8 MiB PATCH protocol remains available when
`BEATIT_UPLOAD_MODE=chunked`. Staging limits, ownership and expiry are unchanged.
Enable direct mode after changing DNS. A retained browser proxy connection can
still reject the whole-body PUT with 413; the client then checks the same session
and continues with bounded chunks. It accepts an already-full offset without
resending bytes. The initial application file-size check remains authoritative.

`BEATIT_MEDIA_BASE_URL=/media` retains existing playback URLs. For DNS-only clients,
the application serves authenticated owner-scoped `/media/{id}/{source}` requests,
including HEAD, byte ranges and If-Range. The selected verified AAC rendition,
immutable object bytes and ETag come from one manifest. All media responses,
including errors, are private/no-store. Direct delivery reports
`X-Beatit-Media: origin`. Original downloads are unchanged. The existing R2 Worker
and Tunnel remain available for clients with cached proxied DNS and rollback.

### Measured 305.6 MiB transfer

The same 320,415,531-byte MP4 used in the earlier Cloudflare test was uploaded
from the Mac through trusted direct HTTP/2 HTTPS using the existing login session.
One PUT transferred all bytes in **67.03 seconds (38.24 Mbps)**; durable MySQL/R2
publication took a further **6.41 seconds**. The persisted input size and SHA-256
matched the source. The earlier 39-chunk Cloudflare transfer took approximately
534 seconds, about eight times as long. This is one observed comparison, not a
controlled network benchmark or a Chrome throughput measurement. Cached Chrome
HTTP/3 connections can retain the old proxy route after DNS changes.

An authenticated direct range request returned 206 with the original same-origin
URL, verified AAC content, a SHA ETag and `X-Beatit-Media: origin`. Anonymous
access returned 401. See [sanitized measured evidence](benchmarks/direct-origin-2026-11-09.json).

## Workspace events and session lifetime (issue #15)

A visible authenticated workspace has one `/api/events` EventSource, with a
fresh owner-scoped history/system snapshot on connect. Job progress, completion,
edits, deletions and system changes arrive as SSE events. There is no recurring
healthy `/api/auth/me`, `/api/jobs`, `/api/system` or job-detail browser polling.
The frontend suspends hidden connections, cleans up on logout/unmount and uses
bounded exponential reconnect with jitter. A reconnect discards stale delivery
work; account changes and definitive auth expiry clear the previous workspace.

The backend shares one three-second metadata sampler among connected owners;
unchanged lease heartbeats do not send job events or reread full result JSON.
System information is cached for 60 seconds. Streams recheck session revocation
every 15 seconds and send keepalives at the same interval. Per-process limits are
64 streams globally, eight per owner and 16 queued events per stream. Overflow or
restarts require a new snapshot rather than durable event replay. A selected job
outside the latest 100 history entries can be pinned with `?job_id=...`.

The browser retains the existing opaque Secure HttpOnly cookie; it never copies
credentials into JavaScript storage. An authenticated request renews a session
only when at most 24 hours remain, extending its seven-day idle window up to a
30-day absolute lifetime. An open SSE stream never extends the cookie by itself.
Migration `006_session_renewal.sql` preserves existing tokens and bounds legacy
sessions from their migration timestamp. Revocation and logout take effect on
ordinary requests immediately and on an open stream within its auth-check window.

## DNS cutover and rollback

The previous record was a proxied CNAME to the `chunsik` Tunnel
`1efe2797-d5d9-4d81-baf1-2a5399929bc6.cfargotunnel.com` (TTL Auto). Replace that
record with an **A `beatit` → `144.24.83.69`, DNS only**. Retain the existing
Tunnel ingress and R2 Worker route until cached DNS expires. The canonical
hostname and OAuth callback do not change.

For rollback, first set `BEATIT_UPLOAD_MODE=chunked` and recreate the service,
then restore the previous proxied Tunnel CNAME. Keep the current image or use the
recorded previous image/environment under the deployment's `rollbacks/` directory.
The session migration permits the previous image's login inserts; existing
durable jobs, artifacts and revisions must be checked before and after restart.
Stop only `beatit-nginx.service` if direct HTTPS must be disabled; never stop
Tailscale or restart the unrelated global Nginx service as part of this change.
