# Operations

The entrypoint is `~/.local/bin/delegate-opencode`. The skill also works via
`python3 ~/.codex/skills/delegate-opencode/scripts/delegate.py` if the bin directory is not on PATH.

## Submit and retrieve

The following is a command catalog, not a startup sequence. Ordinary handoffs submit directly;
quota/tier-guidance commands are operator diagnostics, and `notify` is for a verified milestone.

```sh
delegate-opencode doctor
delegate-opencode quota
delegate-opencode quota --tier-guidance --compact
delegate-opencode notify --title 'Release ready' --body 'TestFlight processing completed.'
delegate-opencode console --open
delegate-opencode submit --directory /absolute/repo --tier normal \
  --title 'Map configuration' 'Locate configuration loading and summarize precedence with file/line evidence.'
delegate-opencode submit --directory /absolute/repo --mode write --scope src/parser.py --tier normal \
  --profile auto --command 'python3 -m unittest -v tests.test_parser' \
  --acceptance 'Preserve all existing behavior except the specified empty-input fix.' \
  'Fix the already-identified empty-input parser failure and run the authorized tests.'
delegate-opencode status
delegate-opencode wait JOB_ID
delegate-opencode collect JOB_ID
delegate-opencode cancel JOB_ID --reason user_requested
delegate-opencode stats
delegate-opencode quota --import-snapshot /private/path/snapshot.json
delegate-opencode usage-report --windows 1,24,48
```

For substantial, continuing or complex work, follow the
[handoff contract](handoff.md) and prefer `submit --spec /absolute/private/task.json`. `--spec -` requires a
JSON object on the process stdin; calling `functions.exec_command` with a command string alone
does not supply it. For an in-memory JavaScript object use
`encodeURIComponent(JSON.stringify(spec)).replace(/'/g, "%27")`, put that result inside one
pair of shell single quotes, and pass it to `--spec-urlencoded`. This encoding avoids treating
raw task text as shell code. Independent specs may be submitted with `Promise.allSettled`; inspect
every result and wait only on successfully returned job IDs. Use a private file for very large
specifications. Fields: `directory`, `objective`, `title`, `acceptance` (string array),
`tier` (`fast`/`normal`/`deep`/`ultra`), `profile` (`auto` for ordinary use), `profile_reason`
(required for the exceptional explicit profile), `capability_floor` (optional
`fast`/`normal`/`deep`/`ultra`, never higher than `tier`) with a required nonempty
`capability_reason` of 1-500 characters; see [routing](routing.md), `mode` (`read`/`write`),
`scopes` (literal relative path array, repository-local),
`targets` (operational target array such as `ssh:example.com:nginx`),
`commands` (suggested checks; exact allowlist when Auto Approve is off), `resources` (shared lock-name array),
`workspace` (`auto`/`shared`/`isolated`), `large`, `web`, and `notify_on_complete` (boolean).
`auto` uses the shared/main checkout, including broad repository writes; scope and resource locks
serialize overlapping writers. `large` remains accepted for compatibility but does not create a
worktree. Large, important, long-running and code-writing tasks still use this shared checkout.
Neither the coordinator nor worker should run `git worktree add` as a convenience. Request
`isolated` explicitly only for user-requested isolation, unavoidable concurrent conflicting writes,
or a destructive experiment. Prefer serialization when it avoids another dependency/build tree.
The CLI form is `--notify-on-complete`; use it only for a key user-facing milestone that merits
one Bark event after successful completion. Each event is delivered independently to every
configured client. For a verified milestone outside one worker task, use `notify --title ...
--body ...`; routine progress stays silent. Legacy `urgency` and
`complexity` remain accepted and are normalized into a tier.
Do not put API keys or other secrets in tasks. With Auto Approve enabled (the default),
all native tools, including web fetching, run without prompts. In restricted mode,
`web: true` explicitly allows the `webfetch` tool and, as in Auto Approve mode, never
expands scope, authority or any other capability.
Use `group_title` for a readable main-task name, `group_id` to override the current upstream-harness
task ID, and `parent_task_id` for a child of an existing delegated task in the same group.

