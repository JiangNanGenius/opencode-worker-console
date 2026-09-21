# Operations

The entrypoint is `~/.local/bin/delegate-opencode`. The skill also works via
`python3 ~/.codex/skills/delegate-opencode/scripts/delegate.py` if the bin directory is not on PATH.

## Submit and retrieve

```sh
delegate-opencode doctor
delegate-opencode quota
delegate-opencode quota --tier-guidance --compact
delegate-opencode console --open
delegate-opencode submit --directory /absolute/repo --tier normal \
  --title 'Map configuration' 'Locate configuration loading and summarize precedence with file/line evidence.'
delegate-opencode submit --directory /absolute/repo --mode write --scope src/parser.py \
  --profile auto --command 'python3 -m unittest -v tests.test_parser' \
  --acceptance 'Preserve all existing behavior except the specified empty-input fix.' \
  'Fix the already-identified empty-input parser failure and run the authorized tests.'
delegate-opencode status
delegate-opencode wait JOB_ID
delegate-opencode collect JOB_ID
delegate-opencode cancel JOB_ID --reason user_requested
delegate-opencode stats
```

For complex text, prefer `submit --spec /absolute/private/task.json`. `--spec -` requires a
JSON object on the process stdin; calling `functions.exec_command` with a command string alone
does not supply it. For an in-memory JavaScript object use
`encodeURIComponent(JSON.stringify(spec)).replace(/'/g, "%27")`, put that result inside one
pair of shell single quotes, and pass it to `--spec-urlencoded`. This encoding avoids treating
raw task text as shell code. Independent specs may be submitted with `Promise.allSettled`; inspect
every result and wait only on successfully returned job IDs. Use a private file for very large
specifications. Fields: `directory`, `objective`, `title`, `acceptance` (string array),
`tier` (`fast`/`normal`/`deep`), `profile` (`auto` for ordinary use), `profile_reason`
(required for the exceptional explicit profile), `capability_floor` (optional
`fast`/`normal`/`deep`, never higher than `tier`) with a required nonempty
`capability_reason` of 1-500 characters; see [routing](routing.md), `mode` (`read`/`write`),
`scopes` (literal relative path array, repository-local),
`targets` (operational target array such as `ssh:example.com:nginx`),
`commands` (suggested checks; exact allowlist when Auto Approve is off), `resources` (shared lock-name array),
`workspace` (`auto`/`shared`/`isolated`), `large`, `web`, and `notify_on_complete` (boolean).
The CLI form is `--notify-on-complete`; use it only for a key user-facing milestone that merits
one Bark alert after successful completion. Legacy `urgency` and
`complexity` remain accepted and are normalized into a tier.
Do not put API keys or other secrets in tasks. With Auto Approve enabled (the default),
all native tools, including web fetching, run without prompts. In restricted mode,
`web: true` explicitly allows the `webfetch` tool and, as in Auto Approve mode, never
expands scope, authority or any other capability.
Use `group_title` for a readable main-task name, `group_id` to override the current Codex
task ID, and `parent_task_id` for a child of an existing delegated task in the same group.

Prefer one coherent assignment that owns an outcome, such as investigation, bounded
implementation, checks and a concise report. Use `steer` for refinements to that outcome
instead of creating a sequence of microtasks. Split tasks when deliverables or writable
scopes are genuinely independent.

Choose one tier from the uncertainty the worker must resolve and keep `--profile auto`:
Fast is the capable default for a bounded, concrete outcome with a firm direction and clear
acceptance; Normal when a concrete outcome needs broader investigation, synthesis or several
interdependent judgments; Deep for an abstract or unclear objective, an unresolved system-wide
root cause, architecture trade-offs or unusually complex logic. Fast can own complete features,
known fixes, tests, documentation and routine deployment. Large context, many files, long runtime
and importance alone do not make a task Deep, and genuinely Deep work has no separate task-count
limit.
The optional `capability_floor` deliberately prevents conservation or fallback from serving the
task below that tier (CLI: `--capability-floor` plus `--capability-reason`); it is for deliberate
no-downgrade requirements, never a routine tier upgrade, and continuations inherit it.
The first available policy stage wins; members in that stage share actual admissions by
weight. See [routing and plan efficiency](routing.md) for quota guidance, bounded
quota-aware ratios, fallbacks and configuration. A direct user requirement for a named model or
a controlled comparison may pin one model with an explicit profile and `--profile-reason`;
ordinary coordination never chooses a profile.

