# Worker Desk

A local console for OpenCode sessions and a durable, multi-model worker queue.

Manage task ownership, parent/child tasks, worker profiles, automatic routing, sessions and account usage in one place. Use it with Codex through the included `delegate-opencode` skill, or with any coordinator through the CLI and JSON task specs. The console UI supports English, Simplified Chinese (`zh-CN`), Traditional Chinese (`zh-TW`), Japanese (`ja`) and Korean (`ko`); pick one from the language selector in the header. The browser locale is detected on first visit, unsupported locales fall back to English, and a manual choice is remembered in local storage. The CLI and API use English.

## What it does

- **Console access:** username/password sign-in, expiring sessions and sign-out protect both the dashboard and native OpenCode gateway. Optional LAN binding and explicit reverse-proxy origins are configured locally.
- **Tasks:** delegate coherent bounded outcomes to general-purpose agents, including investigation, implementation, operations and verification. These examples are not a capability allowlist; select work by task fit, available tools and the user's authorization. Inspect evidence and cancel safely.
- **Sessions:** browse across projects, search, create, rename, archive/restore, fork, permanently delete and open the native OpenCode conversation. Forking does not send a prompt.
- **Models:** add, remove and disable profiles; choose provider/model and optional reasoning variant; configure routing and concurrency. Settings apply only when workers and native sessions are idle.
- **Profile routing:** coordinators can select a profile from task semantics or use coarse automatic routing based on urgency and complexity. All tiers can own complete tasks.
- **Conversation concurrency:** each owning Codex conversation can run four workers by default. Other conversations have independent slots, with no shared global or provider cap. Shared-file/resource conflicts and account availability still govern dispatch. There is no per-task step or time cap: a worker iterates until its model stops or the task is cancelled.
- **Usage:** DeepSeek balance and Kimi Coding plan windows, sample age and reset time. Other OpenCode providers can run tasks without a usage adapter.
- **Workspace coordination:** disjoint shared write scopes, resource locks, or isolated Git worktrees based on the current working tree. Review a patch before applying it.
- **Agent controls:** guide a running worker, bind sessions to workspaces, perform user-authorized deletion, and optionally clean old owned data when disk space is low.
- **On-demand transcripts:** inspect a worker task or native session with messages, tool inputs, outputs and errors; page through history or explicitly export the full conversation. Regular reports stay concise.
- **Credential references:** provider authentication stays with OpenCode and the bridge server password stays local. For an extra secret, register a metadata-only file/env reference and run a command with values injected through the child environment and captured output redacted before the model sees it. Exposure reduction, not a sandbox.
- **Error bridge:** model/API errors, tool failures, command exit codes, retry state and connection failures reach the coordinator through `status`, `wait` and `collect`, with credential redaction. Pending questions are included in collected results.
- **Billing recovery:** confirmed model billing errors block further provider dispatch despite positive usage telemetry. This includes Kimi's HTTP 403 monthly usage limit, which its ordinary usage windows may not reveal. The coordinator autonomously selects an available alternative provider and continues after reviewing partial work. The bridge does not blindly replay dispatched operations. Ordinary positive usage telemetry cannot clear a hidden monthly limit.
- **Durability:** persistent tasks, observed session recovery without blindly replaying a prompt, and cancellation confirmation before releasing ownership.

No Node build step, database service or paid framework is required. The runtime uses Python's standard library and vanilla HTML/CSS/JavaScript.

## Requirements

- Python 3.9+ and Git. Existing OpenCode is reused; if missing, the installer automatically installs the pinned official version into user storage (npm first, checksum-verified GitHub release fallback).
- OpenCode 1.18.30 is the integration-tested version. The cross-project session API is experimental; compatibility with older versions is not promised.
- macOS or Linux/POSIX. Native Windows is not supported (`fcntl` and POSIX processes are used).
- Configure your provider in OpenCode first. The application reuses OpenCode's local credentials and does not collect API keys in the browser.

## Install

```sh
git clone https://github.com/JiangNanGenius/opencode-worker-console.git
cd opencode-worker-console
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL
~/.local/bin/delegate-opencode console --open
```

The first command maps the three default profiles to your chosen model. You can then change them in **模型与调度**. To opt into the DeepSeek/Kimi preset instead:

```sh
python3 scripts/install.py --preset deepseek-kimi
```

This preset uses the `max` reasoning variant for all three models. Existing installations
retain their saved settings; select the highest supported variant in **Models & routing**
when updating a profile or choosing another model.

