# Passkey-only public access

The console can run two HTTP listeners in one process. The existing LAN
listener keeps username/password authentication. A second listener binds only
to `127.0.0.1` and requires a user-verified WebAuthn session for all protected
console and OpenCode HTTP, SSE and WebSocket paths. Listener roles are assigned
by the server, never by forwarded headers. Both listeners share one collector,
account, credential registry and revocation state.

## Configuration and runtime

Operator-controlled configuration, for example:

```json
{
  "console_passkey": {
    "enabled": true,
    "public_origin": "https://desk.example.com",
    "rp_id": "desk.example.com"
  },
  "console_public_port": 51529,
  "console_trusted_proxies": ["127.0.0.1/32"]
}
```

`public_origin` must use HTTPS on port 443. `rp_id` must equal that exact DNS
hostname. These values come only from configuration; changing either requires
a console restart. An already running console rejects public authentication
and closes authenticated streams if its configured RP no longer matches.
Credentials are scoped to the configured RP, so changing domain requires new
registration. The example domain is the default; other deployments should set
their own origin explicitly.

The existing `console_url`, `console_bind` and LAN allowed origins are preserved.
The public origin is assigned only to the public listener. The LAN listener
accepts direct loopback/RFC1918 IPv4 peers, and does not use X-Forwarded-For to
grant LAN access. Expose only the second port through the public proxy/tunnel.
Do not forward a public tunnel to the LAN password port. The internal OpenCode
server stays on loopback with its separate private Basic credential.

Prepare dependencies explicitly using the original worker/CLI interpreter:

```sh
/Library/Developer/CommandLineTools/usr/bin/python3 scripts/console_runtime.py prepare
/Library/Developer/CommandLineTools/usr/bin/python3 scripts/console_runtime.py check
```

Preparation requires an existing Python 3.12 and uv; `--python`, `--uv`,
`--worker-python` and `--state` can specify alternate paths. It does not download
Python or restart any service. The managed environment lives under
`STATE/runtime/console-auth/<lock-digest>/venv`. All dependencies are version/hash
locked. Only the console uses this environment; service restarts preserve the
recorded worker interpreter. The normal installer prepares dependencies before
stopping observers when passkeys are enabled. An unavailable dependency runtime
fails closed. Configuration changes and code synchronization still require an
explicit console restart.

The TLS reverse proxy must preserve Host/Origin, overwrite X-Forwarded-For with
one sanitized client IP, support WebSocket upgrades and disable SSE buffering.
Only its direct socket address should be trusted. Public cookies are distinct
from LAN cookies and use the `__Host-` prefix. Renaming a LAN password cookie
does not make it valid for the public listener.

## First credential and recovery

1. Sign in at the LAN console and open `/console-passkeys`.
2. Re-enter the current password to obtain a ten-minute one-use enrollment URL.
3. Open that HTTPS URL and register a passkey using device verification.
4. Sign in with the new passkey. Enrollment itself creates no login session.

The enrollment token is carried only in the URL fragment, immediately removed
from browser history and submitted in a POST body. The server stores only its
digest. It authorizes one registration ceremony and cannot access business APIs
or proxy OpenCode. Do not save enrollment URLs in logs or shared documents.
If a registration ceremony is cancelled or expires, request a new link.

Additional credentials and deletion require a passkey login within the last
five minutes. The public site cannot delete the final credential. Deleting a
credential revokes its sessions and closes active streams on the next bounded
authentication check (normally within 30 seconds plus the five-second I/O
timeout). Password/account rotation invalidates all sessions and pending
ceremonies. Legacy sessions remain LAN-only.

If all credentials are lost, the local Mac administrator can create a fresh
registration link without enabling public password authentication:

```sh
python3 scripts/console_auth.py enrollment-grant
```

Run with the same `DELEGATE_STATE`/`DELEGATE_CONFIG` overrides as the deployment.
The command prints an `enrollment_url` for immediate use and persists only its
digest. It does not replace the password, delete credentials or restart services.

## Validation

Run the real signed WebAuthn fixtures with the managed console interpreter:

```sh
<managed-venv>/bin/python -m unittest discover -s tests -p test_console_passkeys.py
python3 -m unittest discover -s tests -p test_console_auth.py
python3 -m unittest discover -s tests -p test_console_runtime.py
```

The passkey suite tests real signatures, user verification, exact origins,
userHandle, one-use challenges, enrollment expiry, credential revocation,
public cookie isolation and idle streams. Running it without the SDK skips the
cryptographic tests; that is not a replacement for the managed-runtime run.
Browser/device enrollment and the deployed TLS/proxy path need separate live
verification.