For ordinary coordinator routing, read `delegate-opencode quota --tier-guidance --compact`: it
returns the `quota_posture` tie-breaker, `conservation_level`, confidence and next-refill labels
without the full provider dump. Use the verbose `quota --tier-guidance` and `quota` views only
for operator investigation.

Without a policy, `auto` uses the legacy single-profile mapping for the selected tier.
It does not infer semantic properties from task text. Every tier is a general-purpose agent;
examples and names do not limit job roles. Do not split a coherent investigation into tiny
fast lookups. Use `stats` to assess actual distribution without manufacturing tasks to hit a
quota. Task counts are not token or cost ratios.

Routine UI work is eligible for delegation. Workers may implement a specified interface and
verify source/DOM structure, localization coverage, build/lint output and an existing headless
browser test command. OpenCode does not have Computer Use; do not ask it to improvise macOS
GUI control with AppleScript, screenshot utilities or similar terminal workarounds. Prefer
Astra for complex real interface operation, visual direction and subtle aesthetic judgment;
workers can contribute proposals and analysis. This allocation reflects available tools and
strengths, not a category ban. Astra owns final acceptance; worker static evidence alone is
not final UI proof.

## Operational targets and remote systems

`targets` declares authorized operational scope for work that is not repository-local:
SSH hosts and services, deployment environments, APIs, databases, and files or data on
remote systems. A write task needs at least one local `scopes` entry or one `targets`
entry, so a remote-only write requires no Git repository and no local file scope and can
run in an ordinary directory. Two write tasks naming the same target never run
concurrently; use `--resource` with stable names such as `ssh:<host>:<service>`
(for example `ssh:example.com:nginx`), not per-model locks, so every profile serializes
against the same remote system.

For an already-authorized remote service repair, submit one complete outcome:

```sh
delegate-opencode submit --directory "$PWD" --profile auto --mode write \
  --target ssh:staging-api:api --resource ssh:staging-api:api \
  --group-title 'Restore staging API' --title 'Repair and verify staging API' \
  --acceptance 'Report the cause, actual remote changes, running version and health.' \
  'Use the existing SSH alias staging-api and its remote runbook to diagnose and repair the api service. Verify the resulting service state and health. Preserve unrelated services.'
```

The working directory may be an ordinary local directory. Add `--scope` only for local
files the outcome also needs to change; add repeated `--target` for additional authorized
operational targets. The console's New task form exposes the same targets and resources.

Workers choose suitable methods themselves: `ssh`, `scp` and `rsync`, plus existing
project scripts, CI workflows and runbooks for routine deployment; established
authenticated tools are reused instead of asking Astra to spell out commands. Connection
setup comes from the user's existing `~/.ssh/config` and `ssh-agent` identities; only a
genuinely new login or consent belongs to the user. When a new secret is unavoidable,
register a metadata-only `credential` reference and run the command through
`credential run` — never place key or password values in prompts, argv, task JSON or
logs. Name the target and environment explicitly in the objective so the authorized
system is unambiguous.

Read tasks stay read-only everywhere, including remote systems: never use read mode to
smuggle remote mutations. Remote effects are evidenced by command output, API responses
and observed state the worker actually saw; local before/after snapshots do not cover
remote changes and are never claimed as such.

## Local console

`console --open` opens the URL saved in `console_url` in the pool configuration. The page
shows ownership groups, nested children, execution status, model, duration, summaries,
DeepSeek balance, Kimi plan windows, Ark AFP windows, current routing shares and nominal
AFP-equivalent economics. It supports search, status filtering, details and
coalesced quota refresh. Click a task title to open its actual OpenCode web session.

The console requires username/password login and checks explicitly allowed Host/Origin
values. It defaults to loopback; optional LAN or HTTPS reverse-proxy access is described in
[remote access](remote-access.md). Its OpenCode gateway supplies server authentication
in the backend. Native response bodies and process logs are not universally redacted;
credential handling and its limits are described in [credential references](credentials.md). The gateway
supports OpenCode streaming and WebSocket traffic. A direct manual continuation inside
OpenCode is outside the pool's task ledger: submit follow-up work through Astra when file
ownership and state tracking must remain coordinated.

