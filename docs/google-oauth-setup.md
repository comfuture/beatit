# Google sign-in setup

Beatit uses Google's server-side OpenID Connect authorization-code flow. The first
verified sign-in creates an account, and subsequent sign-ins reuse the Google
`sub` identifier even if the person's email changes. There are no passwords,
anonymous project sessions, browser-held Google tokens, or client-supplied owner IDs.

## Prepare the Google project

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) in
   the Google Cloud project that will own Beatit's OAuth client.
2. Configure the application's branding and audience. For an external application
   left in testing, add both smoke-test Google accounts as test users. The app
   requests only `openid`, `email`, and `profile` scopes.
3. Create a **Web application** OAuth client. Add the exact redirect URI
   `https://beatit.changkyun.kim/api/auth/callback`. Local development may additionally
   use `http://localhost:8000/api/auth/callback`. Hostnames, ports and schemes must
   match exactly; `127.0.0.1` and `localhost` are different origins.
4. Save the client ID and secret in the ignored `.env` file or the deployment's
   secret manager as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
5. Set `BEATIT_PUBLIC_URL` to the corresponding origin without a path, for example
   `https://beatit.changkyun.kim`. Keep `BEATIT_ALLOWED_HOSTS` consistent with that
   hostname. The callback URL is derived exclusively from this configured origin,
   never from an incoming Host or forwarded header.

There is no need to enable Gmail, Drive, or Calendar APIs, grant offline access,
or create another authentication provider. The Google client secret stays on the
Python server. OCI MySQL stores durable project state and sessions; private
S3-compatible object storage holds media and generated files.

See [.env.multiuser.example](../.env.multiuser.example) for the complete environment
contract. Do not commit or send credential values in chat. The project reference,
public application URL and readiness status can be shared without sending secrets.

## Login and session behavior

- `GET /api/auth/google` starts sign-in and always offers account selection.
- `GET /api/auth/callback` consumes a ten-minute, browser-bound, single-use state
  record and exchanges the code with PKCE. Authlib verifies the RSA signature;
  Beatit requires the Google issuer, intended client ID, expiry, nonce, a stable
  subject and a verified email. Unknown signing keys trigger a refresh.
- `GET /api/auth/me` returns `{ "user": null }` or the current account's ID,
  email, name and picture. Responses are not cached.
- `POST /api/auth/logout` revokes the current session in MySQL and removes
  its cookie. Signing into another account also rotates the browser's old session.

Session tokens are opaque random values. Only their SHA-256 hashes are stored in
MySQL; they expire after seven days. Production cookies use the `__Host-`
prefix, `Secure`, `HttpOnly`, `SameSite=Lax`, and `/` path with no Domain attribute.
HTTP localhost uses separate unprefixed cookies. Google access/ID tokens are
discarded after the callback. A missing OAuth configuration disables sign-in with
a setup error and never grants anonymous project access.

Use HTTPS at the public reverse proxy. Forward only to the private application
listener; configure Uvicorn's trusted proxy addresses to match the actual proxy
instead of trusting forwarded headers from arbitrary clients.

## Verify with two accounts

Use two independent browser profiles or contexts and two Google test users.
Sign in as A and create a short transcription; sign in as B in the second context
and verify that A's history, project URL, file URLs, waveform, logs, edits and
downloads return 404. Submit B's short clip while A is queued/running. Log out A,
then sign in as B in that same browser: no A selection, loaded media or edits may
remain. Recreate application scratch and verify both accounts retain their own
results through MySQL and object storage.

Mocked Google assertions and synthetic local sessions exercise implementation
behavior but do not replace this real provider/deployed-storage smoke test.

Google's [OpenID Connect documentation](https://developers.google.com/identity/openid-connect/openid-connect)
describes the client registration and server flow. Authlib's
[OAuth HTTP client guide](https://docs.authlib.org/en/stable/oauth2/client/http/index.html)
documents the PKCE client used by Beatit.
