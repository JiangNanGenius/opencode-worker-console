---
name: delegate-opencode
description: Delegate executable work early to general-purpose OpenCode workers when an authorized task needs investigation, implementation, verification or routine operations. Pass sufficient context, default ordinary executable work to Normal, improve workflow efficiency without automatically selecting Fast, reserve Fast for a concrete model-latency bottleneck, use Deep for strong reasoning and Ultra for the hardest unresolved work, and keep profile=auto; retain coordination, critical judgment and complex device interaction upstream.
---

# Delegate OpenCode work

Use OpenCode as the execution layer, not just as a helper for leftovers. After the minimum
entry checks needed to identify the workspace, authority and existing work, delegate a coherent,
verifiable package before doing its substantive investigation or implementation yourself. An
unfamiliar repository is a reason to delegate discovery, not to read it all before handing off.
Do not wait for the user to ask for workers. Task domain, importance, size and personal familiarity
do not determine eligibility; actual execution tools and authority do.

The upstream harness owns planning, dependencies, critical decisions, integration and final
acceptance. Workers own their package's investigation, implementation, relevant verification and
local repair. Review their critical evidence without repeating all their repository reading or
checks. Direct execution remains reasonable for a genuinely trivial, self-contained action or a
step that specifically needs the upstream harness; do not manufacture tasks to meet a quota.

## Delegate at useful boundaries

- Hand over an observable outcome and its checks, not an individual search or command. Split large
  goals at decision or delivery boundaries; do not send an entire project to Deep and disengage.
- Start independent packages concurrently when their authority, inputs and resources are ready.
  While they run, handle coordination, user decisions or independent work rather than duplicating
  the assigned package. Dependent stages consume verified outputs; overlapping writes or live
  resources require serialization, not extra worktrees or empty-slot targets.
- Treat requests to "improve efficiency", "speed things up" or "unblock work" as workflow goals,
  not automatic Fast requests. Identify the actual bottleneck: improve package boundaries and
  handoffs, dispatch ready independent work, resolve dependency/resource conflicts, and reuse
  verified outputs rather than repeating investigation or checks. These changes do not by
  themselves change a task's tier.
- At a meaningful result, reassess the remaining work. Use a linked task for a distinct deliverable
  and `task retier` for the same outcome, preserving its session and handoff. Resolved Deep
  uncertainty normally continues as Normal. Select Fast only under the criteria below; reassess
  it when the bottleneck changes and return to Normal when its speed benefit is gone and
  meaningful work remains. Cost, quota and elapsed time are bridge routing concerns, not reasons
  for the coordinator to change a task's tier.

## Give enough context without delaying dispatch

For a simple, self-contained task, a short objective, necessary context and concrete acceptance
are sufficient. Do not impose the full handoff template on it. For substantial or continuing work,
use [the handoff contract](references/handoff.md) and a private JSON spec with a structured
`objective`. Pass the relevant facts already known: goal, current state, settled decisions,
evidence and artifact locations, authority, constraints, dependencies and acceptance. Include
suggested direction and useful entry points when known; separate facts from hypotheses and let
the worker adjust its method to evidence. For continuations, identify done, in-progress and
remaining work, including side effects that must not be repeated.

The cold-start test is whether a capable engineer without the upstream chat can proceed without
guessing a settled decision or redoing known work. Missing repository facts can be the worker's
investigation; missing context already held by the coordinator should not be. Do not turn the
handoff into a command-by-command recipe or include credentials, raw chat or irrelevant history.
There is no arbitrary word limit for necessary context. Use `steer` for material new information,
not to repair a deliberately thin initial assignment.

## Allocate interface work by tools and judgment

Workers can implement interfaces, run terminal/headless checks and use reliable available tools
for lightweight UI interaction. Delegate DOM or structured-element operations and straightforward
clicks, input and state checks when the goal is clear, the path is stable, the result is observable
and failure is recoverable. Decide by uncertainty and feedback, not by a fixed number of clicks.
Check the execution runtime's actual capabilities; never assume it has the harness's Computer Use,
connector or authenticated session. Do not install a new UI tool solely to force delegation.

