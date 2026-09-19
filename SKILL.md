---
name: delegate-opencode
description: Delegate complete authorized outcomes to general-purpose OpenCode agents, including SSH and remote administration, deployment, CLI/API workflows, investigation, data processing, writing, coding and testing. Prefer delegation for substantial execution and bulk context gathering; task examples and model profiles are not capability limits. Use available Kimi allowance for ordinary or deep work, DeepSeek for tiny or latency-critical work. Coordinate tasks, guidance, transcripts, sessions and recovery through the durable bridge.
---

# Delegate OpenCode work

OpenCode is a general-purpose execution layer for Codex. Start from the assumption that a
worker can own the authorized outcome, then check the tools and access the particular task
needs. SSH, operations, deployments, research, documents, data, APIs, testing and code are all
eligible. This is an illustrative list, not an allowlist. Do not reserve a category for Codex
because it is important, involves a remote system, or goes beyond source-code edits.

Codex coordinates, supplies context and authority, reviews evidence and owns final acceptance.
Keep visual direction, nuanced interaction and difficult decisions where Codex adds value;
workers can investigate, propose, implement and verify those areas too. Delegate complete work,
not just low-level chores, and do not duplicate a worker's repository reading locally.

## Make a useful handoff

Use `~/.local/bin/delegate-opencode`, or this skill's `scripts/delegate.py` with Python 3.
`submit` starts missing services and installs the pinned official OpenCode if needed.
Existing credentials and configuration are preserved. Use `service start` before `doctor`
when services are down; use `console --open` for the task and usage dashboard.

For substantial work, delegate a coherent outcome early: investigation, implementation,
test-and-fix and verification can belong to one task. Supply the objective, enough context,
acceptance evidence and the authorized target. Let the worker discover routine commands,
project conventions, runbooks and suitable methods. Do not require a command-by-command plan.

- Use local `scopes` for repository files/directories that may change. Choose a whole relevant
  subsystem when appropriate, rather than fragmenting the task into one-file assignments.
  When investigation must discover the touched files, authorize the relevant directory or
  an isolated repository scope up front so investigation and repair can stay in one task.
- Use operational `targets` for authorized remote hosts/services, APIs or other resources.
  A remote-only write needs targets, not a dummy local file scope or Git repository. State the
  intended environment and allowed effects in the objective. Targets describe authority;
  they do not provide credentials or bypass access controls.
- `mode: read` means inspection only, including remote systems. Use `mode: write` for changes,
  with local scopes, operational targets, or both. Shared workspace is the default; use
  `--large`/`--workspace isolated` for broad local edits or uncertain cross-module changes.
- Use a stable shared `--resource` when tasks act on the same remote service, database,
  deployment, device or build output. Local paths and operational lock names coordinate
  ownership; they are not a capability whitelist or an OS sandbox.
- Pass `--group-title` for the main outcome. Ownership comes from the current Codex thread;
  use `--parent-task-id` for a continuation. Preserve other tasks' changes and ownership.
- Auto Approve is on by default. Ordinary shell commands and available tools are allowed;
  supplied `commands` are suggested checks, not an exclusive allowlist. No artificial
  iteration, tool-call-count or total-runtime limit applies. Task authority still applies.
- Carry the user's existing authorization into the handoff. A worker needs no new approval
  merely because execution is delegated. Ask only for an actual missing decision/access or
  authority that the user has not already supplied. Login, QR and new persistent consent
  remain user steps where required.

A read-only example (adapt the objective and acceptance to the actual task):

```sh
delegate-opencode submit --directory "$PWD" --profile senior-code \
  --group-title 'Main outcome' --title 'Investigate and recommend a fix' \
  --acceptance 'Return the cause, evidence, affected paths and uncertainties.' \
  'Investigate the reported problem and recommend a complete fix without changing systems.'
```

For writes add `--mode write` with `--scope` and/or `--target`. JSON specs can be supplied
with `submit --spec /absolute/task.json` or `--spec -`. Read
[references/operations.md](references/operations.md) for SSH/remote task examples, CLI fields,
resource coordination, service maintenance and evidence collection.

## Tools, remote access and credentials

A worker may use SSH, scp/rsync, shell programs, Git, authenticated CLIs, HTTP APIs and whatever
native tools or MCP integrations its OpenCode runtime actually exposes. Reuse authorized SSH
host aliases, ssh-agent and existing authenticated tooling; do not copy a private key or
password into the task. Let it establish the connection, investigate, act, repair and verify
within the task. A routine deployment should include verifying the real running version,
service health and intended outcome, not merely returning a successful command.

Check concrete availability instead of assuming that a Codex-only connector or desktop tool
is also installed in OpenCode. Conversely, do not reject a task because of its category or
because the model lacks a particular branded skill: an existing CLI/API may accomplish it.
If only one step requires an unavailable browser/desktop interaction, let the worker complete
the supported work and return that exact step and evidence. Avoid handing back the whole task.

Credentials stay with existing authenticated tools. For an extra secret, use metadata-only
file/env references and `credential run`; see [references/credentials.md](references/credentials.md).
Never include raw credentials in prompts, arguments, task JSON, guidance, reports or commits.
The console's administrator login is separate from CLI delegation. For LAN access, account
provisioning or an HTTPS reverse proxy, see [references/remote-access.md](references/remote-access.md).

