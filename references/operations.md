# Operations

The entrypoint is `~/.local/bin/delegate-opencode`. The skill also works via
`python3 ~/.codex/skills/delegate-opencode/scripts/delegate.py` if the bin directory is not on PATH.

## Submit and retrieve

```sh
delegate-opencode doctor
delegate-opencode quota
delegate-opencode console --open
delegate-opencode submit --directory /absolute/repo --urgency background \
  --title 'Map configuration' 'Locate configuration loading and summarize precedence with file/line evidence.'
delegate-opencode submit --directory /absolute/repo --mode write --scope src/parser.py \
  --profile fast-code --command 'python3 -m unittest -v tests.test_parser' \
  --acceptance 'Preserve all existing behavior except the specified empty-input fix.' \
  'Fix the already-identified empty-input parser failure and run the authorized tests.'
delegate-opencode status
delegate-opencode wait JOB_ID --seconds 20
delegate-opencode collect JOB_ID
delegate-opencode cancel JOB_ID
delegate-opencode stats
```

For complex text, use `submit --spec /absolute/task.json` or `--spec -` with a JSON
object on stdin. Fields: `directory`, `objective`, `title`, `acceptance` (string array),
`profile` (`auto` or a configured profile), `mode` (`read`/`write`), `scopes` (literal relative
path array), `commands` (suggested checks; exact allowlist when Auto Approve is off), `resources` (shared lock-name array),
`urgency` (`fast`/`background`), `complexity` (`normal`/`deep`), `workspace`
(`auto`/`shared`/`isolated`), `large`, `web`, and `timeout_seconds`.
Do not put API keys or other secrets in tasks. Web fetching is opt-in with `web: true`.
Use `group_title` for a readable main-task name, `group_id` to override the current Codex
task ID, and `parent_task_id` for a child of an existing delegated task in the same group.

Prefer one coherent assignment that owns an outcome, such as investigation, bounded
implementation, checks and a concise report. Use `steer` for refinements to that outcome
instead of creating a sequence of microtasks. Split tasks when deliverables or writable
scopes are genuinely independent.

Select `profile` deliberately when the coordinator can judge the task's semantic fit.
Use `fast-code` for urgent, clearly bounded work; `senior-code` for longer independent
implementation and second opinions; and `deep-research` for large-repository mapping,
cross-module or ambiguous root causes, architecture/dependency synthesis and consequential
independent review. Every tier may investigate, edit and test a complete bounded task; the
profile names do not restrict job roles. `auto` is a coarse convenience based only on
`urgency` and `complexity`; it does not infer those semantic properties from task text.
Deep criteria take precedence over a small final edit surface: broad repository reading,
multiple subsystems, architecture/dependency mapping, an ambiguous root cause or a
consequential challenge review should use `deep-research --complexity deep`. Do not split a
coherent deep investigation into several fast lookups to avoid waiting. Periodically use
`stats` to detect a persistently unused tier when qualifying work exists, then correct future
profile choices without manufacturing tasks to meet a quota.

## Local console

`console --open` opens the URL saved in `console_url` in the pool configuration. The page
shows ownership groups, nested children, execution status, model, duration, summaries,
DeepSeek balance and Kimi quota windows. It supports search, status filtering, details and
coalesced quota refresh. Click a task title to open its actual OpenCode web session.

The console binds to loopback, checks Host/Origin, and bootstraps an HttpOnly SameSite
cookie on local top-level navigation. Its OpenCode gateway supplies server authentication
in the backend; API keys and the server password never enter browser data. The gateway
supports OpenCode streaming and WebSocket traffic. A direct manual continuation inside
OpenCode is outside the pool's task ledger: submit follow-up work through Astra when file
ownership and state tracking must remain coordinated.

Global settings: `~/.config/opencode/delegate-pool.json`. This is separate from the
user's interactive OpenCode configuration. Current defaults: 3 total workers, at most
1 Kimi worker, 80 agent steps; 30-minute tasks or 60 minutes for deep tasks.
Model/variant changes saved in the console restart idle execution services; queue limits are read at runtime
(changing total thread capacity also requires restarting the queue).