The coordinator owns the overall plan and integration. Hand off coherent work packages with
their necessary checks; split substantial goals at useful decision or delivery boundaries.
Independent packages may run concurrently, while dependent stages consume verified outputs
sequentially. Serialize overlapping writes/resources. Do not split into individual searches or
commands, and do not send the whole project to Deep merely because one part is uncertain.
Use `steer` for refinements and `task retier` when the same outcome's remaining reasoning needs
or concrete latency value changes.

Choose one tier from the uncertainty the worker must resolve and keep `--profile auto`:
Normal is the default for ordinary executable work — a bounded, concrete outcome with a firm
direction and clear acceptance, including investigation, tests, documentation, deployment and
finalization; known direction or low uncertainty alone is not a reason to select or switch Fast.
Fast is the latency-priority tier when direction, constraints and acceptance are clear AND faster
model work is reasonably expected to materially advance a concrete near-term milestone: an
explicitly urgent delivery or deadline, or a coordinator-identified dependent task or phase that
is actually ready and currently blocked by this work. A routine serial dependency — someone
will eventually need the output — or mere ownership of a critical resource is not by itself a
Fast trigger, and queueing, compilation, external CI/test execution or pure waiting is not sped
up by faster model output; keep Normal unless the remaining model-driven work is itself the
consequential current blocker. Fast can own complete features, known fixes, tests, documentation
and routine
deployment; it is not weak or simple-only, and it does not need a line-by-line prescription or a
fully pre-read repository. Deep handles complex reasoning; Ultra handles especially difficult
architecture, stubborn root causes, long-horizon reasoning and checkpoint-reviewed escalation
after two rounds without material progress. Large context, many
files, long runtime and importance alone do not make a task Deep, urgency alone cannot bypass
unresolved reasoning needs, and genuinely Deep work has no separate task-count limit.
The optional `capability_floor` deliberately prevents conservation or fallback from serving the
task below that tier (CLI: `--capability-floor` plus `--capability-reason`); it is for deliberate
no-downgrade requirements, never a routine tier upgrade, and continuations inherit it.
Deep enforces a Deep floor even when the field is omitted or weaker. Ultra's default guarded
capacity fallback may use Qwen3.8 Max; an explicit `capability_floor=ultra` prevents it.
The first available policy stage wins; members in that stage share actual admissions by
weight. See [routing and plan efficiency](routing.md) for quota guidance, bounded
quota-aware ratios, fallbacks and configuration. A direct user requirement for a named model or
a controlled comparison may pin one model with an explicit profile and `--profile-reason`;
ordinary coordination never chooses a profile.

Ordinary coordinators choose Fast, Normal, Deep or Ultra solely from task uncertainty and concrete
latency value, defaulting to Normal, and keep `profile=auto`; they do not inspect quota or provider
state. `quota --tier-guidance --compact`
returns only that stable contract. The verbose `quota --tier-guidance` and `quota` views are
operator diagnostics for the console, configuration and incident investigation.

Without a policy, `auto` uses the legacy single-profile mapping for the selected tier.
It does not infer semantic properties from task text. Every tier is a general-purpose agent;
examples and names do not limit job roles. Do not split a coherent investigation into tiny
fast lookups. Use `stats` to assess actual distribution without manufacturing tasks to hit a
quota. Task counts are not token or cost ratios.

## Interface operation boundary

Interface work is eligible for delegation; decide each step by actual tools, uncertainty and
feedback rather than treating every UI task as upstream-only. Workers can implement and verify
source/DOM structure, localization, builds and headless behavior. With reliable tools already
available in their runtime, they can also perform structured-element operations, lightweight
clicks/input and state checks. A dedicated desktop-control MCP is not assumed, nor is the upstream
harness's authenticated browser/device session available merely because the harness can use it.

