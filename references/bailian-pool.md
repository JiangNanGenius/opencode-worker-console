# Alibaba inference credential pool

`scripts/bailian_pool.py` is an optional loopback proxy for the fixed Alibaba
Cloud Model Studio Token Plan endpoint already declared in
`providers.BAILIAN_BASE_URL`. It lets the existing Alibaba provider consume two
(or more) Keychain-held inference keys sequentially without exposing any key to
OpenCode, task state, logs or this repository.

It is off unless `bailian_pool.enabled` is exactly `true`. With the pool
disabled, `server.runtime_overlay` keeps the previous
`provider_api_key_files["bailian-token-plan"]` behavior byte-for-byte.

## Complete private configuration example

Add this object to the owner-only pool configuration
(`~/.config/opencode/delegate-pool.json` by default). The example Keychain labels must be provisioned locally before enabling the
pool; configuration contains only service/account metadata.

```json
{
  "bailian_pool": {
    "enabled": true,
    "listen_port": 51530,
    "request_timeout_seconds": 900,
    "slots": [
      {
        "id": "seat-1",
        "keychain": {
          "service": "workerdesk.bailian-token-plan",
          "account": "seat-1"
        }
      },
      {
        "id": "seat-2",
        "keychain": {
          "service": "workerdesk.bailian-token-plan",
          "account": "seat-2"
        }
      }
    ]
  }
}
```

The optional legacy-compatible file form replaces `keychain` with an absolute
owner-only path (mode `0600`, owner-owned regular file, at most 64 KiB):

```json
{
  "id": "seat-1",
  "file": "/private/path/seat-1.key"
}
```

A slot must contain exactly one of `keychain` or `file`. Unknown slot fields,
including anything that looks like a plaintext `key`/`secret`/`token` value, are
rejected before a process starts. `id` values must be unique. `listen_port`
defaults to `51530`; `request_timeout_seconds` defaults to `900`.

An optional non-secret `"generation": "2026-10-21"` label can be added to a
Keychain slot. The pool already detects a changed Keychain value and makes the
slot eligible again; updating this label as well makes the provider-level
credential identity change, so a quota/billing circuit bound to the old key also
detaches on the next operator check. Any short safe label works.

When the pool is enabled, `provider_api_key_files["bailian-token-plan"]` is
intentionally ignored for inference. OpenCode is pointed at
`http://127.0.0.1:<listen_port>` and authenticates with the existing owner-only
`STATE/server-password` file through the provider's `apiKey: {file:...}`
mechanism. The proxy removes that local header and injects the selected slot's
`x-api-key` for the fixed upstream request. Neither key is written to the
runtime overlay.

## Request behavior

- Listens on `127.0.0.1` only and requires the existing server password (either
  `x-api-key` or a Bearer token for manual probes).
- Accepts only `POST /messages` and `POST /messages/count_tokens`. The upstream
  host, scheme and path are fixed; caller-supplied absolute URLs are rejected.
  A query string is forwarded to the same fixed path so Anthropic beta
  semantics are preserved.
- Selects the first eligible slot in configured order. A slot stays selected
  until it has a confirmed blocking error for the current credential
  generation.
- For an explicit pre-response quota/balance rejection (HTTP 400/402/403/429
  with a recognised quota/balance code or message), marks that slot exhausted
  and retries the same exact buffered request with the next eligible slot.
- For an explicit pre-response authentication rejection (400/401/403 with a
  recognised authentication/permission marker, or an unmarked 401/403), marks
  the slot `auth` and retries the next eligible slot.
- A temporary 429 (`rate_limit_error`, throttling, etc.) is relayed as-is and
  does **not** exhaust the slot or trigger a retry.
- Transport failures, 5xx responses, redirects, unknown 4xx responses, and any
  response whose body has started streaming are never replayed. Redirects are
  refused with a local 502 and no `Location` header is exposed.