Global settings: `~/.config/opencode/delegate-pool.json`. This is separate from the
user's interactive OpenCode configuration. The default is four running workers per owning
Codex conversation (`max_parallel_per_owner`). There is no per-task step or time cap:
a worker iterates until its model stops or the task is cancelled. Different conversations
have independent slots: there is no shared
global or provider concurrency ceiling. Child tasks count against the same owner. Non-Codex
callers fall back to their group/workspace identity. Scope/resource conflicts still queue.
Model/variant changes saved in the console restart idle execution services; owner limits
are read at runtime. Legacy global/provider concurrency and per-task step/time settings no
longer govern dispatch.

## Quota and routing

The adapter reads existing OpenCode API credentials in-process and calls only the fixed
official HTTPS endpoints, without redirects. No credentials are returned to Astra.
DeepSeek currency balances remain separate. Kimi duration, remaining and reset fields are
preserved; Kimi profiles share one account pool. Supplemental `used_ratio` units and booster
wallet payment behavior are not guessed or changed. Unknown values remain unknown.

Queries are coalesced for 60 seconds, refreshed every 5 minutes while running or every
minute while waiting. Idle queues do not query. Transient errors preserve last successful
data for 15 minutes with a stale marker; older data is unknown. Invalid credentials or known
exhausted quota block dispatch; unknown telemetry does not impose a shared concurrency cap.
Plan allowance is preferred for all automatically routed work; `fallback` is the direct paid
DeepSeek fallback in the Agent Plan preset, not a task tier or a small-task shortcut.
The optional Kimi deep-task reserve defaults to 0%, so ordinary
tasks can use available plan allowance. If configured higher, it holds ordinary tasks below
that percentage while allowing deep work. Before dispatch, an automatic task skips unavailable
stages and the bridge chooses the next configured stage. It returns `fallback_used: true` and a
routing notice only when every preferred stage was unavailable or exhausted and the final
fallback was dispatched.

The separate low-weekly guard defaults to 5% and one native-K3 slot. It uses a fresh valid Kimi
`overall` aggregate, falling back only to an exact seven-day window; the five-hour window is not
a weekly proxy. At or below the threshold, automatic Normal tasks exclude every Kimi profile.
Automatic Deep tasks may use a Kimi profile in the first Deep stage only while fewer than the
configured global native-K3 slot count are active. Other Deep tasks choose the Ark peer or a
later stage. Explicit profile requests remain an operator override. Both settings are editable.

After dispatch, a model-origin HTTP 429, explicit usage-window exhaustion, insufficient balance
or unequivocal monthly-plan exhaustion is a confirmed capacity stop. When automatic quota
rerouting is enabled, the bridge confirms the old attempt has stopped, excludes its provider,
applies the low-weekly guard again and submits a continuation to the next route in the same
OpenCode session. This preserves transcript, workspace and partial work. The continuation tells
the new model to inspect existing state and not repeat completed edits, deployments, messages,
payments, destructive operations or other external side effects. It never replays the original
prompt. Authentication failures, network failures and ordinary model errors do not trigger this
transition. If no route is suitable, rerouting is disabled, or acknowledgement is uncertain, the
task surfaces recovery guidance instead.
Fresh telemetry also permits later use of a replenished account. Queue status records
the reason. Unknown quota is not a promise of availability.

An unequivocal model billing error blocks further provider dispatch even if the cached balance
was positive. Two reasons are distinguished. `insufficient_balance` (HTTP 402 or an explicit
balance message) is replenishable: a fresh successful account check clears it after top-up, as
before, and it stays an absolute block even for an explicitly requested profile.
`monthly_usage_limit` is Kimi's hidden monthly plan exhaustion: an HTTP 403 model error
whose message says the monthly usage limit was reached and the quota refreshes next cycle.
That error is authoritative even while `/coding/v1/usages` reports available windows
(`available: true`, remaining 5-hour/overall percentages), because neither window proves the
monthly cycle was restored. Monthly exhaustion is advisory rather than an absolute dispatch
prohibition: automatic routing and candidate alternatives keep avoiding the exhausted provider,
but a task submitted with this provider as an explicit `requested_profile` may still try, and a
verified successful reply clears the flag. A fresh positive query never clears it, the plan usage
endpoint is never parsed as a monthly reset, and `quota.view` presents the provider as
`billing_blocked` with `billing.reason`, `monthly_plan_exhausted: true` and the authentic window
telemetry unhidden. The block is provider-wide, so Kimi K2.8 and K3 share it and can never be
alternatives to each other while it is open. No model probe loop is started.

