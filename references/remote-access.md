# Console authentication and remote access

Worker Desk authenticates the console itself. Every console API, native
OpenCode HTTP path and WebSocket upgrade requires a server-side session created
through an explicit login. The old automatic, deterministic HMAC cookie was
removed and is never accepted.

## Fresh default credentials

A **fresh** initialization creates a single admin account:

```
username: admin
password: admin
```

This default is written only when no account exists. Upgrading or restarting an
existing installation never resets or replaces an existing credential.

**Replace the default before publishing.** Treat repository contents as code,
not as a credential store: never commit a real deployment password, and never
assume the absence of a password from source or history proves there is none.
Provision the real account locally:

```sh
# Owner-only state; read the password from a pipe, file descriptor or prompt.
printf '%s' "$CONSOLE_PASSWORD" | python3 scripts/console_auth.py set-user \
  --username admin --password-stdin

# Or omit --password-stdin to be prompted twice with hidden input.
python3 scripts/console_auth.py set-user --username admin
```

`set-user` accepts no positional password and no `--password` argument. It stores
only a PBKDF2-HMAC-SHA256 verifier with a per-user random salt (at least 600000
iterations) under the owner-only state directory (`console-auth/account.json`,
mode 0600 in a mode 0700 directory). It prints safe metadata only. Changing the
password invalidates every existing session.

Sessions are random 256-bit tokens stored only as SHA-256 digests with a 12-hour
expiry. Each session is also bound to the exact account revision that issued it,
so a rotated password, a replaced or malformed account file, or a crash between
writing the account and clearing the session registry all invalidate old tokens
rather than leaving one usable. Logout revokes the session server-side and
clears the cookie. Sessions survive a compatible `install.py --live` restart of
the UI/observer processes because they live in state, not process memory.

## Local administration commands

```sh
# Loopback only (default).
python3 scripts/console_auth.py configure --bind 127.0.0.1

# Explicit LAN use: bind all interfaces and allow the LAN origin.
python3 scripts/console_auth.py configure --bind 0.0.0.0 \
  --origin http://192.168.1.20:31337

# Root-hosted reverse proxy on a real public origin, behind a same-host proxy.
python3 scripts/console_auth.py configure --bind 127.0.0.1 \
  --origin https://desk.example.test --trust-proxy 127.0.0.1/32

# Stop trusting forwarded headers again.
python3 scripts/console_auth.py configure --clear-trusted-proxies
```

`configure` updates `console_bind`, `console_allowed_origins` and
`console_trusted_proxies`. It preserves unrelated configuration and all
authentication state, and needs no user secrets. Every internal address stays
pinned to loopback exactly as before: `server_url` (the OpenCode upstream) and
`console_url` are still plain HTTP on `127.0.0.1`, and only `console_bind` may
be `127.0.0.1` (default), `0.0.0.0`, or a specific local IPv4 address.

Allowed origins are validated strictly: `http`/`https` only, a concrete host,
optional port, and no userinfo, path, query, fragment or wildcard. The internal
`console_url` is always an implicit allowed origin.

### Trusted reverse proxies

`--trust-proxy` accepts a repeatable IPv4 address or CIDR entry that is the
**direct socket peer** of the console, not the browser. A bare address is stored
normalized as `/32` (for example `--trust-proxy 127.0.0.1` becomes
`127.0.0.1/32`). At most 64 entries are accepted, and a catch-all such as
`0.0.0.0/0` is rejected. With no trusted proxy configured, every request is
attributed to its socket peer and `X-Forwarded-For` is ignored entirely.

> **Restart required.** The running console caches the bind, origin and
> trusted-proxy settings at process start, so `configure` alone does not change
> a live console. Restart it with `python3 scripts/install.py --live` (an
> existing installation; it restarts the skill, pool observer and console while
> retaining the OpenCode process and its sessions) or restart the console
> service with `python3 scripts/install.py` when no tasks are active. Trusted
> proxies do not widen OpenCode: the gateway upstream remains loopback-only.

Only when the socket peer is inside a trusted network is a single sanitized
`X-Forwarded-For` value used for the login-throttle bucket. The proxy must
**overwrite** the header with the real client IP; a missing value, a comma
separated chain or a malformed value falls back to the socket peer, and a
header from an untrusted peer is ignored. Never append or forward a
client-supplied chain: parsing it would let a caller choose its own bucket.

### Same-user loopback limits

Trusting `127.0.0.1/32` is a trust decision about **every local process that
can reach the console port**, not about one proxy binary. Any such process that
can connect and write `X-Forwarded-For` can choose its throttle identity, so
keep the console bound to loopback and let only the reverse proxy reach it.
Recommended ingress isolation, in rough order of importance:

- Keep `console_bind` at `127.0.0.1` whenever a same-host proxy is fronting the
  console; never also expose the console port on a LAN interface.
- Trust the narrowest possible source: the proxy's actual address, usually
  `127.0.0.1/32`. Do not list `0.0.0.0/0` or a broad LAN CIDR.
- Bind the proxy to the public interface only, and firewall the console port so
  nothing else on the host or network can connect.
- Treat local process isolation as absent. A signed-in administrator already
  runs with the host user's execution privileges; this is not multi-tenant
  isolation.