Delegate interaction when the goal is clear, the path is stable, results can be observed and a
failed step is recoverable without uncertain side effects. A longer deterministic flow can be
lighter than a short flow requiring repeated visual interpretation; there is no click-count rule.
Prefer established DOM/accessibility locators, existing automation drivers or verified scripts.
A screenshot capture can be evidence when supported; it does not by itself provide control.
Do not improvise blind coordinate/terminal GUI workarounds or install a new interface tool solely
to force delegation. Existing authorization and platform confirmation requirements still apply.

The upstream harness (Codex in this workflow) handles complex device/cross-application operation,
ambiguous popups, sustained visual interpretation, aesthetic direction and final experience
judgment. Transfer only those steps, retaining the worker's supported implementation, build,
deployment and verification package. If a light flow becomes ambiguous or lacks a reliable tool,
return completed actions, current state, observations/screenshots, artifact locations, remaining
goal and uncertain side effects. Continue supported independent work; do not restart the whole
assignment. The upstream harness owns final acceptance, and static evidence is not UI proof.

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
authenticated tools are reused instead of asking the upstream harness to spell out commands. Connection
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
OpenCode is outside the pool's task ledger: submit follow-up work through the upstream harness when file
ownership and state tracking must remain coordinated.

Global settings: `~/.config/opencode/delegate-pool.json`. This is separate from the
user's interactive OpenCode configuration. The default is four running workers per owning
upstream-harness conversation (`max_parallel_per_owner`). There is no per-task step or time cap:
a worker iterates until its model stops or the task is cancelled. Different conversations
have independent slots: there is no shared
global or provider concurrency ceiling. Child tasks count against the same owner. Other
callers fall back to their group/workspace identity. Scope/resource conflicts still queue.
Model/variant changes saved in the console restart idle execution services; owner limits
are read at runtime. Legacy global/provider concurrency and per-task step/time settings no
longer govern dispatch.

## Quota and routing

The adapter reads existing OpenCode API credentials in-process and calls only the fixed
official HTTPS endpoints, without redirects. No credentials are returned to the upstream harness.
DeepSeek currency balances remain separate. Kimi duration, remaining and reset fields are
preserved; Kimi profiles share one account pool. Supplemental `used_ratio` units and booster
wallet payment behavior are not guessed or changed. Unknown values remain unknown.

Queries are coalesced for 60 seconds, refreshed every 5 minutes while running or every
minute while waiting. Idle queues do not query. Transient errors preserve last successful
data for 15 minutes with a stale marker; older data is unknown. Invalid credentials or known
exhausted quota block dispatch; unknown telemetry does not impose a shared concurrency cap.
Plan allowance is preferred for all automatically routed work; `fallback` is the direct paid
DeepSeek fallback in the Agent Plan preset, not a task tier or a small-task shortcut.
Kimi base / maximum weekly protection defaults to 30%. Dynamic release is enabled by
`kimi_weekly_reserve_dynamic` (default `true`): idle final-half-cycle protection declines toward
min(base, 5)% by the real weekly reset; Ultra demand or unknown/stale quota, reset or demand
signals keep the base. Disable it for fixed base protection. Below the effective threshold only
K3.1 Pro can consume the reserve; explicit old-model pins cannot bypass it. A fresh named
weekly/overall signal is separate from the five-hour limit and account total. Missing or stale
weekly data pauses ordinary Kimi admissions; an account whose official plan has no weekly
window is marked not applicable. In-flight requests may finish, so this is a scheduling reserve.