Existing installations retain their model configuration. Updating the checked-out source and running `python3 scripts/install.py` replaces runtime code after an idle-state check. Use `--no-start` for installation without launching services.

The installer prints the local addresses and installs:

| Item | Default location |
| --- | --- |
| CLI | `~/.local/bin/delegate-opencode` |
| Optional Codex skill / runtime | `~/.codex/skills/delegate-opencode` |
| Configuration | `~/.config/opencode/delegate-pool.json` |
| Private state, evidence and worktrees | `~/.local/state/delegate-opencode` |

`DELEGATE_CONFIG`, `DELEGATE_STATE` and `DELEGATE_INSTALL` support alternate installations. Services start from the calling application and run in the background. After a reboot, the next `submit` or `console --open` starts them again. There is no automatic login service.

## Delegate a task

```sh
delegate-opencode submit --directory /absolute/project \
  --group-title 'Parser maintenance' --profile auto --urgency fast \
  --title 'Investigate empty input' \
  'Find the empty-input path and summarize relevant files and tests with line evidence.'

delegate-opencode status
delegate-opencode collect JOB_ID
delegate-opencode cancel JOB_ID
```

Read-only is the default task intent. Writing requires literal relative scopes. Worker Auto Approve is enabled by default; workers can use tools and ordinary shell commands without permission prompts. Supply known checks to guide execution:

```sh
delegate-opencode submit --directory /absolute/project --mode write \
  --scope src/parser.py --command 'python3 -m unittest -v' \
  --acceptance 'Preserve existing behavior except the specified empty-input fix.' \
  'Implement the reviewed empty-input fix and run the authorized tests.'
```

Use `--workspace isolated` for changes requiring a separate Git worktree. Review `changes.patch`, then run `integrate JOB_ID` to check applicability and `integrate JOB_ID --apply` to apply the reviewed changes. No commit, push or deployment happens automatically.

## Architecture

```text
Coordinator / Codex skill / CLI / Web task form
                    |
        Durable queue + quota-aware routing
                    |
             OpenCode server
                    |
       Configurable provider/model profiles

Browser -> password-protected console + authenticated gateway
           tasks | sessions | profiles | account usage
```

The console requires username/password sign-in and validates allowed Host/Origin values. The public default is `admin` / `admin` on first initialization; replace it before enabling network access. Optional LAN listening and HTTPS reverse-proxy access protect the dashboard and native session gateway with the same login. OpenCode itself remains on loopback; its authentication is supplied only by the backend. Native gateway bodies, process logs and arbitrary tool output are not universally redacted. See [accounts and remote access](references/remote-access.md) for password setup, session behavior and proxy configuration.

## Operational boundaries

- Auto Approve defaults to on for the dedicated Worker service, without changing your interactive OpenCode configuration. Disable it in **模型与调度 → Worker 执行权限** for scoped native edits and exact shell allowlists. Settings apply when execution is idle; owned idle sessions are migrated to the selected policy on server startup.
- Scope and resource locks are cooperative controls, **not an OS sandbox**. In Auto Approve mode, read-only and writable scopes are instructions checked after execution, not tool permission boundaries. Workers run with your filesystem privileges; use trusted repositories.
- A worker's completion report still needs coordinator review. Evidence can be incomplete or mistaken.
- Manual continuations in native OpenCode do not create new queue entries or acquire queue write locks.
- Account usage is shared and sampled. Balance changes are not exact per-task charges. No automatic recharge is performed.
- Session management shows up to the latest 500 sessions, including archived sessions. Archived sessions are recoverable. Permanent deletion requires the exact title in the browser, or explicit `session delete ID --yes` in the agent CLI; both refuse active sessions or active descendants. Worker evidence remains in the local task ledger.
- Startup and process recovery do not promise unattended operation after logout, sleep or machine shutdown.

See [operations](references/operations.md), [security](SECURITY.md), and [contributing](CONTRIBUTING.md).

## Development

```sh
python3 -m unittest discover -s tests -v
node --check web/i18n.js
node --check web/app.js
node --check web/manage.js
node --check web/login.js
```

Console strings live in `web/i18n.js`; interface text is tagged with `data-i18n` attributes or looked up through the `tr()` helper. Adding a locale means adding one entry to the `MESSAGES` table.

Unit tests use temporary files and mocked provider APIs; they need no credentials and make no paid model requests. CI runs the tests on macOS and Linux. Local live verification uses isolated fixtures; do not point verification at a production repository.

