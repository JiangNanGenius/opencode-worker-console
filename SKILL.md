---
name: delegate-opencode
description: Delegate complete authorized outcomes to general-purpose OpenCode agents. Every task category is eligible. Classify Fast/Normal/Deep from the uncertainty the worker must resolve, keep profile=auto, and let fresh quota_posture break a genuine Fast/Normal tie. The bridge selects provider and model, including allowance-aware fallback.
---

# Delegate OpenCode work

OpenCode is a general-purpose execution layer for Codex. Every authorized outcome is eligible for
delegation by default; name, domain, importance and whether code changes never decide it. When the
worker has the needed tools, delegate the whole workflow — investigation, implementation, repair
and verification — instead of keeping an unlisted category or handing out low-level fragments.
Codex supplies context and authority, reviews evidence and owns final acceptance. Keep only a step
that truly requires Codex Computer Use, nuanced visual judgment, or a user-only login, MFA, legal
acceptance or approval; let the worker finish every other supported step and report the exact
remaining handoff with evidence. Workers can implement interface code and run terminal or headless
verification; do not duplicate their repository reading locally.

## Scope the authority

- Local `scopes` name repository files/directories that may change. Authorize a whole relevant
  subsystem (or an isolated repository) up front when investigation must discover touched files.
- Operational `targets` name authorized remote hosts, services, APIs, databases or remote data. A
  remote-only write needs a target, not a dummy local scope or repo; state environment and allowed
  effects in the objective. Targets convey authority, never credentials or access-control bypass.
- `mode: read` is inspection only, including remote systems. `mode: write` needs scopes, targets,
  or both. Shared workspace is the default; use `--large`/`--workspace isolated` for broad or
  uncertain cross-module edits. Give tasks acting on one deployment, service, database or device a
  single stable `--resource` lock name (for example `ssh:host:service`), not per-model locks.
- Auto Approve is on by default: ordinary tools and commands are allowed; supplied `commands` are
  suggested checks, not an exclusive allowlist, and no artificial iteration, tool-call or runtime
  limit applies. Task authority still applies.
- Existing user authorization carries into the handoff; ask only for a genuinely missing decision
  or access. New logins, QR scans and persistent consents remain user steps. `--group-title` names
  the main outcome, `--parent-task-id` continues one; preserve other tasks' ownership.
  `--notify-on-complete` sends one Bark alert for a key milestone only.

## Submit reliably