When the weekly remainder is at or below 5% and cannot last until reset, Ultra concurrency is
limited to one. By default (`ultra_fallback_to_deep=true`), confirmed Ultra quota, five-hour/
monthly allowance or balance exhaustion can continue on eligible Qwen3.8 Max from the Deep
route. Keep Ultra's own policy restricted to Ultra models; do not add Max to it. The task keeps
its Ultra tier, ID, session, authority and checkpoint. It waits if Max is also unavailable and
never falls below Max. Explicit profile pins or an explicit Ultra floor wait for their required
model. Authentication, permissions, transport failures, disabled profiles and manual exclusions
do not authorize a downgrade. Fresh recovery permits return only at an owned idle checkpoint;
completed reports are never rerun. Account total quota comes only from a real logged-in account reading; never infer it
as four weekly windows. Missing monthly data stays unknown. OAuth/account identity must match
the inference key before account quota can govern that key.
After dispatch, a model-origin HTTP 429, explicit usage-window exhaustion, insufficient balance
or unequivocal monthly-plan exhaustion is a confirmed capacity stop. When automatic quota
rerouting is enabled, the bridge confirms the old attempt has stopped, excludes its provider,
applies the low-weekly guard again and submits a continuation to the next route in the same
OpenCode session. This preserves transcript, workspace and partial work. The continuation tells
the new model to inspect existing state and not repeat completed edits, deployments, messages,
payments, destructive operations or other external side effects. It never replays the original
prompt, and it does not disclose the failed provider, plan, tier, balance or transition trigger
to the worker. Task `status`, `wait` and `collect` projections likewise omit individual capacity
stops and recovery hints while a legal automatic route remains. A requested-tier-only blockage,
unknown/stale reading or unproven denial stays an ordinary queued task with no recovery object,
and `wait` continues; the base task keeps its requested tier/floor. Only after every configured
automatic route across all tiers is proven exhausted may they report a generic
`automatic_routes_exhausted` blockage; unknown/stale reads, auth/transport errors, manual
exclusions, capability floors and disabled or unconfigured policies never qualify. Explicit
model pins, capability floors, auth, permission and transport failures keep their actionable
detail; the console and `quota` views retain full operator telemetry. Authentication
failures, network failures and ordinary model errors do not trigger this transition. If no route
is suitable or rerouting is disabled, the original task stays active
with a capacity-wait reason and a durable checkpoint. It retains scope/resource ownership but
does not occupy an execution slot while merely waiting. Explicit model pins wait for that
profile. Once admission recovers, original waiting tasks and maintenance checkpoints take
priority over new work for the same owner's execution slots. Recovery rechecks current
admission, capability floors and execution concurrency before
persisting one continuation intent and message ID. A lost acknowledgement is observed, never
replayed. Authentication and permission failures remain explicit blockers.
After verified recovery, a temporary automatic fallback can return to a higher-priority route
at a confirmed idle checkpoint, with a debounce and route-history reason. Busy streams/tools,
explicit pins and finished tasks are left alone. Fresh telemetry is not evidence that another
seat's inference key recovered; the provider/credential-specific recovery gates still apply.

Alibaba Token Plan prepaid seats add key-specific operations. With Bark quota transitions
enabled, a fresh positive Credits reading at or below 10% warns once to prepare the next seat;
an authoritative zero or a confirmed model 402/insufficient-balance/monthly exhaustion warns
immediately to open/assign the next seat and replace the Token Plan inference API Key; and a
confirmed model 401 emits a separate check/replace inference Key alert. Deduplication is per
opaque inference credential identity, so a later seat/key re-arms; titles and bodies never
contain the identity. While the current key's circuit is active, positive whole-account
telemetry never re-arms the exhausted-key edge, so later failure scans stay deduplicated.
Native 429 remains transient capacity and never asks for a purchase. These are
edge-triggered pushes, not a recurring reminder schedule. A control-plane
`GetSubscriptionStats` AK/SK failure is unknown telemetry, not an inference-key failure.

