# Worker Desk

**Let Codex or Claude Code make the calls while OpenCode workers keep executing.**

[简体中文](README.zh-CN.md) · [Installation](references/installation.md) · [Install with an AI agent](references/agent-install.md)

## Easy installation: send one message

**Installation is simple: copy the message below into Codex or Claude Code and let your agent handle the setup.**

```text
Install and set up Worker Desk for me:
https://github.com/JiangNanGenius/opencode-worker-console
Read references/agent-install.md in the repository and follow it to complete installation, start the services, open the web console, and verify the result.
Reuse my existing OpenCode configuration and install in a permanent user directory outside temporary conversation folders.
Ask me only for a missing provider/model choice or a login I need to complete.
```

The agent handles installation, startup and health checks. You only step in for a provider login or a model choice.

Worker Desk is a local orchestration bridge. It turns a coordinator's judgment and OpenCode's execution into an operable worker pool: tasks have owners, workers have workspaces, models have routes, long jobs keep waiting, and results or errors return to the coordinator.

## Why it exists

| Real constraint | Worker Desk response |
| --- | --- |
| Premium context is expensive and large repositories consume it quickly | Give complete investigation, implementation and test work to long-context workers; return compact evidence to the coordinator |
| Several plans and models are useful, but manual selection drifts | The coordinator chooses Fast / Normal / Deep; the bridge chooses the actual model from capability, runway and price window |
| Jobs can take tens of minutes and still need guidance or a model change | Durable queues, event-driven waits, same-session steering and boundary-safe rerouting keep work moving |
| OpenCode sessions, workspaces, Tokens and retained evidence are fragmented | One authenticated console manages tasks, sessions, models, quota, statistics and cleanup |

Execution models need to be capable, not identical to the frontier coordinator. Repository research, implementation, test repair, writing, SSH operations and routine deployment can all be delegated as complete outcomes. This often reduces premium-model allowance pressure and total monetary cost; **total Tokens and savings still depend on the workload, models and plans**.

## Architecture

```text
You
 └─ Coordinator: Codex / Claude Code / another CLI-capable agent
     └─ Worker Desk bridge
         ├─ Task ownership, guidance, results and error recovery
         ├─ Web console: sessions, workspaces, models and usage
         └─ OpenCode execution
             └─ Your configured provider/model profiles
```

The included **Codex skill** supplies delegation guidance. Claude Code and other coordinators can use the same CLI and JSON interface; their tools, permissions and skill-discovery mechanisms remain separate. Worker Desk does not transfer a coordinator's desktop controls or connectors into OpenCode.

## Start here

| I want to… | Start with |
| --- | --- |
| Install it myself | [English installation guide](references/installation.md) / [中文安装指引](references/installation.zh-CN.md) |
| Ask an AI agent to install it | [Agent installation runbook](references/agent-install.md) / [模型安装指引](references/agent-install.zh-CN.md) |
| Configure models after installation | Open **Setup wizard** in the console |
| Delegate and inspect work | [Skill](SKILL.md) and [operations](references/operations.md) |
| Use the voice reminder for alerts | [Voice reminder settings](references/qwen-voice-settings.md) |
| Show host, task and plan status on an ESP32 display | [Mac status board sources](examples/mac-status-board/README.md) |
| Use LAN access or a reverse proxy | [Accounts and remote access](references/remote-access.md) |

### Guided installation

Requires **Python 3.9+, Git, and macOS or Linux**. Native Windows is unsupported. Existing OpenCode is reused; otherwise the installer provisions the pinned official release (integration-tested: **1.18.32**). No Node build, database service or Python packages are needed for the bridge runtime.

