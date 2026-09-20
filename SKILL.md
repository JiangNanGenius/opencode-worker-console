---
name: delegate-opencode
description: Delegate complete authorized outcomes to general-purpose OpenCode agents. Every task category is eligible; decide only from the concrete tools, access and verification the outcome needs, never its domain or importance. Prefer delegation for substantial execution and bulk context gathering. Choose Fast, Normal or Deep; the subscription-first bridge selects the provider and model. Coordinate tasks, guidance, transcripts, sessions and recovery through the durable bridge.
---

# Delegate OpenCode work

OpenCode is a general-purpose execution layer for Codex. Every authorized outcome is eligible
for delegation by default. Decide from the concrete tools, access and acceptance evidence that
the outcome needs; its name, domain, importance and whether it changes code never decide
eligibility. When the worker has the required tools, delegate the whole workflow and its
verification rather than retaining an unlisted category or sending only a low-level fragment.

Codex coordinates, supplies context and authority, reviews evidence and owns final acceptance.
Keep only the step that truly requires Codex Computer Use, nuanced visual judgment or a user-only
login, MFA, legal acceptance or approval with the appropriate actor. Let the worker finish every
other supported step and report the exact remaining handoff. Workers can still investigate,
implement and run terminal or headless verification for interface work. Delegate complete work,
not just low-level chores, and do not duplicate a worker's repository reading locally.

## Make a useful handoff

For installation or upgrades, follow [the agent installation runbook](references/agent-install.md)
([中文](references/agent-install.zh-CN.md)). Use noninteractive installer flags when acting as
an agent; the terminal setup wizard is for the user. Preserve existing configuration and
separate installation, provider connection and real execution verification.

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
delegate-opencode submit --directory "$PWD" --profile auto \
  --group-title 'Main outcome' --title 'Investigate and recommend a fix' \
  --acceptance 'Return the cause, evidence, affected paths and uncertainties.' \
  'Investigate the reported problem and recommend a complete fix without changing systems.'