An unequivocal model billing error blocks further provider dispatch even if the cached balance
was positive. Two reasons are distinguished. `insufficient_balance` (HTTP 402 or an explicit
balance message) is replenishable: a fresh successful account check clears it after top-up, as
before, and it stays an absolute block even for an explicitly requested profile.
`monthly_usage_limit` is Kimi's hidden monthly plan exhaustion: an HTTP 403 model error
whose message says the monthly usage limit was reached and the quota refreshes next cycle.
That error is authoritative even while `/coding/v1/usages` reports available windows
(`available: true`, remaining 5-hour/overall percentages), because neither window proves the
monthly cycle was restored. For Kimi, the recorded monthly circuit blocks both automatic routing and explicitly named
profiles. Selecting a profile alone never releases the circuit. Automatic recovery may authorize
a bounded retry from a fresh, verified same-account month total; an explicit local
`quota --retry-provider kimi-for-coding` remains available for operator-directed attempts.
Neither path proves model-key health. Weekly/5-hour telemetry never clears the monthly flag,
and its reset is never inferred as a monthly reset. While blocked, `quota.view` presents it as
`billing_blocked` with `billing.reason`, `monthly_plan_exhausted: true` and the authentic window
telemetry unhidden. The block is provider-wide, so Kimi K2.8 and K3.1 Pro share it and can never be
alternatives to each other while it is open. No model probe loop is started.

A confirmed model-origin HTTP 401 from Alibaba is a separate `credential_rejected` circuit bound
to the inference key: it is not a billing reason, never uses the quota-query AK/SK pair, and its
alert asks for the inference Key only. A recorded dispatch identity that no longer matches the
current key is stale: a late old-key 402/401 neither trips nor alerts the new key, while legacy
tasks without matching provenance keep the existing current-credential behavior. A provider marked
`prepaid` also refuses aggregate
account-API or imported-snapshot release of a confirmed balance, auth or monthly block, because the
Alibaba `GetSubscriptionStats` Credits value is a multi-seat group aggregate that may describe a
different seat. A genuine inference-key file identity change detaches the credential-bound block;
a verified completed response on the current key or `quota --retry-provider` can also release it.
A failed control query remains stale/unknown telemetry and, in prepaid mode, does not by itself
disable otherwise usable inference; confirmed zero, expiry and the active inference-key circuit
still block.

Quota endpoint exhaustion is a hard availability verdict: profile selection, explicit local retry release, a monthly schedule, or a successful model response never overrides an endpoint reporting zero allowance or an unavailable account. For Kimi this includes a real zero in the five-hour window or the verified account total. Only the hidden monthly-error flag is retryable; wait for a refreshed positive endpoint sample before dispatching against confirmed empty quota.

Kimi monthly blocks have provenance-guarded recovery paths: an explicit local retry, a
configured schedule, a verified completed model success, or bounded retry authorization from a
fresh official same-account `month_total` (and every present `month_code`). Weekly/5-hour
telemetry never substitutes for these real month pools. The optional schedule is stored as `kimi_monthly_reset`
(`{enabled, day 1-31, time HH:MM, timezone IANA}`; generic defaults are disabled, day 1,
12:00, Asia/Shanghai). Day 29-31 clamps to the month's last day, and the boundary is computed in
the configured IANA zone, independent of host local time and DST. On quota refresh, including a
cache hit, a monthly block opened strictly before the most recent elapsed boundary is released
with `released: scheduled_reset` and the boundary itself as the watermark - not the possibly
late check time - so a genuine post-boundary error re-blocks for the rest of the cycle while
stale pre-boundary errors and repeated message IDs can never relatch. Releasing is an
authorization to try, never proof the provider recovered; it never touches another reason or a
block bound to a different credential, and an invalid legacy schedule fails closed to disabled.
The schedule is optional; real month-pool recovery does not require waiting for that date.

A genuinely finished, error-free model response can clear a monthly block through
`quota.observe_model_success(provider, identity, started_at, completed_at)`: the bridge passes
the privately captured request credential identity and times, and the clear applies only when
the request started strictly after the block opened, completion is not earlier than the start,
all timestamps are positive and finite, and the identity still equals the provider's current
credential. A `credential_rejected` block is credential-specific in every mode and the same
current-key success may clear it; for a prepaid provider the proof may also clear a confirmed
`insufficient_balance` block. Other block reasons keep monthly-only behavior. It persists
`released: model_success` with the completion time as the watermark and preserves
message IDs and the credential binding. Pending, empty, failed, unattributed or older replies
never clear anything.