Run in your own terminal:

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
python3 scripts/install.py --wizard --lang en
~/.local/bin/delegate-opencode console --open
```

The wizard reviews choices before installation. Existing installations keep their profiles, credentials and task evidence. Chinese prompts: `--lang zh-CN`.

A fresh console starts with **admin / admin**; change it before network access. Connect your provider in OpenCode, then use **Setup wizard** to review models, routing and worker preferences. The bridge setup form does not collect provider API keys.

One provider is enough. The console can later build a single-model route, an ordered primary/backup
chain, a fixed weighted pool, or an explicitly quota-adaptive two-plan pool. The Ark/Kimi setup in
this repository is an optional preset rather than a requirement.

### Noninteractive installation

For an already chosen OpenCode `provider/model` identifier:

```sh
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL
```

Or opt into the DeepSeek/Kimi bundle:

```sh
python3 scripts/install.py --preset deepseek-kimi
```

Or install the subscription-first Ark/Kimi/DeepSeek ladder:

```sh
python3 scripts/install.py --preset ark-agent-plan
```

Or install the optional three-plan stack (Kimi + Ark Agent Plan + Alibaba Token Plan + direct
DeepSeek). It declares only the fixed `qwen3.8-max` and `qwen3.8-flash` models, so the opaque
Alibaba Auto alias can never appear in automatic routing:

```sh
python3 scripts/install.py --preset three-plan
```

The four-tier setup uses Fast = DeepSeek Flash, Normal = K2.8 Preview / Qwen3.8 Flash,
Deep = Qwen3.8 Max, and Ultra = native Kimi K3.1 Pro with maximum supported reasoning.
Old native/Ark K3 routes are retired; historical records stay accurate. Confirmed Ultra capacity
exhaustion defaults to a checkpointed Qwen3.8 Max fallback; explicit pins and Ultra floors still wait. Kimi's base / maximum weekly protection for K3.1 Pro defaults to 30%. Dynamic release is on
by default: the idle final half of the real weekly cycle lowers protection toward min(base, 5)%,
opening that allowance to Normal. Ultra demand or unknown signals keep the base. The five-hour
window and account total remain separate limits. Missing account total is unknown,
never four times a weekly allowance. See [routing](references/routing.md).

For single-key Alibaba access, keep its
provider API key in an owner-only file; set `provider_api_key_files` in the private pool config to
`{"bailian-token-plan": "/private/path/key"}` and the runtime references it without copying the
secret. The Credits pool refreshes itself from the official read-only account query
(Model Studio OpenAPI `GetSubscriptionStats`, ACS3-signed): register the Alibaba account
AK/SK pair — separate from the inference key and never from configuration — as owner-only
file references `aliyun-control-ak` and `aliyun-control-sk`, then `delegate-opencode quota
--refresh` or the console refresh button fetches the actual remaining Credits with an
explicit `openapi_live` source. A failed live query never fabricates a refresh: the row
keeps its last genuine reading (or, before the first one, an imported console snapshot)
with a sanitized `live_error`. Snapshot imports remain available as that baseline:

```sh
delegate-opencode quota --import-snapshot /private/path/snapshot.json
```

Multiple independent Alibaba seats with the same expiry are counted as one combined Credits
pool, with optional per-seat detail. An optional authenticated loopback proxy consumes keys
sequentially and switches after a confirmed key-level rejection; existing single-key installs
keep working. Personal Edition usage bundles (add-ons) are reported separately from
the monthly percentage: observed remaining/total/used Credits, active pack count and the
server's nearest pack expiry stay visible while the monthly allowance is positive, a
known-exhausted bundle disappears from the current card without erasing observed history,
and a failed query stays unknown rather than zero. See [combined Credits](references/bailian-seat-ledger.md) and
[multi-key setup and idle-safe activation](references/bailian-pool.md).

Snapshots are shown with explicit freshness, never as live readings; an expired-period or
already-stale positive reading can never clear a recorded depletion or billing circuit.
For timestamped per-response token and estimated-Credit windows (1 h / 24 h / 48 h):

```sh
delegate-opencode usage-report --windows 1,24,48
```

Native OpenCode updates default to `enabled` with a 24-hour check interval. The bridge stages the
official stable binary while tasks run and performs a short serialized restart only when no
task is executing; a queued-only handoff reserves the admission barrier instead of cancelling
anything, and a failed health/version check restores the previous binary and configuration.

These options initialize a **fresh** installation, not replace existing profiles. `--no-start` installs without launching services. [Full installation guide](references/installation.md): provider login, managed binary path, passwords, verification, updates and recovery.

## Agents choose tiers; the bridge chooses models

| Internal profile | Optional preset model | Bridge role |
| --- | --- | --- |
| `senior-code` | Kimi K2.8 Preview | Normal-tier subscription pool |
| `aliyun-max` | Qwen3.8 Max | Deep reasoning |
| `ultra-reasoning` | Kimi K3.1 Pro | Ultra reasoning; confirmed capacity exhaustion can continue on Qwen3.8 Max |
| `fallback` | DeepSeek V4.1 Flash | Final fallback after preferred stages are unavailable |

The upstream harness chooses Fast, Normal, Deep or Ultra only from task uncertainty and submits with `profile=auto`; the bridge privately owns the profile, provider, allowance balance, conservation and fallback. Ordinary task results do not disclose automatic model or route changes, while the operator console retains the full audit trail. The profile table above is an operator configuration view, not an agent role list. The preset uses `max` reasoning; other models need a variant their provider actually supports. Only a user-required named model or controlled comparison should pin a profile. [Routing and plan efficiency](references/routing.md).

Fast can complete bounded, well-specified features, known fixes, tests, documentation and routine
deployment; it is not a mechanical-work bucket. Normal is the default for concrete outcomes. Deep handles complex work needing strong reasoning.
Ultra handles especially difficult architecture, stubborn root causes and long-horizon reasoning.
After two rounds without material progress, the coordinator reviews a checkpoint before upgrading;
authentication, permissions and environment failures need direct repair. Once uncertainty is
resolved, give routine implementation to Normal. File count and importance alone do not choose a tier.

## Product capabilities

### Delegate complete outcomes

Workers can own investigation, implementation, test repair, writing, SSH operations and routine deployment instead of receiving tiny code fragments. Each task carries an objective, acceptance criteria, workspace, and file or remote-resource scope. The default is four concurrent workers per owning upstream-harness conversation; separate conversations have independent capacity.

Write tasks use the bound shared/main checkout by default, including large and long-running work.
An isolated worktree is reserved for an explicit isolation request, unavoidable concurrent
conflicting writes, or a destructive experiment. This avoids duplicate dependencies and build
products; managed worktrees are released after safe integration while unintegrated changes remain
visible for review.

### Route by capability

The coordinator chooses Fast, Normal, Deep or Ultra from uncertainty and the concrete benefit
of model latency. The bridge chooses the model; Ultra prefers native K3.1 Pro and defaults to
Qwen3.8 Max after confirmed capacity exhaustion. Both unavailable means waiting; explicit pins
and `capability_floor=ultra` retain their required model. [The tier and quota contract](references/routing.md) describes checkpoint
handoffs and dynamic weekly protection. Released percentage points are not available quota; spent quota cannot be reclaimed.

Capacity protection combines a global curve with a curve for each provider pool. A constrained provider can leave a same-capability pair entirely while its healthy peer takes 100%; the global pool can still remain healthy. Level 1 gradually gives a later pay-as-you-go model some Fast/Normal work as subscription runway falls. Fast follows the runway of its own configured source, so a freshly restored Normal/Deep subscription cannot hide a nearly exhausted Fast plan. Level 2 ramps faster and can move a provider-only Fast route fully to DeepSeek while the configured balance floor is intact. Deep and explicitly pinned models never fall to a lower-capability stage. Off-peak reaches the high-pressure ceiling earlier; peak traffic rises from the Level 1 ceiling only as subscription runway becomes severe. Weekends are always off-peak, while weekday public holidays come from a configurable, locally cached calendar subscription. See [routing and plan efficiency](references/routing.md).

### Keep long jobs running

There is no artificial model-step, tool-call or total-runtime cap. `wait` blocks and returns immediately when a result appears. Guidance continues at a safe turn boundary. Entering Ultra starts a fresh native model session with the same bridge task, authority and checkpoint, without replaying completed work. Confirmed quota exhaustion, usage-window limits and model-origin 429s continue through the next configured route without replaying the original task.

### Operate from one console

- Expand a task in place to inspect reverse-chronological activity, full redacted messages, input/output/reasoning/cache Tokens, and steer a running model.
- Search, create, rename, fork, archive, bind, delete and open native OpenCode sessions.
- Search, add, edit, pin and delete durable project memory through the bridge or console. The memory backend is either the legacy `opencode-mem` service or a loopback-only OpenViking server (published wheel, Ark embeddings + DeepSeek Flash extraction) whose data, vectors and migration journal stay on the internal disk. Capacity-based rolling retention removes the oldest unpinned records only after the configured ceiling is crossed, keeps pins (journal and native `pinned=true` tags), and runs at the next service start even after the machine was off; deleting sessions does not age memory out. The OpenViking service binds loopback only, requires an owner-only API key for data access, and keeps every model credential an environment reference. The console memory page browses the real project/category/folder tree with bounded previews and pagination, inspects one record at a time with truthfully-reported L0/L1/L2 levels, runs semantic search, and shows a non-mutating capacity preview plus a confirmed maintenance run. See [OpenViking memory](references/openviking-memory.md).
- Long builds, deployments and GitHub Actions runs can publish truthful phase, percentage and ETA data; jobs without measurable totals remain visibly indeterminate.
- Statistics reuse one usage snapshot every three seconds for the last hour, 24 hours and 30 days. Charts carry real Token scales plus model, tier, status and profile breakdowns.
- Task scheduling reports CPU, memory, disk, load and worker counts. Cleaned-task Token, cost and route summaries remain in an expandable **History ledger**.
- **Models & routing** includes a separate **Voice reminders** card for the installed `qwen-voice-notify` skill. It defaults to cloud speech through the existing Volcengine Ark Agent Plan `seed-tts-2.0` model, automatically falls back to the installed offline Qwen3-TTS voice when cloud speech fails before playback, and never falls back after audio has started. Cloud and local fallback voices are selected separately, the shared rate defaults to 1.10× (0.50–2.00×), and settings persist atomically for both the console and `notify.py`; missing new fields keep their defaults so an older save cannot reset them. **试听** is explicit and fixed-text, stop only owns the console preview, and a GET never synthesizes or calls the network. There is no autoplay, volume or output-device change, model download or resident model server. See [Voice reminder settings](references/qwen-voice-settings.md).

### Host overview and structured controls

Public deployments support **Passkey-only sign-in** while a separate LAN address
retains password login. The same gateway protects the console and native OpenCode,
including live streams and WebSockets. Multi-device enrollment, revocation and
Mac-local recovery are available under **Passkey / Secure sign-in**; see the
[public access guide](references/remote-access.md#public-access-with-passkeys).

Codex tasks expand into their subagents and exactly associated OpenCode workers
on the host overview and task dashboard. This local, read-only view shows the
current model, reasoning effort and source freshness without inventing progress.

Signing in lands on the **Host overview**: live CPU, GPU, memory, kernel memory-pressure, die temperatures, fan RPM, network rate, disk I/O and internal/external disk capacity charts (15 minutes, 1 hour, 24 hours), the active Codex and OpenCode tasks with their real model and elapsed time, provider quotas, and managed-service status. One shared collector serves the web and the physical information board; the board payload is published as an owner-only `STATE/host-board.json` in the existing v2 schema. Missing sensors render as unavailable, never as zero.

Progressive drawers offer fixed, validated controls — no terminal, no file browser: fan profiles and fixed RPM through the local fan daemon (its safety floor is never touched), supported macOS power modes with readback, a managed keep-awake (`caffeinate`), ending own-user processes with TERM then explicit KILL (PID + start time validated), restarting managed services, and sleep/restart/shutdown with affected-work confirmation, recent-password reauthentication and persisted operation status. Disruptive actions run through a narrow root helper that must be installed and verified first; until then they report unavailable. See [Host controls](references/host-controls.md) for installation, capabilities, configuration paths and rollback.

### Clean safely and retain evidence

Before deleting terminal work, Worker Desk writes a compact usage ledger, then removes the linked OpenCode session and disposable evidence. Integrated or unchanged managed worktrees are released; unintegrated changes stay for review. An idle `needs_attention` task becomes reclaimable after 24 hours or when a newer task exists under the same coordinator conversation.

### Keep secrets local and alerts quiet

OpenCode owns model authentication. Ark quota telemetry and Bark store only environment-variable names or owner-only file references; APIs and pages never return secret values. Bark defaults to conservation, exhaustion and recovery milestones. A coordinator may opt a key task into one completion alert. [Credential handling](references/credentials.md) · [Remote access](references/remote-access.md)

The console supports English, Simplified and Traditional Chinese, Japanese and Korean. The dedicated Worker service defaults to Auto Approve and runs with the current user's privileges; scopes and resource locks coordinate work but are not an OS sandbox.

## First task

This sends a real model request and can consume provider allowance. Use an existing directory the worker is authorized to read:

```sh
~/.local/bin/delegate-opencode submit --directory /absolute/project \
  --profile auto --tier normal --group-title 'Repository orientation' \
  --title 'Map the project' \
  --acceptance 'Return entry points, test commands and evidence; do not modify files.' \
  'Read the project guidance and summarize how this project is organized.'