OpenCode API references: [server](https://opencode.ai/docs/server/) and [configuration](https://opencode.ai/docs/config/). DeepSeek and Kimi quota adapters query the official provider endpoints; their responses may change independently of OpenCode.

MIT licensed. Independent community project; not affiliated with OpenCode, OpenAI, DeepSeek or Moonshot.

### Guidance, workspaces and cleanup

```sh
delegate-opencode steer JOB_ID 'Also check the empty case.' --request-id stable-guidance-id
delegate-opencode session bind SESSION_ID --directory /absolute/workspace
delegate-opencode session delete SESSION_ID --yes   # only when the user requested deletion
delegate-opencode cleanup                          # preview
delegate-opencode cleanup --apply                  # enabled low-space policy
```

Guidance reaches a later model-step boundary in the current session without changing its
permissions. Acceptance and model compliance are distinct. Reusing a guidance request ID
returns its previous status; unknown delivery is never automatically resent.

Workspace migration changes the native session binding and does not move source files.
Automatic cleanup is disabled by default for public installations. Enable it in the console
with an age floor, a recent-task retention count and free-space thresholds. It only touches
owned runtime data and eligible pool-linked archived sessions; worktrees and final evidence
are retained. The coordinator can also apply a user-requested cleanup with `--apply --force`.

Existing sessions can switch between worktrees of the same Git project. OpenCode 1.18.30 rejects cross-project migration; create a new session bound to the target project instead.

For a compatible code-only update while sessions are running, use `python3 scripts/install.py --live`. It restarts the queue observer and console but retains the OpenCode process and sessions. Provider/agent configuration changes still require an idle runtime.


### Inspect what a worker actually did

```sh
delegate-opencode transcript JOB_ID                       # latest 20 messages
delegate-opencode transcript SESSION_ID --limit 5          # any native session
delegate-opencode transcript JOB_ID --before CURSOR   # next_before from prior page
delegate-opencode transcript JOB_ID --full --output /private/path/task.transcript.json
delegate-opencode transcript JOB_ID --saved --full         # retained execution snapshot
```

Live reads query OpenCode without sending a prompt and include later manual continuations.
They are a point-in-time view, including when the session is running. Default reads paginate
and mark fields longer than 6,000 characters as truncated. `--full` requests all available
messages and preserves complete message parts, tool inputs and outputs, except credential
redaction. Use `--output` to avoid filling the coordinator's context: it creates a new JSON
file with owner-only permissions and refuses to overwrite an existing file or symlink.
The parent directory must already exist. Do not commit conversation exports.

`--saved` works offline for a worker task using its retained `messages.json`; it may be older
than the live session and is unavailable if that optional raw artifact was cleaned up.
Native deletion and connection failure never silently substitute a saved snapshot.
`collect --full` still means the full *result report*; use `transcript --full` for messages.

### Credential references

Keep three credential paths separate: **provider auth is owned by OpenCode**; the
**bridge server password is local and generated**; **deployment/app credentials** should
use an existing authenticated login or tool. Only when no such tool exists, add a local
reference. The model never needs the raw value.

```sh
# The owner-only file already exists, provisioned by your deployment tooling outside Git.
delegate-opencode credential register deploy --file /private/example/deploy-token
delegate-opencode credential list
delegate-opencode credential run --use DEPLOY_TOKEN=deploy --timeout 60 -- \
  /usr/local/bin/my-deploy-tool --environment staging

# Or an existing environment variable name already exported by your login tooling:
delegate-opencode credential register deploy-env --env EXAMPLE_DEPLOY_TOKEN
delegate-opencode credential remove deploy
```

Values are injected only through the child's environment, never into argv; supplied
arguments already containing a value or a recognized encoding are rejected. Captured
stdout/stderr is redacted before it reaches the model, and timeout/output-limit
termination kills the child process group and suppresses the buffers. This reduces
accidental exposure for normal authorized workflows; it is not an OS sandbox and does
not make arbitrary text secrets detectable. Never put secret values in chat, prompts,
steer text, command substitution or command flags: bridge redaction is late and cannot
erase what native OpenCode history or a model already saw. Removing a reference or
rotating a value does not rewrite historic artifacts. Provider keys and the server
password are deliberately transported to OpenCode, not kept out of native gateway or
process-log content. See [references/credentials.md](references/credentials.md).