`status`, `wait` and `collect` retain routing history and show capacity waits. A confirmed
capacity stop with no valid route keeps the original task/session, authority and checkpoint
alive; recovery resumes the remaining goal exactly once. Lost dispatch acknowledgement is
observed, never replayed. Explicit pins remain on their profile, and an explicit Ultra floor
keeps its model requirement. Default Ultra capacity fallback stops at Qwen3.8 Max. Temporary
fallbacks may return to the preferred route only at an owned idle
checkpoint, after fresh guarded recovery/admission and a cooldown. Completed tasks are never
restarted simply to change their historical model.

Ark's signed account query uses a different credential from inference. Both captured identities
must remain current. Fresh official positive evidence from all four AFP windows authorizes a
bounded retry, not an inference-success claim; cash/auth/prepaid blocks are not released this
way. Kimi requires the verified real month pool above. These automatic evidence retries wait at
least five minutes after rejection, then back off through 10/20/40/60 minutes when new failures
persist. New rejections keep the counters, IDs and recovery watermark; stale in-flight queries
or old errors cannot reopen or clear newer state. Zero, unknown, expired, mismatched and rotated
readings fail closed. The Alibaba account aggregate cannot release an individual seat/key;
its independent pool owns actual-request probes and credential-generation checks.

For a future actual recovery, the narrow manual path is
`delegate-opencode quota --retry-provider PROVIDER`. It is labeled as a manual retry
authorization, not proof of recovery: it releases the block so new attempts may run until
a further billing error re-opens it, preserves seen message IDs and the `cleared_at`
watermark, and rejects unknown providers. It does not enforce a single attempt and does not
reinterpret telemetry or reset timestamps. Monthly classification requires exhaustion
wording (`reached`/`exhausted`/`refreshed in the next cycle`, and similar), so a 403 such as
"you do not have permission to view monthly quota" or a stated monthly allowance never
trips the circuit. Historical recovery guidance honors only `manual_retry`, `scheduled_reset`,
`model_success`, `snapshot_replenishment`, `openapi_account_recovery`,
`ark_official_afp_retry_authorization` and `kimi_verified_monthly_pool_retry_authorization` releases; a positive
window sample alone never proves recovery. `openapi_account_recovery` is Alibaba-only: the
official Model Studio `GetSubscriptionStats` account query returned a fresh, complete,
strictly positive and unexpired Credits reading whose request started after the circuit
opened, bound to unchanged inference and control credential identities; because it reports
whole-subscription Credits it may clear monthly exhaustion, a permission window telemetry
never grants other providers. In `prepaid` mode this aggregate path is disabled entirely so a
second seat can never mask a current-key exhaustion or auth rejection. `snapshot_replenishment`
is an imported manual console reading
(for example an Alibaba baseline before the first live reading): it must be positive, from its
still-current period, within its freshness horizon and newer than both the last import and the
block, so an expired-period, stale or out-of-order reading can never clear a depletion latch or
billing circuit. `usage-report` returns timestamped per-response token windows (1 h / 24 h /
48 h) with estimated Alibaba Credits; records without timestamped events are reported separately
as coarse lifetime counters and never contaminate the timestamped headline.

No automatic recharge or booster setting changes. Account-wide before/after quota is not
attributed as an exact task charge. `stats` reports observed mixed task latency, not a benchmark.

## Artifacts, recovery and acceptance

State: `~/.local/state/delegate-opencode` (owner-only). Each task has baseline metadata,
scoped before/after snapshots, a binary-capable `changes.patch`, redacted messages,
actual model IDs, tool execution evidence and a structured report. Results are untrusted
worker output until the upstream harness reviews them. Source files in artifacts can contain private code.