Quota endpoint exhaustion is a hard availability verdict: an explicit profile retry, monthly schedule, or successful model response never overrides an endpoint reporting zero allowance or an unavailable account. Only the hidden monthly-error flag is retryable; wait for a refreshed positive endpoint sample before dispatching against confirmed empty quota.

Kimi monthly blocks are released by exactly three provenance-guarded paths, none inferred from
window telemetry: an explicit local retry authorization, a configured schedule, or a verified
completed model success. The optional schedule is stored as `kimi_monthly_reset`
(`{enabled, day 1-31, time HH:MM, timezone IANA}`; generic defaults are disabled, day 1,
12:00, Asia/Shanghai). Day 29-31 clamps to the month's last day, and the boundary is computed in
the configured IANA zone, independent of host local time and DST. On quota refresh, including a
cache hit, a monthly block opened strictly before the most recent elapsed boundary is released
with `released: scheduled_reset` and the boundary itself as the watermark - not the possibly
late check time - so a genuine post-boundary error re-blocks for the rest of the cycle while
stale pre-boundary errors and repeated message IDs can never relatch. Releasing is an
authorization to try, never proof the provider recovered; it never touches another reason or a
block bound to a different credential, and an invalid legacy schedule fails closed to disabled.
With the default disabled schedule the old sticky monthly behavior is unchanged.

A genuinely finished, error-free model response can clear a monthly block through
`quota.observe_model_success(provider, identity, started_at, completed_at)`: the bridge passes
the privately captured request credential identity and times, and the clear applies only when
the request started strictly after the block opened, completion is not earlier than the start,
all timestamps are positive and finite, and the identity still equals the provider's current
credential. It persists `released: model_success` with the completion time as the watermark and
preserves message IDs and the credential binding. Pending, empty, failed, unattributed or older
replies never clear anything.

`status`, `wait` and `collect` expose `route_history` for successful in-session transitions and
recovery guidance when no automatic transition occurred. In the latter case,
`automatic_fallback: false` describes that terminal recovery result, while
`autonomous_reselection: true` plus `autonomous_next_action` tells the coordinator to create a
same-tier continuation without asking the user or waiting for quota. Candidates and automatic
continuations retain the configured reasoning `variant`. For a monthly blockage, an explicitly
requested profile on the blocked provider is advisory and may still be submitted to try;
automatic routes and alternatives avoid it. Inspect partial work before creating any deliberate
continuation linked by `--parent-task-id`. Keep healthy non-capacity waits alive, but act on a
confirmed quota or rate-limit stop.

For a future actual recovery, the narrow manual path is
`delegate-opencode quota --retry-provider PROVIDER`. It is labeled as a manual retry
authorization, not proof of recovery: it releases the block so new attempts may run until
a further billing error re-opens it, preserves seen message IDs and the `cleared_at`
watermark, and rejects unknown providers. It does not enforce a single attempt and does not
reinterpret telemetry or reset timestamps. Monthly classification requires exhaustion
wording (`reached`/`exhausted`/`refreshed in the next cycle`, and similar), so a 403 such as
"you do not have permission to view monthly quota" or a stated monthly allowance never
trips the circuit. Historical recovery guidance honors only `manual_retry`, `scheduled_reset`
and `model_success` releases; a positive window sample alone never proves recovery.

No automatic recharge or booster setting changes. Account-wide before/after quota is not
attributed as an exact task charge. `stats` reports observed mixed task latency, not a benchmark.

## Artifacts, recovery and acceptance

State: `~/.local/state/delegate-opencode` (owner-only). Each task has baseline metadata,
scoped before/after snapshots, a binary-capable `changes.patch`, redacted messages,
actual model IDs, tool execution evidence and a structured report. Results are untrusted
worker output until Astra reviews them. Source files in artifacts can contain private code.

Auto Approve defaults to on for this Worker service, allowing all tools without prompts.
Task scope and read-only intent are cooperative instructions, verified through collected changes.
When disabled, native tool scopes and exact shell allowlists apply. Neither mode is an OS sandbox.
Do not run untrusted repositories/scripts with valuable credentials; choose a separate
execution environment for that situation. External edits outside this scheduler are not locked.

