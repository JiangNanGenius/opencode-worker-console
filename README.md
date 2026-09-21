# Worker Desk

**A local bridge for multi-model agent collaboration.**

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

The agent handles the installation steps and checks. If an account needs to be connected or a model chosen, it will guide you through that part.

Let a capable coordinator handle planning, judgment and acceptance while OpenCode workers own complete execution tasks. Worker Desk connects them through a durable task queue, explicit workspaces, model profiles and an authenticated web console.

The premise is that execution models are strong enough to deliver complete outcomes. Delegate repository investigation, implementation, test-and-fix, writing, SSH operations or deployment—not just tiny code fragments. Large-context workers can read substantial material and return concise findings with evidence. This can reduce reliance on premium-model capacity and overall monetary cost; **total tokens and savings depend on the workload, models and plans**.

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

## What you can manage

- **Tasks:** expand details directly under a task; inspect live activity and input/output/reasoning/cache tokens without leaving the console. The current filter shows recorded worker totals; these exclude coordinator usage and are not a cost estimate. Clearing a terminal task deletes its linked OpenCode conversation and disposable evidence after writing a compact token/cost/routing ledger. Integrated or unchanged isolated worktrees are released; unintegrated changes remain visible for review.
- **Sessions:** search, create, rename, fork, archive/restore, workspace binding, deletion and native OpenCode links. Forking alone sends no prompt.
- **Models, usage and economics:** visual profiles and route stages for one provider, primary/backup, fixed pools or opt-in quota adaptation; DeepSeek balance, Kimi plan windows and Ark AFP windows. Each provider can use live telemetry, a manual reset window, a monetary low-balance threshold, or opt out of dynamic guidance; the global reset-aware runway threshold is configurable. A configurable low-weekly guard reserves the last Kimi allowance only when it is also unlikely to last until reset, and confirmed quota/429 stops can continue in the same session on the next route. The console shows an aggregate work-pool card, visual remaining/expected pace, Beijing and local reset times, live dynamic shares and a configurable cost ruler (`1 AFP-equivalent = ¥0.002` by default); AFP-equivalent is not Kimi quota.
- **Parallel work:** four running workers per owning Codex conversation by default; independent capacity across conversations. Conflicting files/resources and provider availability still govern dispatch.
- **Workspaces:** disjoint shared scopes, remote operational targets or isolated Git worktrees; review patches before integration.
- **Long jobs:** no artificial model-step, tool-call or total-runtime cap. Observe and guide through completion; never blindly replay uncertain operations.
- **Credentials:** existing authenticated tools and optional metadata-only secret references. [Credential handling](references/credentials.md).
- **Console:** password login, optional LAN/proxy access, and English, Simplified/Traditional Chinese, Japanese and Korean UI.

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