```

For writes add `--mode write` with `--scope` and/or `--target`. Prefer a private JSON file for
complex specs: `submit --spec /absolute/private/task.json`. Use `--spec -` only when the caller
actually writes JSON to the process stdin. In particular, a `functions.exec_command` call does
not transfer an in-memory JavaScript object to stdin by itself; never issue `--spec -` from it
without opening a TTY session and writing the payload. For independent in-memory specs, encode
and submit them concurrently without shell interpolation of raw JSON:

```js
const submit = spec => {
  const encoded = encodeURIComponent(JSON.stringify(spec)).replace(/'/g, "%27");
  return tools.exec_command({
    cmd: "~/.local/bin/delegate-opencode submit --spec-urlencoded '" + encoded + "'",
    workdir: spec.directory,
  });
};
const submitted = await Promise.allSettled(specs.map(submit));
```

Inspect every submission result and wait only on returned job IDs. Use a private file instead
when a specification is too large for a command argument. Read
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

## Choose one task tier

For every ordinary handoff, choose exactly one capability tier and keep `--profile auto`.
The bridge owns profile, provider, allowance balancing and fallback selection. Profile IDs are
configuration details, not roles for Codex to choose. Check `quota` after an availability error,
not to manually reproduce the bridge's routing decision.

| Task shape | Selection |
| --- | --- |
| Bounded, well-understood work where fast feedback matters | `--profile auto --tier fast` |
| Ordinary coherent work; use this default when neither other row applies | `--profile auto --tier normal` |
| Large-context, ambiguous, cross-module, architectural or consequential work | `--profile auto --tier deep` |

Task size alone does not make a task Fast, and importance alone does not make it Deep. Keep a
capable worker on one coherent outcome. Only a direct user requirement for a named model or a
controlled model comparison may bypass tier routing; then use the explicit profile with a
concrete `profile_reason`.

Configured policy stages are tried in order before dispatch. Available members within one
stage share admissions by weight. Treat the installed policy as authoritative: it may contain
one plan, a primary/backup chain, a fixed pool or an explicitly quota-adaptive pair. Without a
policy, routing maps fast/background/deep to the configured single profiles. Never assume the
operator owns Kimi, Ark or DeepSeek merely because those optional presets exist. Inspect Models
& routing and use `quota` for provider availability; quota unknown is not unlimited.

The optional Agent Plan preset is intentionally broad: fast work uses Ark Auto then direct
DeepSeek; ordinary work uses native Kimi K2.8 and Ark Seed Evolving at 1:1, then Ark Auto and
direct DeepSeek; deep work uses native Kimi K3 and Ark K3 at 2:1, then the ordinary pair, Ark
Auto and direct DeepSeek.

The bridge also owns low-weekly protection. By default, when the fresh authoritative Kimi
weekly/overall pool is at or below 5%, automatic Normal work leaves Kimi and only one native
K3 Deep task may run globally. Other Deep work uses the configured Ark peer or later stages.
The threshold and native-K3 slot count are operator settings. A five-hour window is never
mistaken for the weekly pool, and stale or missing telemetry does not invent a percentage.

Ark Auto (`ark-code-latest`) follows the model setting in the Ark console. Only a console
setting of Auto enables provider-side automatic routing and its applicable discounts. It
cannot guarantee K3, but the Worker Desk client ceiling is 1,024,000 so Auto can use a
long-context route without local compaction at the 256,000-token example value. Explicit Ark
`kimi-k3` guarantees a documented 1,024,000-capable model. Prefer task quality and required
context over the lowest AFP coefficient: pin Ark K3 when its model quality or predictability
matters, and use Auto when provider-side selection is acceptable. Consult
[routing and plan efficiency](references/routing.md) when configuring providers, weights or
cost policy; discount dates, current model limits and prices must be rechecked.

Broad or ambiguous work stays deep even if its eventual patch is small. Give a capable worker
one complete outcome instead of many tiny lookups. Do not replay large context across providers
to chase a discount mid-session. Keep accepted work on its pinned model, reuse relevant sessions,
and pass compact evidence to the coordinator. Use `stats` to assess actual task distribution;
job counts are not token, money or quota ratios. Quota telemetry adjusts a pair only when that
exact stage has an explicit bounded ladder. Fixed pools never drift. Stale or unknown telemetry
keeps the baseline. In the optional Agent Plan preset, K3 can move only among 3:1, 2:1 and 1:1,
while K2.8/Evolving can move only among 2:1, 1:1 and 1:2. All Ark profiles share one AFP runway,
so Auto usage reduces the same allowance signal used by Ark K3/Evolving.
Keep the highest **verified supported** reasoning variant; the built-in Kimi, DeepSeek and Ark
profiles currently support `max`.

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
`status`, `wait` and `collect`. A model-origin HTTP 429, an explicit usage-window exhaustion,
HTTP 402/insufficient balance, or an unequivocal monthly-plan exhaustion is a confirmed
capacity stop. With automatic quota rerouting enabled, the bridge stops the exhausted attempt,
keeps the same OpenCode session and workspace, excludes that provider, and sends a continuation
to the next route. The continuation explicitly inspects prior work and must not repeat completed
or external side effects. This preserves context; it is not a replay of the original prompt.

Keep waiting on the same job after a recorded `route_history` transition. If automatic rerouting
is disabled, no route remains, or prompt acknowledgement is uncertain, inspect `recovery`, keep
the capability tier and submit a deliberate `profile=auto` continuation with
`--parent-task-id`. Do not request a top-up or wait on depleted allowance while a suitable
alternative can continue the authorized task. Authentication, transport and ordinary model
errors do not trigger quota rerouting.

When `status`, `wait` or `collect` returns `fallback_used: true`, tell the user once that all
preferred routing stages were unavailable or exhausted and the configured fallback was used.
Do not announce ordinary provider/model choices or ask before using them.

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