Auto Approve defaults to on for this Worker service, allowing all tools without prompts.
Task scope and read-only intent are cooperative instructions, verified through collected changes.
When disabled, native tool scopes and exact shell allowlists apply. Neither mode is an OS sandbox.
Do not run untrusted repositories/scripts with valuable credentials; choose a separate
execution environment for that situation. External edits outside this scheduler are not locked.

An accepted prompt's identity is saved before sending. Recovery observes that session;
it never blindly sends the prompt again. Cancellation retains ownership until abort is
confirmed. `uncertain` means the service cannot confirm state; restore service connectivity.
The queue reports tool failures without a repeated-failure or iteration cutoff; test failures
inside otherwise successful shell commands are additionally the worker's and upstream harness's
responsibility.

For isolated integration, the source must still match the task's original baseline for every
changed file. `integrate JOB_ID` performs both hash checks and `git apply --check`;
`--apply` applies the reviewed task delta. It does not commit, push or deploy.
After integration, run the appropriate tests and preserve evidence before releasing the exact
completed worktree. Unintegrated changes stay for review; do not leave an integrated or unchanged
managed worktree consuming disk indefinitely.

OpenCode 1.18.32 accepted a JSON-schema `format` request but returned HTTP 400 when reading
that saved message. This integration therefore requests a plain final JSON report in the
prompt and validates it itself. Missing or invalid reports become `needs_attention`.

## Deployment and service control

Deployment is a normal worker responsibility when included in the user's authorized outcome.
Give the worker the workspace, intended environment, expected result and the necessary
writable scopes or operational targets; use the same `--resource` for jobs deploying to
the same target, with stable `ssh:<host>:<service>` names rather than per-model locks. Let it discover routine
commands from project scripts, CI workflows and runbooks. Provide a method for special processes
only when needed. The worker owns build, deployment, routine troubleshooting and verification
of the running version and health, and returns concise evidence for the upstream harness's final review.
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
`opencode-ai@1.18.32` under the private runtime via the official npm registry. Without npm,
it downloads the matching official GitHub release and verifies the release API's SHA-256 digest.
No sudo or global package replacement is used. Provider login remains with OpenCode.

```sh
delegate-opencode steer JOB_ID 'Check this edge case before finishing.' --request-id investigation-2
delegate-opencode task retier JOB_ID --tier normal --reason 'Root cause and acceptance are now known; the remaining work is ordinary bounded implementation.' \
  --handoff-file /private/checkpoint.json --request-id implementation-phase
delegate-opencode task retier JOB_ID --tier fast --reason 'The identified fix has an explicit deadline tonight and its output is the ready, currently blocked input of the dependent packaging task.' \
  --handoff-file /private/checkpoint.json --request-id expedite-release-fix
delegate-opencode sessions --search parser
delegate-opencode session bind SESSION_ID --directory /absolute/project
delegate-opencode session delete SESSION_ID --yes
delegate-opencode task delete JOB_ID --yes
delegate-opencode task clear-finished --yes
delegate-opencode cleanup
delegate-opencode cleanup --apply
```

Steering persists intent before sending `prompt_async`, keeps the same model/permissions,
and delays normal task completion until a reply to the latest guidance is observed. HTTP
acceptance is not proof that the model followed the instruction. Unknown delivery is retained
and not automatically replayed. OpenCode 1.18.32 picks up the new user message in its running
session loop; it does not expose a separate delivery-mode field in its local OpenAPI schema.