Keep complex device/cross-application interaction, sustained visual interpretation, ambiguous
popups, aesthetic direction and final experience judgment with the upstream harness (Codex in
this workflow). Transfer only the steps requiring those capabilities; the worker can complete
implementation, builds, supported deployment and other verification around them. If a lightweight
flow becomes ambiguous or lacks a reliable tool, hand over completed actions, current state,
observations/screenshots, artifacts, the remaining goal and any uncertain side effect. Do not
restart the whole assignment or improvise blind coordinate/terminal GUI workarounds. See
[the UI operations boundary](references/operations.md#interface-operation-boundary).

## Set authority and workspace

`mode: read` is inspection only. Writes require local `scopes`, operational `targets`, or both;
authorized remote work needs no dummy repository scope. Give sufficient subsystem authority to
complete the package and preserve other tasks' changes. Targets convey scope, not credentials
or permission to bypass controls. Existing user authorization carries into the handoff; ask only
for a genuinely missing decision, access or authorization. Return a platform-required user step
precisely without handing back the supported work.

Use the bound shared/main checkout (`workspace: shared` or omit it), including large and
long-running changes. Prefer scope/resource locks or sequential work over duplicated dependencies
and build products. Neither coordinator nor worker should create a Git worktree for convenience.
Request `--workspace isolated` only for user-requested isolation, unavoidable conflicting writes
that must run concurrently, or a destructive experiment. Include integration and safe release in
that package. Give tasks sharing a deployment, service, database or device one stable `--resource`
name, not separate per-model locks.

Auto Approve is on by default: ordinary tools are allowed and `commands` are suggested checks,
not an exclusive list. No artificial iteration, tool-call or runtime limit applies; task authority
still does. Workers may use SSH, authenticated CLIs, APIs and available native/MCP tools. Reuse
existing authentication; pass extra secrets only through metadata-only references and `credential
run` ([credentials](references/credentials.md)), never prompts, argv, reports or commits.

## Choose one tier by uncertainty and latency value

Keep `profile=auto`; the bridge chooses providers, models, balancing and fallback.

| Remaining task shape | Tier |
| --- | --- |
| Ordinary executable work: clear/bounded implementation, investigation, tests, docs, deployment, finalization | `normal` (default) |
| Direction, constraints and acceptance are clear; remaining model-driven work is the current bottleneck, and faster output has an evidence-backed benefit for a named near-term milestone or explicit deadline | `fast` |
| Complex investigation or implementation needing strong reasoning | `deep` |
| Especially difficult architecture, stubborn unknown root causes, long-horizon reasoning, or two rounds without material progress after checkpoint review | `ultra` |

The four-tier service uses Fast (DeepSeek Flash), Normal (K2.8 Preview / Qwen3.8 Flash),
Deep (Qwen3.8 Max) and Ultra (native Kimi K3.1 Pro, standard speed, maximum supported
reasoning). Old native and Ark K3 routes are retired; historical records remain unchanged.
Start clearly extreme reasoning work directly on Ultra. After two rounds without material
progress, review the checkpoint and upgrade when reasoning is the blocker. Authentication,
permissions, missing tools and environment failures need their own repair before escalation.
When uncertainty is resolved, give routine implementation to Normal. By default, confirmed
Ultra quota, five-hour/monthly allowance or balance exhaustion can continue on Qwen3.8 Max
without changing the Ultra tier, task, session, authority or checkpoint. It does not fall below
Max; when both routes are unavailable it waits. Fresh recovery allows a return to K3.1 Pro only
at an owned idle checkpoint; completed reports are never rerun. Authentication, permissions,
transport failures and manual exclusions do not authorize this downgrade. Explicit profile pins
or `capability_floor=ultra` wait for their required model. Operators may disable this chain with
`ultra_fallback_to_deep=false`; ordinary coordinators still use `profile=auto`.

Kimi weekly protection has a base / maximum of 30% by default. The bridge may release idle
protection to Normal in the final half of the real weekly cycle; Ultra demand, including activity
within 30 minutes, or unknown/stale signals keep the base. This is routing policy, not a reason
for the coordinator to change task tiers. Released percentage points are not available quota and
cannot reclaim spent allowance. Operators may disable `kimi_weekly_reserve_dynamic` to keep the
base fixed; ordinary coordinators continue to use `profile=auto` without inspecting quota.

Entering Ultra creates a fresh native model session while keeping the bridge task, authority and
checkpoint. Carry completed work, evidence, decisions and remaining work; do not replay completed
side effects or inject the old model's reasoning trace. See [routing](references/routing.md).

Normal remains the default even when work is simple or its direction is known. Generic efficiency
or urgency wording, a routine downstream dependency, and critical-resource ownership do not by
themselves justify Fast. Before selecting or switching to Fast, identify the remaining
model-driven bottleneck and explain which concrete milestone earlier output will advance.
Queue/resource contention, compilation, external CI/test execution and pure waiting call for
workflow changes, not a faster model. Do not promote an entire pipeline because each stage has
an eventual consumer.

Fast is a capable latency-priority tier, not a weak simple-only tier. It can own a complete
outcome and investigate ordinary local details without line-by-line prescription or a full
upstream repository read. Select it proactively when the criteria hold; no explicit user Fast
instruction is required. Deep covers complex reasoning; Ultra handles especially difficult or stalled reasoning.
Size, importance and context alone do not imply either tier, and urgency alone cannot bypass unresolved reasoning needs. Fast and Normal can both
deliver complete outcomes. Do not carry a parent's Deep or Ultra tier into all implementation, or disguise
a genuinely difficult problem as many Fast lookups. There is no Deep or Ultra task-count limit. Ordinary
handoffs do not inspect quota, conservation, provider health or prices, and never change tier to
protect a plan. Use `quota --tier-guidance` only for explicit operator troubleshooting. Ordinary
task results hide route choices, selected models, capacity-wait reasons and individual
provider/tier/plan exhaustion or recovery hints while any legal automatic route can continue.
Only when every configured automatic route across all tiers is proven exhausted may
`status`/`wait`/`collect` expose one generic whole-route blockage. If just the requested tier is
blocked while another tier is usable, or the reading is unknown/unproven, they stay an ordinary
queued task with no recovery object; `wait` simply continues. The requested tier and capability
floor remain in the base task. Disabled or unconfigured policies do not qualify for the whole-
service message. Auth, permission, transport failures and explicit model pins or
capability floors keep their actionable detail. The console and operator quota views retain
the full per-provider telemetry. See [routing](references/routing.md) and
[operations](references/operations.md). Keep the highest verified supported reasoning variant
on continuations.

## Submit and continue

Use `~/.local/bin/delegate-opencode` (or this skill's `scripts/delegate.py`). `submit` starts
missing services and installs the pinned official OpenCode when needed; `service start` precedes
`doctor` when services are down. Example read-only investigation:

```sh
delegate-opencode submit --directory "$PWD" --tier normal --profile auto \
  --group-title 'Main outcome' --title 'Investigate the reported failure' \
  --acceptance 'Return the cause, evidence, affected paths and remaining uncertainty.' \
  'Investigate the reported failure and recommend a complete fix without changing systems.'
```

For writes add `--mode write` and `--scope`/`--target`. Use `--group-title` for the main outcome
and `--parent-task-id` for a linked child. Prefer `submit --spec /absolute/private/task.json` for
substantial specs; `--spec -` only works when JSON is actually supplied on stdin. In-memory spec
encoding, fields and CLI examples are in [operations](references/operations.md); inspect every
submission result and wait on returned job IDs. Installation/upgrades use the
[agent install runbook](references/agent-install.md) ([中文](references/agent-install.zh-CN.md));
LAN and reverse-proxy setup is in [remote access](references/remote-access.md).

For the same outcome, change tier with `task retier JOB_ID --tier normal --reason 'Root cause
and acceptance rule are now known; the remaining work is ordinary bounded implementation.'
--handoff-file /private/checkpoint.json --request-id STABLE_ID`. The owner-only (`0600`)
checkpoint has `done`, `remaining`, `decisions`, `evidence` and `do_not_repeat`; use the actual
state, not example claims. For Fast, identify the remaining model-driven bottleneck and the
concrete milestone earlier output will advance. The bridge preserves task, session, workspace,
authority and cumulative
usage and switches at a safe continuation boundary. `pending` is not `applied`; observe
`tier_change.status` and never replay an uncertain acknowledgement. Reuse an ID only with identical
content; keep waiting on the same task. See [handoff checkpoints](references/handoff.md#same-task-tier-checkpoint).

## Wait through completion

Use `wait JOB_ID` without `--seconds`: Fast observes for 5 minutes, Normal 30 and Deep/Ultra 60, returning
immediately on completion, failure or actionable attention. These are observation windows, not
execution deadlines. Follow a yielded command session with empty-stdin blocking polls for the
longest duration the host permits; do not interleave repeated `status` or fresh short waits.
If `continue_waiting: true`, call `wait` again in the same turn. Keep the turn active until terminal
or genuine coordinator input; do not stop with a request for the user to manually continue.

For long builds, tests, deployments, migrations or CI, request phase reports through `progress
report`. Percentage and ETA require real item/step counts; otherwise use indeterminate progress.
Quiet output, no diff, long runtime or high token use do not establish a stall. Workers own their
repair loops. Use `steer JOB_ID 'material guidance' --request-id STABLE_ID` and let accepted guidance
execute. Cancellation is only for `user_requested`, `superseded`, `wrong_scope`, `duplicate` or
`side_effect_risk`, never impatience.

A confirmed model-origin 429, window exhaustion, 402/insufficient balance or monthly exhaustion
may trigger automatic same-session fallback. Keep waiting; route changes are progress, and the
continuation does not tell the worker which provider, plan or tier stopped. If no legal route
remains, the original task retains its authority and checkpoint and waits for admission to
recover. Only proven exhaustion across the full service permits a generic whole-route
blockage in status/wait. Explicit pins wait for
the same profile. Do not create a duplicate task.
Temporary fallbacks return to a recovered preferred route at a confirmed idle checkpoint;
finished tasks stay finished. Auth,
transport and ordinary model errors do not trigger automatic rerouting. A transport failure may
follow accepted dispatch: observe, never blindly replay the prompt or completed side effects.

## Review, memory and notifications

Review the compact report, critical diff and relevant evidence. Passing tests, actual deployment,
rendered/device behavior and user acceptance are distinct facts; static checks alone do not prove
UI behavior. For isolated work, review then `integrate JOB_ID`, apply with `--apply`, verify and
release the managed checkout; preserve unintegrated changes. An `uncertain` task retains ownership
until its state is resolved.

When project memory is enabled, use `memory search --directory "$PWD" 'topic'` before repeating
broad discovery. Both upstream and workers can retain verified project decisions; never store
secrets or speculation. Read/update/pin/delete through the CLI or console; see
[OpenViking memory](references/openviking-memory.md) for backend, migration and management details.

Use `--notify-on-complete` only for a key milestone worth one Bark alert. For a verified milestone
outside one worker task, use `notify --title 'Title' --body 'Result'`. Routine progress stays silent;
configured clients receive each event independently and quota alerts remain automatic. Endpoints
are private credential references, never task text.

For original evidence, use paged `transcript JOB_ID` or `--full --output /private/path.json`
(`--saved` reads retained evidence). Sessions support binding, archive and deletion. An explicit
cleanup request may use `task delete JOB_ID --yes` or `task clear-finished --yes` to remove linked
sessions and releasable evidence/worktrees while the compact usage ledger follows its retention.
See [operations](references/operations.md) for lifecycle and maintenance details.