Use `~/.local/bin/delegate-opencode` (or this skill's `scripts/delegate.py`). `submit` starts
missing services and installs the pinned official OpenCode when needed; `service start` precedes
`doctor` when services are down; `console --open` opens the dashboard. Delegate one coherent
outcome early with objective, context, acceptance evidence and authorized target; let the worker
discover commands, conventions and runbooks. For installation/upgrades follow
[the agent install runbook](references/agent-install.md)
([中文](references/agent-install.zh-CN.md)), with noninteractive flags and separate verification of
installation, provider connection and real execution. A read example:

```sh
delegate-opencode submit --directory "$PWD" --tier normal --profile auto \
  --group-title 'Main outcome' --title 'Investigate and recommend a fix' \
  --acceptance 'Return the cause, evidence, affected paths and uncertainties.' \
  'Investigate the reported problem and recommend a complete fix without changing systems.'
```

For writes add `--mode write` with `--scope` and/or `--target`. Complex specs go in a private JSON
file: `submit --spec /absolute/private/task.json`. Never use `--spec -` unless the caller actually
writes JSON to process stdin — a command string alone does not supply an in-memory object.
Prefer a private spec file unless the payload is explicitly piped to stdin. For independent in-memory specs submit
concurrently with `--spec-urlencoded`, passing
`encodeURIComponent(JSON.stringify(spec)).replace(/'/g, "%27")` in single quotes. Inspect every
result and wait only on returned job IDs. Fields and examples are in [operations](references/operations.md).

## Tools, remote access and credentials

A worker may use SSH, scp/rsync, shell programs, Git, authenticated CLIs, HTTP APIs and whatever
native or MCP tools its runtime exposes. Reuse authorized SSH aliases, ssh-agent and existing
authenticated tooling; never put a private key or password in a task. A deployment covers the real
running version, service health and intended outcome, not just a successful command. Check concrete
availability rather than assuming a Codex-only connector exists; conversely, an existing CLI/API may
make a branded skill unnecessary. If one browser/desktop step is unavailable, complete the supported
work and return that exact step — never hand back the whole task. For an extra secret, register a
metadata-only reference and run the command through `credential run`
([credentials](references/credentials.md)); never place raw credentials in prompts, argv, task JSON,
reports or commits. LAN, provisioning and reverse-proxy setup is in
[remote access](references/remote-access.md).

## Choose one tier by uncertainty

For every handoff choose exactly one capability tier and keep `--profile auto`. The tier measures
ambiguity the worker must remove — not intelligence, importance, duration, files or context size —
and Fast and Normal both deliver complete outcomes.

| Task shape | Tier |
| --- | --- |
| Outcome, governing constraints and observable acceptance are known; the worker may discover local details and methods | `--tier fast` |
| Clear result, but execution needs open investigation, synthesis or several interdependent judgments | `--tier normal` |
| Worker must define the goal or rules, reconcile conflicting requirements, design system structure, find an unknown system-wide cause, or reason through unusually complex invariants | `--tier deep` |

Fast is the capable baseline, not a mechanical-work bucket: it can own a complete feature, known bug
fix, tests, docs, migration or routine deployment when the direction is firm. Use the three-question
gate — what changes, what constraints govern it, what evidence proves it done. If all three are
answerable, prior planning difficulty or a large, long-running implementation does not upgrade the
tier. Moving a listed set of documents into predetermined folders is Fast; judging mixed material
for retention is Normal; redesigning an information architecture with no rules can be Deep. A failed
or materially uncertain Normal investigation justifies a Deep continuation; never split a coherent
outcome to avoid Deep, and give one complete outcome instead of many tiny lookups. Do not restart
productive work only to change tier; apply the new tier to the next handoff.

Before the first handoff in a coherent batch, call `quota --tier-guidance --compact` once or reuse a
fresh result this turn; use verbose `quota` only when investigating provider state as an operator.
Use the `quota_posture` label only as a tie-breaker when Fast and Normal both fully fit:
`fast_preferred` leans Fast, `normal_flexible` (a healthy Normal-only plan) lets you choose by
error/rework risk, urgency and size, and `neutral` means task needs alone decide. Never interpret raw
percentages or downgrade work that needs the higher tier; quote `conservation_level` (`0`/`1`/`2`)
when explaining capacity. Policy stages, fallback, conservation, prices, ratios, curves and plan
presets are operator concerns documented authoritatively in [routing](references/routing.md) — do
not imitate provider decisions in prompts.

Optional: set `capability_floor` to `fast`/`normal`/`deep` only for a deliberate requirement that
conservation or fallback must never serve the task below that tier. It cannot exceed the requested
tier, continuations inherit it, and a nonempty `capability_reason` (1–500 characters) is required,
explaining why lower capability is unacceptable; ordinary tasks omit both fields. Only a direct
user requirement for a named model, or a controlled comparison, pins a profile with an explicit
profile plus concrete `profile_reason`. Keep the highest **verified supported** reasoning variant on
continuations (the built-in Kimi, DeepSeek and Ark profiles currently support `max`).

## Wait until it is terminal

Observe with `wait JOB_ID` (no `--seconds` for the tier window: Fast 5 min, Normal 30, Deep 60). It
is an event-driven blocking observation that returns immediately on completion, failure or actionable
attention — not a sleep, deadline or polling schedule. If the command runner yields a
process/session ID, keep following that same process with an empty-stdin blocking poll for the
longest window the host supports; do not interleave `status`, reconsider, or new short `wait` calls.
A nonterminal response with `continue_waiting: true` means call `wait` again **in the same turn**.
Keep the turn active until terminal or genuine coordinator input; never end with a progress message
asking the user to "continue".

Quiet output, unchanged status, no visible diff, a long investigation, high token use or the
coordinator's desire to take over are not stalls and never cancellation reasons; after an accepted
`steer`, let the guidance execute and keep waiting. `cancel JOB_ID --reason` is reserved for
`user_requested`, `superseded`, `wrong_scope`, `duplicate` or `side_effect_risk` — never relabel
impatience. Use `steer JOB_ID 'guidance' --request-id STABLE_ID` for batched corrections read at the
next model-step boundary; workers own their own test-and-fix and routine repair loops.

## Recover without duplicating work

A model-origin HTTP 429, explicit window exhaustion, HTTP 402/insufficient balance, or unequivocal
monthly-plan exhaustion is a confirmed capacity stop. With automatic rerouting, the bridge stops the
attempt, excludes the provider and sends an in-session continuation on the next route — same OpenCode
session and workspace — explicitly told to inspect prior work and never repeat completed edits,
deployments, messages, payments or other external side effects. Keep waiting on the same job;
`route_history` and `queued_boundary_switch` entries are progress, not cancellation signals. If
rerouting is disabled, no route remains, or acknowledgement is uncertain, inspect `recovery`, keep
the tier, and submit a deliberate `profile=auto` continuation with `--parent-task-id` after reading
existing state. Authentication, transport and ordinary model errors do not trigger rerouting; a
transport error after dispatch may still mean the prompt was accepted — observe the session, never
blindly resubmit, and never replay a prompt whose acceptance is uncertain. Tell the user once when
`fallback_used: true`; stay silent about ordinary model choices.

## Review evidence and manage sessions

Review the compact report, critical diff and evidence before accepting. A successful tool call, a
passing test, a deployment command and an observed production/device or rendered-UI result are
different facts; report CLI/API results, headless/UI checks and physical observations separately,
and request original detail only where needed. For isolated work review the patch, run `integrate
JOB_ID` then `integrate JOB_ID --apply`, and verify afterward; integration never commits or deploys.
An `uncertain` task keeps its ownership — recover and inspect it before any replacement.

## Notify the user at meaningful milestones

Use `--notify-on-complete` on a delegated task when its successful completion matters away from
the desk, such as a finished deployment or processed TestFlight build. Routine tasks omit it.
For a verified milestone outside one worker task, send a deliberate message with
`notify --title 'Title' --body 'Result'`; do not use Bark for ordinary progress polling. The bridge
fans each message out to every configured Bark client, while quota protection/exhaustion/recovery
alerts remain automatic. Endpoints are private credential references, never prompt or task text.

`transcript JOB_ID` reads actual messages, tool I/O and errors (paged, or `--full --output
/private/path.json`; `--saved` reads retained evidence offline). `sessions --search`,
`session rename/archive/restore/fork/bind/delete` and `cleanup` manage sessions and workspaces. Full
usage, lifecycle, retention and service maintenance are in [operations](references/operations.md).