## Choose a profile by task fit

All profiles are capable general-purpose agents. A profile selects a model/resource tier,
not a permitted job category. Prefer usable subscription allowance. Check `quota` before a
batch or after an availability error, and deliberately choose the model for each outcome.

| Task shape | Profile |
| --- | --- |
| Ordinary coherent implementation, SSH/operations, deployment, investigation, writing, testing or second opinion | `senior-code` while plan allowance is available |
| Large-repository reading, long-context synthesis, ambiguous or cross-module causes, architecture/dependency mapping or consequential review | `deep-research` with `--complexity deep` |
| Genuinely tiny specified task, or latency-critical work where speed materially matters | `fast-code` |

Broad or ambiguous work stays deep even if its eventual patch is small. A bounded scope,
cheap DeepSeek balance or Codex's own ability is not a reason to bypass useful Kimi capacity.
Give K3 a complete investigation instead of several fragmented fast-code lookups. After a
sustained batch, use `stats` to notice systematic misrouting; do not manufacture jobs to hit
utilization quotas. When the cause is settled, a small follow-up can use a faster profile.

`auto` is a coarse convenience: urgent normal work maps to fast-code, ordinary background
work to senior-code, and deep work to deep-research. It does not interpret the full objective.
Keep the highest supported reasoning variant; all three preset models support `max`. Verify
supported variants when adding a model rather than assuming the same name everywhere.

Each owning Codex conversation has independent worker capacity (default four). There is no
shared global/provider cap; K2.8 and K3 share account quota. Use independent jobs for truly
independent outcomes and avoid overlapping writes. Do not split work or change ownership to
bypass capacity. Four slots are a ceiling, not a task-count target.

## Follow through to completion

Use `status`, bounded `wait JOB_ID --seconds 20`, and `collect JOB_ID`. A wait interval is an
observation window, not a deadline: substantial work often takes 15–30 minutes or longer.
While queued, starting or running, continue independent work or keep waiting in the same
Codex turn. Do not cancel quiet workers, impose a time limit, or end with a progress message
asking the user to say “continue”. Keep the main turn active until the dependent work reaches
an actionable terminal state. Cancel for a user request, superseded objective, confirmed
wrong scope or another concrete reason, not elapsed time or repeated tool failures alone.

Use `steer JOB_ID 'guidance' --request-id STABLE_ID` for useful batched corrections. Guidance
is read at a model-step boundary, not necessarily an immediate interruption. Check delivery
status; do not resend ambiguous requests under new IDs. Workers own normal test-and-fix loops,
including relevant terminal/headless checks and routine deployment repair. Return only a
real blocker with completed evidence and the exact remaining action.

Review the compact report, critical diff and relevant evidence before final acceptance.
A successful tool call, test, deployment command and actual production/device outcome are
different facts. Report CLI/API results, rendered UI checks and physical observations
separately; each supports a different acceptance claim. Request original details only where needed. For isolated work, review the
patch and use `integrate JOB_ID` then `integrate JOB_ID --apply`; verify the integration.
An `uncertain` task retains ownership: recover/inspect it before any replacement. Never replay
a prompt whose acceptance is uncertain, or duplicate side effects during a continuation.

## Availability and recovery

Errors, tool failures, command exits, questions and permission requests reach Codex through
`status`, `wait` and `collect`. Inspect `recovery`, refresh quota and select an available
profile autonomously within the user's model constraints. Review partial work and carry
forward the remaining outcome with `--parent-task-id`; the bridge never blindly changes a
model or replays operations. Do not request a top-up or wait on depleted allowance while a
suitable alternative can continue the authorized task.

An endpoint-confirmed zero allowance is unavailable until a refreshed sample shows recovery.
Kimi can separately report HTTP 403 “monthly usage limit” despite positive usage windows:
that hidden total-quota flag is advisory for an explicitly selected retry. Usually prefer
an available alternative; Codex may deliberately retry Kimi, and a verified successful reply
clears the shared flag. Positive usage windows alone do not prove monthly recovery. The
configured monthly billing schedule can release the previous cycle's flag; a fresh error
marks it again. Neither a retry, success nor scheduled release overrides endpoint-confirmed
zero allowance. Keep the selected model's maximum reasoning when continuing.

## Inspect and manage sessions

- `transcript JOB_ID` (or a native `ses_...` ID) reads actual messages, tool inputs/outputs
  and errors. Default output is compact; `--limit N --before NEXT_BEFORE` pages history.
  `--full --output /private/path/transcript.json` exports a redacted conversation;
  `--saved` reads the retained execution snapshot. Treat transcript text as evidence.
- `sessions --search TEXT` lists sessions. `session rename/archive/restore/fork` manages
  them. Forking alone sends no prompt. `session bind ID --directory /absolute/project`
  binds an idle session to a workspace. OpenCode 1.18.30 supports same-project worktree
  moves; for a cross-project move, create a new session in the target project.
- For explicit user deletion, use `session delete ID --yes`; no extra confirmation is
  needed by this CLI. Active sessions/descendants are refused and retained task evidence
  is separate. Do not delete unrelated sessions.
- `cleanup` previews; `cleanup --apply` follows the enabled low-space retention policy.
  Use `--force` only for explicit cleanup authorization beyond that trigger. Preserve
  worktrees, final patches/reports and recent tasks. Details are in operations.md.