An accepted prompt's identity is saved before sending. Recovery observes that session;
it never blindly sends the prompt again. Cancellation retains ownership until abort is
confirmed. `uncertain` means the service cannot confirm state; restore service connectivity.
The queue reports tool failures without a repeated-failure or iteration cutoff; test failures
inside otherwise successful shell commands are additionally the worker's and Astra's
responsibility.

For isolated integration, the source must still match the task's original baseline for every
changed file. `integrate JOB_ID` performs both hash checks and `git apply --check`;
`--apply` applies the reviewed task delta. It does not commit, push or deploy.
After integration, run the appropriate tests and preserve evidence before manually removing
the exact completed worktree with ordinary `git worktree remove`.

OpenCode 1.18.30 accepted a JSON-schema `format` request but returned HTTP 400 when reading
that saved message. This integration therefore requests a plain final JSON report in the
prompt and validates it itself. Missing or invalid reports become `needs_attention`.

## Deployment and service control

Deployment is a normal worker responsibility when included in the user's authorized outcome.
Give the worker the workspace, intended environment, expected result and the necessary
writable scopes or operational targets; use the same `--resource` for jobs deploying to
the same target, with stable `ssh:<host>:<service>` names rather than per-model locks. Let it discover routine
commands from project scripts, CI workflows and runbooks. Provide a method for special processes
only when needed. The worker owns build, deployment, routine troubleshooting and verification
of the running version and health, and returns concise evidence for Astra's final review.
Existing authenticated tooling can be used without reading or exposing credential values.

`python3 scripts/install.py` installs or updates the global skill, launcher and three local
background services. Existing deployment is backed up under `state/releases`; credentials,
settings and results are preserved. Update only when no tasks are active.
For an existing installation with active tasks, use `python3 scripts/install.py --live` to
update the skill, pool observer and console while retaining the OpenCode process and sessions.

The server binds only to 127.0.0.1 with a generated password in an owner-only file; the
password is injected into the process environment. Services are launched by the calling
app so they retain its authorized macOS file-access context. A LaunchAgent was tested and
could not read Documents; it is not used. After reboot/crash, `submit`, `wait`, `cancel`, or
`service start` restarts missing processes and recovers durable tasks. `doctor` checks health.
`python3 ~/.codex/skills/delegate-opencode/scripts/install.py --stop` stops services,
retaining all settings, skill files, evidence and worktrees. Run the installer to start again.

This installation makes the skill discoverable to new Codex turns/sessions. An already
running session can use the absolute CLI path immediately; reload the skill list if needed.

## Agent controls and lifecycle

The CLI starts missing processes before task submission, steering and native session operations.
If the configured OpenCode binary is missing, it discovers an existing installation or installs
`opencode-ai@1.18.30` under the private runtime via the official npm registry. Without npm,
it downloads the matching official GitHub release and verifies the release API's SHA-256 digest.
No sudo or global package replacement is used. Provider login remains with OpenCode.

```sh
delegate-opencode steer JOB_ID 'Check this edge case before finishing.' --request-id investigation-2
delegate-opencode sessions --search parser
delegate-opencode session bind SESSION_ID --directory /absolute/project
delegate-opencode session delete SESSION_ID --yes
delegate-opencode cleanup
delegate-opencode cleanup --apply
```

Steering persists intent before sending `prompt_async`, keeps the same model/permissions,
and delays normal task completion until a reply to the latest guidance is observed. HTTP
acceptance is not proof that the model followed the instruction. Unknown delivery is retained
and not automatically replayed. OpenCode 1.18.30 picks up the new user message in its running
session loop; it does not expose a separate delivery-mode field in its local OpenAPI schema.

Workspace binding uses `/experimental/control-plane/move-session`, with `moveChanges: false`.
It changes the native session directory and preserves the task's original workspace/evidence.
New sessions and tasks accept an explicit workspace; named workspace shortcuts are local.

Permanent deletion in the session library requires typing the title. The CLI `--yes` path is
for a coordinator acting on an explicit user request. Neither path deletes active session
trees. Task-list batch cleanup has its own confirmation: it writes the compact accounting
ledger, deletes linked native conversations and disposable evidence, and releases an isolated
worktree only after integration or when its Git status proves that the worker made no changes.
Unintegrated work remains as a visible task for review.