~/.local/bin/delegate-opencode wait JOB_ID
~/.local/bin/delegate-opencode collect JOB_ID
```

Replace `JOB_ID` with the returned ID. The default wait window follows the task tier (Fast 5 minutes, Normal 30 minutes, Deep 60 minutes) and returns as soon as a result is available. Continue the same command process when the runner yields a session ID, using the longest supported blocking poll without separate status checks; call `wait` again only if the full window returns `continue_waiting: true`. An observation window is not a task deadline. Review evidence. Writes require `--mode write` and local `--scope`, operational `--target`, or both. [More examples](references/operations.md).

For a key milestone, add `--notify-on-complete`. The coordinator chooses this per task; the bridge
sends one Bark alert only after that task completes successfully. Routine tasks remain silent.

## Installed independently of your conversation

| Item | Default location |
| --- | --- |
| CLI | `~/.local/bin/delegate-opencode` |
| Runtime and Codex skill | `~/.codex/skills/delegate-opencode` |
| Configuration | `~/.config/opencode/delegate-pool.json` |
| Private state, artifacts and worktrees | `~/.local/state/delegate-opencode` |
| Project memory, legacy store (configurable) | `~/.local/state/delegate-opencode/opencode-mem` |
| Project memory, OpenViking data and journal | `~/.local/state/delegate-opencode/openviking` |
| OpenViking model config (owner-only, env refs) | `~/.openviking/ov.conf` |

Archiving a coordinator conversation does not remove the installation. Keep source in a stable checkout for updates; runtime is copied out. Alternate paths: `DELEGATE_INSTALL`, `DELEGATE_CONFIG`, `DELEGATE_STATE`, `DELEGATE_BIN_DIR`.

## Operational boundaries

Auto Approve defaults to on for the dedicated Worker service. Workers have the local user's privileges; scopes and resource locks are cooperative controls, **not an OS sandbox**. Restricted execution is selectable in the console.

Provider login, console login and the internal server password are separate. Keep real secrets out of prompts, guidance and Git. Native gateway bodies and arbitrary process/tool output are not universally redacted.

Services run in the background and start on demand after reboot; there is no login daemon and no promise of execution through logout/sleep. Model changes require idle workers and native sessions. Worker Desk checks stable OpenCode releases on a configurable schedule, applies an update only while tasks and native sessions are idle, verifies the restarted version and restores the previous binary on failure. Compatible code-only live updates retain OpenCode; see the installation guide.

Kimi hidden monthly exhaustion differs from endpoint-confirmed zero allowance. Confirmed quota, window and model-origin 429 stops can continue on the next route in the same OpenCode session, preserving context and partial work without replaying the original task. [Recovery](references/operations.md#quota-and-routing).

## Development

```sh
python3 -m unittest discover -s tests -v
node --check web/i18n.js
node --check web/app.js
node --check web/manage.js
node --check web/login.js
node --check web/setup.js
```

Tests use temporary fixtures and mocked provider APIs, not paid requests. Node is only needed for frontend checks. CI covers macOS/Linux and Python 3.9/3.12. [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md).

MIT licensed. Independent community project; not affiliated with OpenCode, OpenAI, Anthropic, DeepSeek, Moonshot or Volcengine.