## Reverse proxy requirements

Front the console with a reverse proxy that:

- Preserves the public `Host` **including its port**, because allowed origins
  are compared with the request authority.
- Overwrites `X-Forwarded-For` with the sanitized single client IP, and lists
  the proxy's direct address in `console_trusted_proxies`.
- Preserves `Origin` on mutation requests.
- Disables response buffering for SSE and forwards the `Upgrade`/`Connection`
  headers for WebSocket, on the same root path as the console.
- Terminates TLS for an `https` origin so the session cookie is marked `Secure`.

The configured `console_allowed_origins` must contain the exact public origin
the browser uses. A request whose `Host` is not an allowed origin authority is
rejected with 403, and a cross-origin or `Sec-Fetch-Site: cross-site` request is
rejected with 403 on login, mutations and WebSocket handshakes. Forwarded
headers never affect that authorization decision; they can only select a
throttle bucket when the socket peer is explicitly trusted, as above.

### nginx (root-hosted TLS; HTTP, SSE and WebSocket)

Define the upgrade map once in the `http` block:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Then a complete server block. Replace `desk.example.test` and the certificate
paths with your own values; `127.0.0.1:31337` must match the configured
`console_url` port.

```nginx
server {
    listen 443 ssl;
    server_name desk.example.test;                       # placeholder

    ssl_certificate     /etc/letsencrypt/live/desk.example.test/fullchain.pem;  # placeholder
    ssl_certificate_key /etc/letsencrypt/live/desk.example.test/privkey.pem;    # placeholder

    location / {
        proxy_pass http://127.0.0.1:31337;               # loopback upstream
        proxy_http_version 1.1;

        # Keep the public authority (host AND port): the console validates it.
        proxy_set_header Host $http_host;
        proxy_set_header Origin $http_origin;

        # Overwrite, do not append, the single sanitized client IP.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket upgrade for native OpenCode streams.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Stream SSE without buffering or a send/read cutoff.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding off;
    }
}
```

`$http_host` keeps a non-default port in the Host header; `$remote_addr`
replaces any caller-supplied `X-Forwarded-For`. Configure the matching trust so
the console attributes the request to the real client:

```sh
python3 scripts/console_auth.py configure --bind 127.0.0.1 \
  --origin https://desk.example.test --trust-proxy 127.0.0.1/32
```

The console derives the `Secure` cookie flag from the configured HTTPS origin,
not from `X-Forwarded-Proto`; the forwarded scheme is forwarded for backend
observability only.

### Caddy (root-hosted TLS; HTTP, SSE and WebSocket)

Caddy obtains TLS automatically and handles WebSocket upgrades and SSE flushing;
`flush_interval -1` disables response buffering.

```caddyfile
desk.example.test {                        # placeholder domain
    reverse_proxy 127.0.0.1:31337 {        # loopback upstream
        # Preserve the public authority including a non-default port.
        header_up Host {hostport}
        # Overwrite any caller-supplied X-Forwarded-For with the real client IP.
        header_up X-Forwarded-For {remote_host}
        # Stream SSE without buffering.
        flush_interval -1
    }
}
```

Then trust the same-host proxy:

```sh
python3 scripts/console_auth.py configure --bind 127.0.0.1 \
  --origin https://desk.example.test --trust-proxy 127.0.0.1/32
```

## Security notes

- Login responses are deliberately generic: a bad username, a bad password and
  an oversized, wrong-typed or malformed-Unicode body never reveal which field
  failed and never raise a traceback. Failed attempts are throttled per peer and
  return 429 with `Retry-After`; the lock is persisted across trivial restarts.
- The throttle bucket is the socket peer, unless that peer is an explicitly
  configured trusted reverse proxy, in which case exactly one sanitized
  `X-Forwarded-For` address is used. Without a trusted proxy all requests share
  the direct peer bucket; through a trusted proxy, distinct client IPs get
  distinct buckets. If the throttle reservation cannot be persisted, login
  fails closed with a generic 503 rather than admitting unlimited attempts.
- A failed server-side logout revocation is never reported as success: the
  browser cookie is still cleared, but the response is a generic 503.
- Login attempts, passwords and request URLs are never logged by the console.
- The account hash, session registry and network configuration are not exposed
  through console settings, catalogs or error messages.
- Keep `server_url` and `console_url` on loopback. The proxy target is verified
  to be loopback before the backend Basic credential is injected, for GET, POST,
  PATCH, DELETE and WebSocket upgrades alike.
- Long-lived SSE/HTTP streams and WebSockets re-check the session every 30
  seconds and every socket read/write has a finite timeout, so logout, expiry or
  password rotation closes an open connection within roughly that interval plus
  one short I/O timeout. An idle or slow producer therefore does not hold a
  revoked stream indefinitely.
- For LAN HTTP the cookie is a normal `HttpOnly; SameSite=Strict` cookie; for a
  configured HTTPS origin it is additionally marked `Secure`. Prefer the
  reverse-proxy TLS path for anything beyond a trusted LAN.
- A malformed account file or initialization marker fails closed (login is
  unavailable) and is never replaced by the default credential. Repair it with
  an explicit `set-user`.

See [credentials.md](credentials.md) for metadata-only local credential
references and the redacting child runner.
