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
| Use LAN access or a reverse proxy | [Accounts and remote access](references/remote-access.md) |

### Guided installation

Requires **Python 3.9+, Git, and macOS or Linux**. Native Windows is unsupported. Existing OpenCode is reused; otherwise the installer provisions the pinned official release (integration-tested: **1.18.30**). No Node build, database service or Python packages are needed for the bridge runtime.

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

That preset uses Ark Auto for fast work; Kimi K2.8 and Ark Seed Evolving at 1:1 for
ordinary work; native Kimi K3 and Ark K3 at 2:1 for deep work; then Ark Auto and direct
DeepSeek as ordered fallbacks. Live quota runway can move either paired ratio by one bounded
step without rewriting the saved baseline.

These options initialize a **fresh** installation, not replace existing profiles. `--no-start` installs without launching services. [Full installation guide](references/installation.md): provider login, managed binary path, passwords, verification, updates and recovery.

## Agents choose tiers; the bridge chooses models

| Internal profile | Optional preset model | Bridge role |
| --- | --- | --- |
| `senior-code` | Kimi K2.8 Preview | Normal-tier subscription pool |
| `deep-research` | Kimi K3 | Deep-tier subscription pool |
| `fallback` | DeepSeek V4.1 Flash | Final fallback after preferred stages are unavailable |

Codex chooses Fast, Normal or Deep and submits with `profile=auto`; the bridge owns the profile, provider, allowance balance and fallback. The profile table above is an operator configuration view, not an agent role list. The preset uses `max` reasoning; other models need a variant their provider actually supports. Only a user-required named model or controlled comparison should pin a profile. [Routing and plan efficiency](references/routing.md).

Fast can complete bounded, well-specified features, known fixes, tests, documentation and routine
deployment; it is not a mechanical-work bucket. Normal is for a concrete outcome whose execution
still needs open investigation or synthesis. Deep is for abstract or unclear goals,
unresolved system-wide causes, architecture trade-offs and unusually complex
logic. File count, runtime, context size and importance do not make a task Deep, and genuinely
Deep work has no separate task-count limit.

## Product capabilities

### Delegate complete outcomes

Workers can own investigation, implementation, test repair, writing, SSH operations and routine deployment instead of receiving tiny code fragments. Each task carries an objective, acceptance criteria, workspace, and file or remote-resource scope. The default is four concurrent workers per owning Codex conversation; separate conversations have independent capacity.

### Route by capability

The coordinator classifies uncertainty only: **Fast** for complete work with a clear direction and acceptance boundary, **Normal** for a concrete goal that still needs open investigation, and **Deep** for abstract goals, system-wide causes and difficult trade-offs. The bridge then selects a provider through a single model, ordered fallback chain, fixed pool or quota-adaptive pool. One provider is enough; multiple plans are an enhancement.

Capacity protection uses two continuous curves. Level 1 gradually gives a later pay-as-you-go model some Fast/Normal work as subscription runway falls. Level 2 ramps faster and temporarily serves automatic Normal work from the Fast source pool. Deep and explicitly pinned models do not change. During DeepSeek off-peak hours, Level 2 can reach 50% when balance remains above the configured floor; peak hours or a low balance use the Level 1 ceiling. Weekends are always off-peak, while weekday public holidays come from a configurable, locally cached calendar subscription. See [routing and plan efficiency](references/routing.md).

### Keep long jobs running

There is no artificial model-step, tool-call or total-runtime cap. `wait` blocks and returns immediately when a result appears. Guidance and model changes can continue in the same OpenCode session at a safe turn boundary, preserving context and partial work. Confirmed quota exhaustion, usage-window limits and model-origin 429s continue through the next configured route without replaying the original task.

### Operate from one console

- Expand a task in place to inspect reverse-chronological activity, full redacted messages, input/output/reasoning/cache Tokens, and steer a running model.
- Search, create, rename, fork, archive, bind, delete and open native OpenCode sessions.
- Statistics reuse one usage snapshot every three seconds for the last hour, 24 hours and 30 days. Charts carry real Token scales plus model, tier, status and profile breakdowns.
- Task scheduling reports CPU, memory, disk, load and worker counts. Cleaned-task Token, cost and route summaries remain in an expandable **History ledger**.

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

Archiving a coordinator conversation does not remove the installation. Keep source in a stable checkout for updates; runtime is copied out. Alternate paths: `DELEGATE_INSTALL`, `DELEGATE_CONFIG`, `DELEGATE_STATE`, `DELEGATE_BIN_DIR`.

## Operational boundaries

Auto Approve defaults to on for the dedicated Worker service. Workers have the local user's privileges; scopes and resource locks are cooperative controls, **not an OS sandbox**. Restricted execution is selectable in the console.

Provider login, console login and the internal server password are separate. Keep real secrets out of prompts, guidance and Git. Native gateway bodies and arbitrary process/tool output are not universally redacted.

Services run in the background and start on demand after reboot; there is no login daemon and no promise of execution through logout/sleep. Model changes require idle workers and native sessions. Compatible code-only live updates retain OpenCode; see the installation guide.

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