Cleanup is opt-in in a fresh public installation. It defaults to a 5 GiB trigger, 10 GiB target,
30-day age floor and retention of the latest 20 tasks. Only owned runtime data and pool-linked,
old archived sessions are eligible. Expired bulky evidence plus integrated or provably unchanged
isolated worktrees may be reclaimed; unintegrated changes, compact usage records, patches needed
for review and recent tasks remain. A parent archive with any ineligible descendant is retained.
Removed file byte totals are estimates, not exact disk-space attribution. Cleanup produces a
private `cleanup-last.json`.

Existing sessions can switch between worktrees of the same Git project. OpenCode 1.18.30 rejects cross-project migration; create a new session bound to the target project instead.


## Error bridge

`status JOB_ID` and `wait JOB_ID` include `errors` while a task is running or finished.
`collect JOB_ID` includes errors and pending questions without requiring `--full`.
Each error carries `source`, `code`, `message`, `retryable` (null when unknown), and
`suggested_action`, with tool/command/exit-code or HTTP metadata where available.
A model billing error also carries `billing: true` and `billing_reason`
(`insufficient_balance` or `monthly_usage_limit`). Only a model origin qualifies: transport
text, tool output containing the same phrase, ordinary HTTP 403 authorization failures and
429 rate limits never trip the durable billing circuit, but a model-origin 429 is a confirmed
capacity stop eligible for same-session routing continuation. Historical compact billing errors inform
`recovery` read-only and never re-arm or clear a circuit by themselves.
Errors from earlier failed tool attempts can coexist with a successfully completed report.
A transport error after dispatch means the prompt may have been accepted: observe the
existing session, never blindly resubmit. The bridge reports information when queried;
Codex should use bounded `wait` calls while awaiting a delegated result.

With no `--seconds`, `wait` observes Fast tasks for up to 5 minutes, Normal tasks for 30 minutes
and Deep tasks for 60 minutes. An explicit value has a one-minute minimum and may be longer.
The window does not define how long the worker should take: the command checks approximately
twice per second and returns immediately when work becomes terminal or an actionable queued
blockage appears. Fifteen to forty minutes is normal for coherent implementation, compilation,
tests or deep investigation; configured deep tasks may run longer.

Start ordinary observation with `wait JOB_ID` and no `--seconds`; this preserves the full tier
window. When a command runner yields a process/session ID while that wait remains active, follow
the same process with empty stdin using the longest blocking window the host supports (five
minutes when that is the host cap). Do not issue a separate status query, reconsider the task, or
create a sequence of new `wait --seconds 60` commands between transport waits. The bridge keeps
checking inside the process, so a terminal or actionable result still returns immediately. These
are transport continuations of one wait, not new model-visible status-review cycles.

A nonterminal response after the full tier window contains `terminal: false`,
`continue_waiting: true` and `next_action: call_wait_again`; call `wait` again in the same turn
rather than ending with a progress-only response or asking the user to send “continue”. For
independent jobs, long waits may run concurrently. Quiet output, unchanged status, no visible
diff, a long investigation, high token use, ordinary tool retries or the coordinator's desire to
take over do not prove a stall and are never cancellation reasons. After an accepted steer, give
the worker time to execute it and continue waiting.

Cancellation is an audited exception. Use `cancel JOB_ID --reason REASON`, where `REASON` is
`user_requested`, `superseded`, `wrong_scope`, `duplicate` or `side_effect_risk`. Do not select a
false allowed reason to disguise impatience. A worker remains owned until it completes, reports
an error, asks for coordinator input, or one of those concrete reasons actually applies.


## Read complete conversations on demand

`transcript JOB_ID` or `transcript SESSION_ID` fetches recent native messages with their original
`info` and `parts`, including tool inputs, results and errors. Only GET calls are used; reading
never starts another model turn. Live reads use the directory currently bound to the session.

The default page has 20 messages, oldest-to-newest. If `has_more` is true, pass `next_before`
to `--before` for the previous page. `--limit` accepts 1-100. Fields longer than 6,000 characters
are marked, and `truncated_fields` reports their count. `--full` removes pagination and field
truncation; combine it with `--output /private/path/task.transcript.json` to keep bulk context
in a private file. Known API/OAuth credentials and the Worker server password are redacted.

Use `--saved` explicitly with a worker task ID for the retained execution transcript, even
when OpenCode is unavailable or its session was deleted. `source` and `sampled_at` distinguish
live data from the saved artifact. A live session can keep running after the read completes.
A full session read does not recursively collect separate child-session conversations.