`task retier` and authenticated `POST /console-api/task/{id}/retier` change the capability tier
without replacing the task or native session. The HTTP body is `{tier, reason, handoff, request_id}`;
the CLI reads `handoff` from an owner-only (`0600`) JSON file outside the repository. Its `done`, `remaining`, `decisions`,
`evidence` and `do_not_repeat` fields accept text or arrays of text. `remaining` explains the
concrete work still needed. See [the checkpoint example](handoff.md#same-task-tier-checkpoint).

Queued tasks apply the tier before dispatch. Running tasks queue a context-preserving continuation
at a safe boundary without aborting current tools or turns. `tier` reports the effective tier,
`requested_tier` the requested target, `tier_change` the latest request and `tier_history` its
audit trail. Request states are `pending`, `applied`, `rejected` or `uncertain`; only observed
continuation execution proves application, even when routing selects the same model. A pending or
uncertain request prevents another change. Identical request IDs with identical content are
idempotent; conflicting reuse is rejected and ambiguous sends are never blindly replayed.
Capability floors, explicit model pins, scopes and permissions remain binding; terminal or
cancelled tasks cannot be reclassified. A tier change never resets usage or marks a task done.

Workspace binding uses `/experimental/control-plane/move-session`, with `moveChanges: false`.
It changes the native session directory and preserves the task's original workspace/evidence.
New sessions and tasks accept an explicit workspace; named workspace shortcuts are local.

Permanent deletion in the session library requires typing the title. The CLI `--yes` path is
for a coordinator acting on an explicit user request. Neither path deletes active session
trees. Deleting a native session alone does not delete its bridge task row. `task delete` owns
the full bridge lifecycle: it writes the compact accounting ledger, removes the linked native
session and disposable evidence, then releases the managed terminal worktree. Active tasks are
always rejected. `task clear-completed` and `task clear-finished` provide the same confirmed
operation for explicit bulk cleanup. Task-list batch cleanup has its own confirmation: it writes the compact accounting
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

Existing sessions can switch between worktrees of the same Git project. OpenCode 1.18.32 rejects cross-project migration; create a new session bound to the target project instead.

## Durable project memory

When enabled, `opencode-mem` stores its vector database under the bridge's internal state directory
by default, even when a project checkout lives on an external volume. Set an absolute storage path
in Worker Desk when another internal directory is preferred. Git worktrees share the repository's
Git-common identity; non-Git projects use their stable path or an `.opencode-mem-project` marker, so
memory remains attached to the project rather than one disposable checkout.

Retention is capacity-based and independent from sessions. The default logical ceiling is 256 MB;
only after crossing it does maintenance remove exact duplicate unpinned records, then the oldest
unpinned records until usage is at or below 90%. Pinned records are never reclaimed. Session/task
cleanup and time spent powered off do not delete durable memory. `max_memories` is a separate safety
limit rather than the primary retention policy.

```sh
delegate-opencode memory status
delegate-opencode memory search --directory "$PWD" 'topic'
delegate-opencode memory list --directory "$PWD"
delegate-opencode memory add --directory "$PWD" 'verified durable fact'
delegate-opencode memory maintain
```


## Error bridge

`status JOB_ID` and `wait JOB_ID` include `errors` while a task is running or finished.
`collect JOB_ID` includes errors and pending questions without requiring `--full`.
Each error carries `source`, `code`, `message`, `retryable` (null when unknown), and
`suggested_action`, with tool/command/exit-code or HTTP metadata where available.
A model billing error also carries `billing: true` and `billing_reason`
(`insufficient_balance` or `monthly_usage_limit`). Only a model origin qualifies: transport
text, tool output containing the same phrase, ordinary HTTP 403 authorization failures and
429 rate limits never trip the durable billing circuit, but a model-origin 429 is a confirmed
capacity stop eligible for same-session routing continuation. An Alibaba model 401 additionally
carries `auth: true` and `auth_reason: credential_rejected`; that inference-key circuit is
created only for Alibaba, and a control-plane quota-query AK/SK rejection is never reported as
a model auth error. Historical compact billing errors inform
`recovery` read-only and never re-arm or clear a circuit by themselves.
Errors from earlier failed tool attempts can coexist with a successfully completed report.
A transport error after dispatch means the prompt may have been accepted: observe the
existing session, never blindly resubmit. The bridge reports information when queried;
The upstream harness should use bounded `wait` calls while awaiting a delegated result.

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