## Quota and routing

The adapter reads existing OpenCode API credentials in-process and calls only the fixed
official HTTPS endpoints, without redirects. No credentials are returned to Astra.
DeepSeek currency balances remain separate. Kimi duration, remaining and reset fields are
preserved; Kimi profiles share one account pool. Supplemental `used_ratio` units and booster
wallet payment behavior are not guessed or changed. Unknown values remain unknown.

Queries are coalesced for 60 seconds, refreshed every 5 minutes while running or every
minute while waiting. Idle queues do not query. Transient errors preserve last successful
data for 15 minutes with a stale marker; older data is unknown and concurrency is limited
to one per affected provider. Invalid credentials or known exhausted quota block dispatch.
When any valid Kimi window is below 20%, ordinary auto tasks may use the other configured
provider; deep tasks reserve Kimi availability. Explicit profiles never silently change.
Queue status records the route/fallback reason. Unknown quota is not a promise of availability.

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
it never blindly sends the prompt again. Cancel/timeout retains ownership until abort is
confirmed. `uncertain` means the service cannot confirm state; restore service connectivity.
The queue can report and stop on repeated tool failures, but test failures inside otherwise
successful shell commands are additionally the worker's and Astra's responsibility.

For isolated integration, the source must still match the task's original baseline for every
changed file. `integrate JOB_ID` performs both hash checks and `git apply --check`;
`--apply` applies the reviewed task delta. It does not commit, push or deploy.
After integration, run the appropriate tests and preserve evidence before manually removing
the exact completed worktree with ordinary `git worktree remove`.

OpenCode 1.18.30 accepted a JSON-schema `format` request but returned HTTP 400 when reading
that saved message. This integration therefore requests a plain final JSON report in the
prompt and validates it itself. Missing or invalid reports become `needs_attention`.

## Deployment and service control

`python3 scripts/install.py` installs or updates the global skill, launcher and three local
background services. Existing deployment is backed up under `state/releases`; credentials,
settings and results are preserved. Update only when no tasks are active.

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

Permanent deletion in the browser requires typing the title. The CLI `--yes` path is for a
coordinator acting on an explicit user request. Neither path deletes active session trees.
Worker task records, summaries, reports and patches remain after native conversation deletion.

Cleanup is opt-in in a fresh public installation. It defaults to a 5 GiB trigger, 10 GiB target,
30-day age floor and retention of the latest 20 tasks. Only owned runtime data and pool-linked,
old archived sessions are eligible. Worktrees, final reports, patches and task records remain.
A parent archive with any ineligible descendant is retained. Removed file byte totals are
estimates, not exact disk-space attribution. Cleanup produces a private `cleanup-last.json`.

Existing sessions can switch between worktrees of the same Git project. OpenCode 1.18.30 rejects cross-project migration; create a new session bound to the target project instead.


## Error bridge

`status JOB_ID` and `wait JOB_ID` include `errors` while a task is running or finished.
`collect JOB_ID` includes errors and pending questions without requiring `--full`.
Each error carries `source`, `code`, `message`, `retryable` (null when unknown), and
`suggested_action`, with tool/command/exit-code or HTTP metadata where available.
Errors from earlier failed tool attempts can coexist with a successfully completed report.
A transport error after dispatch means the prompt may have been accepted: observe the
existing session, never blindly resubmit. The bridge reports information when queried;
Codex should use bounded `wait` calls while awaiting a delegated result.

`wait --seconds N` is a bounded observation call and does not define how long the worker
should take. Fifteen to thirty minutes is normal for coherent implementation, compilation,
tests or deep investigation; configured deep tasks may run longer. A nonterminal response
contains `terminal: false`, `continue_waiting: true` and `next_action: call_wait_again`.
When the requested result depends on that worker, the coordinator must call `wait` again in
the same turn rather than ending with a progress-only response or asking the user to send
“continue”. Provider capacity, several quiet waits or the coordinator's desire to finish its
turn are not cancellation reasons. Cancel only when the user requests it, the scope is
confirmed wrong or unsafe, the objective is superseded, or a terminal condition requires it.


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