- Streaming responses are relayed chunk by chunk with a flush per chunk; the
  application bytes and status are unchanged. Hop-by-hop headers are stripped.
- If every eligible slot is blocked, the proxy returns a provider-level
  `rate_limit_error`; if every configured reference is unavailable it returns
  `api_error` (503). A single exhausted seat never globally blocks the other.

## State and observability

Safe state is persisted at `STATE/bailian-pool.json` with mode `0600`:

```json
{
  "version": 1,
  "active": "seat-1",
  "slots": {
    "seat-1": {
      "state": "ok",
      "generation": "g1_<non-reversible digest>",
      "updated_at": 1780000000.0
    },
    "seat-2": {
      "state": "exhausted",
      "kind": "quota",
      "http_status": 429,
      "generation": "g1_<non-reversible digest>",
      "updated_at": 1780000000.0
    }
  }
}
```

The generation is a non-reversible digest of the slot id and current key value.
A blocking state is bound to that generation: a rotated Keychain value or file
automatically becomes eligible again on the next request. A late success from
an already-dispatched request cannot clear a block for the same generation.
Only a changed generation (or deliberate operator reset of the private state)
clears it. No key value, Authorization header, request body or upstream error
body is stored.

Buffered upstream error bodies are relayed only after the resolved slot key and
its common representations are replaced with `<redacted>`; the outgoing
Content-Length is recomputed from the replaced bytes. Credential-bearing
response headers (`Authorization`, `x-api-key`, `proxy-authorization`) are
suppressed, and other error-header values are redacted too. gzip/deflate error
bodies are decoded for inspection and relayed decompressed; an unknown or
uninspectable encoding is refused rather than relayed. Successful model and SSE
content is passed through byte-for-byte and is never redacted.

Safe active metadata is available through:

```sh
python3 scripts/bailian_pool.py status   # state + configured slots, no secrets
python3 scripts/bailian_pool.py check    # validate config and list slot sources
```

The authenticated `GET /health` endpoint returns the same safe snapshot
(`enabled`, `port`, `active`, per-slot `state`/`kind`), with no references.

## Lifecycle

- `service.start()` validates the pool configuration before launching anything,
  starts `bailian_pool.py` before OpenCode when enabled, and waits for
  authenticated `/health` readiness.
- `service.stop()` and `service.reload_runtime()` include the pool process.
  `reload_runtime` validates the new configuration before stopping the old
  stack, so a typo cannot leave services down.
- When the pool is disabled, no pool process is started and the old runtime
  overlay/key-file path remains in effect.

## Deployment notes for the operator

1. Keep the real keys in the existing Keychain service/account items; do not put
   values in configuration, argv, task text or this document.
2. Add the `bailian_pool` block to the private configuration and run
   `python3 scripts/bailian_pool.py check`.
3. Restart idle execution services through the existing operator flow
   (`delegate-opencode service restart`, or the console's idle-safe settings
   restart). This refuses while tasks are active; wait for them to finish
   before applying the new execution configuration.
4. Confirm the listener with the service health check. After the first
   authenticated request, `python3 scripts/bailian_pool.py status` shows the
   active seat and any safe error states. `status` never reads Keychain values,
   so a fresh state file reports configured seats as `ok` without proving the
   credentials are present.
5. Rollback is configuration-only: set `enabled` to `false` (or remove the
   block) and restart. The old `provider_api_key_files` behavior returns; keep
   the old key file in place until the pool is accepted.

## Focused verification

```sh
python3 -m unittest tests.test_bailian_pool tests.test_bailian_proxy \
  tests.test_provider_identity -v
```

These tests use a real loopback fake upstream and synthetic Keychain values.
They cover local authentication, full JSON framing with an explicit
Content-Length, streaming flush and exact bytes, quota/auth retries before
forwarding, no replay on transport or streamed content, all-exhausted provider
failure, generation-bound recovery, query/log redaction, and disabled-pool
compatibility. They never read a real credential.
