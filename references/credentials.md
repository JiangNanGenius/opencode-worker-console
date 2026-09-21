# Credential references

This bridge keeps credential handling narrow. There are three distinct paths, and
they should not be mixed:

1. **Provider authentication is owned by OpenCode.** The bridge reads existing
   OpenCode credentials only in-process to make its fixed official quota calls.
   Provider keys are not collected by this bridge in prompts, argv, task JSON,
   browser data or this CLI. Keep logging in through OpenCode.
2. **Bridge/server authentication is generated locally.** The OpenCode server
   binds to loopback with an owner-only generated password that the bridge
   supplies deliberately through the local process environment and loopback
   authentication. That deliberate transport is separate from keeping values out
   of prompts and task text.
3. **Deployment and other application credentials** should be used through an
   existing authenticated login or tool whenever one exists. Only when no such
   tool exists should you add a local reference with `credential register`.

The goal is that the model does not need the raw secret. A referenced value is
resolved inside the invoking CLI process and passed directly to a child through
its environment. The child's stdout/stderr is captured and redacted before any
part of it can reach the model-visible tool boundary.

This bridge does not redact the raw native OpenCode gateway, its process logs or
arbitrary tool output. Those are not guaranteed to be credential-free and may
already contain credential material in native session history.

This is exposure reduction for normal authorized workflows. It is **not an OS
sandbox** and does not stop a child command, or a same-user agent with full
Auto Approve, from deliberately reading or forwarding a value. Non-captured side
effects (files, sockets, external logs, child-managed children) are not covered.
Late redaction at the bridge cannot erase what a model already saw in native
OpenCode history or in a previous prompt, and it cannot detect every arbitrary
text secret.

## Register, list, remove

Storage is metadata only, under the owner-only state directory. No command
returns a value. There is deliberately no `get`/`show-value` action and no
`--value`/positional plaintext argument.

```sh
# The owner-only file already exists, provisioned by your deployment tooling outside Git.
delegate-opencode credential register deploy --file /private/example/deploy-token
delegate-opencode credential list
delegate-opencode credential remove deploy
```

```sh
# An existing environment variable name already exported by your login tooling.
delegate-opencode credential register deploy-env --env EXAMPLE_DEPLOY_TOKEN
```

`list` returns names, source types and safe metadata only (source, reference,
registered timestamp, availability). `remove` deletes reference metadata only;
it never deletes or modifies the source file or environment.

Ark Agent Plan inference authentication remains in OpenCode. Optional AFP telemetry uses a
separate Volcengine control-plane AK/SK pair. Put each value in its own owner-only file outside
every repository and register these fixed metadata names:

```sh
delegate-opencode credential register volcengine-control-ak --file /private/path/ark-control-ak
delegate-opencode credential register volcengine-control-sk --file /private/path/ark-control-sk
delegate-opencode quota --refresh
```

The same two references can be registered from **Models & routing → Quota query credentials** in the authenticated console. The form accepts only an environment-variable name or an absolute owner-only file path; it never accepts, stores in configuration, or echoes the secret value itself.

The console returns only the safe source label and normalized plan windows. It never returns
the values, their hashes or source paths. Without these references, inference still works and
Ark quota stays unknown.

File references must be absolute, regular, owned by the current user, owner-only
(0600 or stricter), non-empty, at most 64 KiB and not a symlink. A file already
tracked by Git is rejected at registration; this checks the current index only and
does not prevent a later commit of an untracked file, so keep references outside
repositories. References are resolved lazily; a missing or invalid source is
skipped for redaction and reported as a generic safe error for `credential run`,
with no value material.

File references are preferred for new runtime bindings. An environment reference
contains an existing variable name; a file reference contains one raw secret value,
not a dotenv file with `KEY=value` lines. An environment reference
is resolved in the process that runs `credential run`; the already-running bridge
server cannot acquire exports added to a later shell, and env values are not
persisted. File references also avoid command substitution such as
`--flag "$(cat file)"`.

## Run a command with a reference

`credential run` takes an explicit argv list (no shell), injects each resolved
reference as a named environment variable, and returns sanitized captured output.

```sh
delegate-opencode credential run --use DEPLOY_TOKEN=deploy --timeout 60 -- \
  /usr/local/bin/my-deploy-tool --environment staging

delegate-opencode credential run --use DB_PASSWORD=db --use API_KEY=api --timeout 120 -- \
  python3 scripts/rotate.py
```

- Values are never interpolated into argv. Supplying an argument that already
  contains a resolved value or a recognized encoding (literal, URL, JSON-escaped
  or Base64) is rejected.
- The child inherits the caller's authenticated environment plus only the named
  injections. The bridge process environment is not mutated.
- stdin is closed so the command cannot hang waiting for interactive input.
- stdout/stderr are captured in bounded memory. Injected values and common
  reversible representations (URL percent-encoding, JSON escaping, standard and
  URL-safe Base64 and, for `user:password` values, their HTTP Basic form) are
  removed before anything is returned. Registered references are also included
  in the bridge's normal redaction for `submit`, `steer`, `collect` and
  `transcript`.
- The child's exit status is preserved. Timeout exits `124`; exceeding the output
  limit exits `125`. On either, the bridge kills the child process group and
  suppresses the captured stdout/stderr entirely, returning only safe status,
  byte counts and limit metadata. This avoids exposing a fragment cut before
  exact-value redaction.
- If any reference fails to resolve, the command does not run and no unsanitized
  output is produced. `credential run` is fail-closed for the references it is
  asked to use; only general bridge redaction skips an optional missing or
  invalid registration.

## Operational and SSH secrets

For SSH, `scp` and `rsync` work, prefer the user's existing `~/.ssh/config` host aliases
and `ssh-agent` identities; the bridge and its prompts never need the raw key or
password, and only a genuinely new login or consent belongs to the user. When a script
truly needs a new secret value, register a metadata-only reference and invoke the
command through `credential run`, so the value stays out of prompts, argv, task JSON,
logs and transcripts. Operational target names such as `ssh:<host>:<service>` are plain
identifiers, never credentials, and follow the normal task-field rejection and redaction
rules like any other submitted text.

## Limits, rotation and history

- Redaction covers known local credentials and registered references, plus
  recognizable key formats. It cannot promise that every arbitrary text secret
  is caught, especially transformed values. A very short or common registered
  value can also over-redact ordinary text; prefer long, distinctive values.
- Registered values are shared with the bridge's normal redaction, so they are
  rejected in `submit` and `steer` text and removed from `collect`/`transcript`
  output as well as from `credential run` output.
- Removing a reference, deleting its source or rotating a value stops future
  redaction of the old value. Historic task artifacts, saved transcripts and
  native OpenCode sessions are **not** rewritten, so previously captured
  plaintext is not erased by this feature. Delete or rotate at the source, and
  avoid placing values in chat, prompts, steer text, command substitution or
  command flags in the first place.
- The bridge does not fetch Keychain values or contact the network for
  redaction.

No separate approval flow is required for ordinary register/list/remove/run use.
