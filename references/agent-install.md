# Agent installation runbook

[简体中文](agent-install.zh-CN.md) · [Human installation guide](installation.md)

Audience: Codex, Claude Code or another agent asked to install/update Worker Desk. Complete the authorized installation and verify it. Use deterministic CLI flags; the interactive wizard is for a human terminal. This document is not a request to install until the user asks.

## Outcome and inputs

Deliver a durable user-level installation, preserved existing state, usable local console, configured model profiles and a truthful readiness report. Required choices are a stable checkout path and, on a fresh installation, an authorized provider/model or explicit preset. Reuse known choices from the conversation and existing configuration. Ask only if a genuinely missing model/account choice prevents progress.

Do not request or read raw API keys, passwords, SSH private keys or browser cookies. Reuse OpenCode's authentication and existing authenticated tooling. Login, QR, credential entry and new persistent authorization are user steps.

## Inspect before changing

1. Check the OS, Python 3.9+ and Git. Native Windows is unsupported; don't claim validated Windows integration.
2. Locate the existing wrapper and any explicit `DELEGATE_*` overrides. Default runtime is `~/.codex/skills/delegate-opencode`, state `~/.local/state/delegate-opencode`, config `~/.config/opencode/delegate-pool.json`.
3. If installed, inspect task state and native sessions before a disruptive update. Filter status locally to IDs/status/ownership; do not dump every transcript into context. Read only necessary non-secret configuration fields; never print entire credential/config files.
4. Reuse a clean stable checkout, normally `~/Developer/opencode-worker-console`. Check `git status --short` and the remote. Preserve unrelated changes; no reset/clean/stash without a reason and applicable authority.

If installing fresh:

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
```

If the path already exists, inspect/reuse it instead of executing the clone over it.

## Provision noninteractively

Use the user's actual model identifier, **not the literal placeholder**:

```sh
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL
```

Use `--preset deepseek-kimi` or `--preset ark-agent-plan` only when the user chose that bundle or
the existing task context establishes it. The Ark preset adds the subscription-first weighted
policy; it requires native Kimi, Ark Agent Plan and direct DeepSeek access. A preset is not an
account. Append `--no-start` if the user asked to provision without starting.

Do not infer that multiple providers are required. `--model` is the normal complete installation
for a single plan. The web console can later add an ordered backup, a fixed weighted pool or an
explicit quota-adaptive pair without reinstalling.

OpenCode is reused when available or installed at the pinned official version. The installer creates independent runtime/state directories and prints actual paths/URLs. There is no fixed port to hard-code. Explicit overrides: `DELEGATE_INSTALL`, `DELEGATE_STATE`, `DELEGATE_CONFIG`, `DELEGATE_BIN_DIR`; use the resulting wrapper consistently.

An existing installation is updated with `python3 scripts/install.py`, preserving settings/account/evidence. Model/preset flags do not overwrite existing profiles. Do not invoke `--wizard` under a noninteractive tool or attempt to feed fabricated answers into a TTY.

## Provider and console readiness

- Open the installed console with its wrapper: `~/.local/bin/delegate-opencode console --open`.
- A fresh local console has public defaults `admin / admin`; an existing account must not be reset. Leave credentials out of chat/logs.
- If provider authentication is missing, finish independent installation work and direct the user to OpenCode's provider login. The installed binary path is the `opencode_binary` config field; it may not be on PATH.
- The user can run `opencode auth login` (or the configured binary) in their own terminal. Do not automate new login consent or put a key in a command.
- The web **Setup wizard** uses actual connected-provider metadata and supported variants. It preserves drafts until explicit apply. Default Auto Approve applies only to the dedicated Worker service; do not reinterpret it as unlimited task authority.
- Use the existing model-settings API/console only when idle. Do not manually rewrite a live config to evade ownership, revisions or active-session checks.
- Do not widen network access as part of ordinary local installation. If the user requests LAN/proxy deployment, complete that authorized work using [remote-access.md](remote-access.md), with their own account and exact origins.

For custom overrides, direct maintenance commands need the same environment. The generated wrapper already retains explicitly provided overrides.

## Verify in layers

```sh
~/.local/bin/delegate-opencode service start
~/.local/bin/delegate-opencode doctor
~/.local/bin/delegate-opencode quota --refresh
```

Inspect returned data: a CLI exit code of zero is not sufficient. `doctor` may contain `server_error`; check server health, registered profiles and daemon heartbeat. A provider without a quota adapter may still work. Quota samples and model catalog entries do not prove an inference request will succeed.

If end-to-end model verification is within the user's request and account usage is authorized, submit one small read-only task in an explicitly safe directory with `--tier fast --profile auto`; the bridge selects the profile. Collect the result and check the actual model/error evidence. Wait to completion with bounded `wait` calls. Do not cancel because a job takes several minutes or end by asking the user to say “continue.” Do not spawn an expensive full-repository investigation as an installation smoke test.

No real request is required merely to render the wizard or run fixture tests. Report a skipped model check as unverified, not successful. If login is the only remaining blocker, state it precisely and retain completed installation.

## Safe update and recovery

- Use `git pull --ff-only` only after checking the checkout and preserving local work.
- A normal install/update waits for idle work. Do not cancel another conversation's tasks.
- `python3 scripts/install.py --live` is for compatible code-only updates. It retains OpenCode while restarting observer/console; it cannot change model/binary configuration. Never assume every migration is live-compatible.
- An uncertain dispatch must be inspected through the existing task/session before retrying. Never blindly replay side effects.
- Confirmed provider quota/window errors and model-origin 429s continue in the same OpenCode session on the next route by default. Inspect `route_history` and partial work. If automatic rerouting is disabled or no route remains, continue with the same tier and `profile=auto`; endpoint-confirmed zero remains unavailable and Kimi hidden monthly errors retain the separate explicit-retry path.
- Do not delete task ledgers, auth state, worktrees or artifacts to “start clean.” Existing runtime backups live under private state `releases/`; restoration should preserve current config/state and respect active tasks.

## Coordinator handoff

For Codex, the default install includes `delegate-opencode/SKILL.md` in the user skill directory. Skill discovery depends on the caller; don't claim an already-running conversation hot-reloaded it. The CLI can be invoked immediately by absolute path.

For Claude Code or another coordinator, provide the installed [SKILL.md](../SKILL.md) as delegation guidance and use the same CLI/task JSON contract. There is no automatic installation into every agent's skill directory, and no automatic sharing of Codex-only tools.

When the caller has no Codex conversation identity, supply a stable `--group-id`
for that conversation and a readable `--group-title` on submissions. Keep the ID
for continuations; it is the fallback scheduling owner, not a way to bypass
concurrency or resource coordination.

Link detailed operations instead of pasting full transcripts or the entire manual into global instructions. Give complete outcomes to workers; profile names are not capability allowlists.

## Report completion

Return only necessary non-secret facts:

- Source and installed runtime/CLI paths.
- Local console URL; whether it was actually opened.
- Task tier submitted with automatic routing, or existing operator profiles preserved.
- Installation, service health, provider connection and real model execution as separate statuses.
- Any pending user login or model choice, with the exact next step.
- For updates, whether OpenCode was retained and whether running work was affected.

Do not include balances, credential values, transcript dumps or claims of measured cost/token savings in an installation report.
