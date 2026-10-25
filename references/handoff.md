# Self-contained handoff contract

Match handoff detail to the work. A simple, self-contained task needs a short objective,
necessary context and observable acceptance; it does not require every heading below. For example,
when a parser's exact empty-input failure and expected behavior are already known, pass those facts,
the relevant file/test entry points and constraints, then let the worker implement and verify it.
Do not spend more time preparing a small handoff than executing the work would require.

Use the structured template for broad implementation, continuing work, a multi-stage repair,
deployment, release, migration, CI investigation, or work whose essential context lives in the
upstream chat. Share what is already known; leave unknown repository facts to the worker's
investigation. Missing local implementation details do not justify completing discovery upstream
before dispatch. A meaningful investigation is itself a delegable package.

## Cold-start rule

The worker does not see the upstream conversation. Before submitting, ask:

> Could a capable engineer finish this outcome without asking what was already decided or
> repeating work whose result is already known?

If not, the handoff is incomplete. Pass relevant findings already known to the upstream harness,
including repository facts and conversation-only context. The worker investigates remaining
unknowns and verifies current state; the harness need not read the whole repository first.

## Information for substantial handoffs

Include the parts that apply; these headings are a writing aid, not extra required API fields:

1. **Outcome** — the observable end state, not a list of keystrokes.
2. **Current state** — what is already implemented, running, deployed, broken, or unverified.
3. **Settled decisions** — conclusions that constrain the solution and a short practical reason
   when the reason prevents the worker from undoing the decision. Do not include private chain of
   thought.
4. **Evidence and artifacts** — exact paths, commit/build/run/task IDs, URLs, error text, test
   results, screenshots described as observations, and relevant timestamps.
5. **Authority and constraints** — writable scopes or operational targets, environment, user
   authorization, compatibility or safety limits, and other tasks whose work must be preserved.
6. **Acceptance** — concrete evidence that proves the outcome, including separate build,
   deployment, rendered-UI, device, or production gates where relevant.
7. **Open questions** — only genuine unresolved points the worker must investigate or return.
8. **Do not repeat** — completed edits, deployments, messages, destructive actions, purchases, or
   other side effects that a continuation must inspect rather than replay.
9. **Dependencies** — verified prerequisites, interfaces or outputs consumed from other work
   packages, and the specific result handed back for downstream integration.
10. **Suggested approach** — when a viable direction is known, include promising files or entry
    points, the intended path and important pitfalls. Label hypotheses and recommendations separately
    from verified facts or settled requirements; the worker may adjust methods based on evidence.

Do not include credentials, tokens, passwords, a raw conversation transcript or irrelevant history.
Useful direction is not an exhaustive command recipe: preserve the worker's execution autonomy.
There is no fixed word limit: be concise for simple work and lossless for substantial work.

## Private spec template

Use a private owner-readable JSON file and put the structured handoff in `objective`:

```json
{
  "directory": "/absolute/repository",
  "title": "Deliver the complete outcome",
  "group_title": "Parent outcome",
  "tier": "normal",
  "profile": "auto",
  "mode": "write",
  "scopes": ["relevant/subsystem"],
  "objective": "Outcome:\n...\n\nCurrent state:\n...\n\nSettled decisions:\n...\n\nEvidence and artifacts:\n...\n\nDependencies and output locations:\n...\n\nSuggested approach (when known):\n...\n\nConstraints and authority:\n...\n\nOpen questions:\n...\n\nDo not repeat:\n...",
  "acceptance": [
    "The intended behavior is implemented in the authorized scope.",
    "Relevant checks pass and exact results are reported.",
    "Remaining UI, device, production, or user-only gates are identified precisely."
  ]
}
```

Each task owns a coherent work package. Split a large goal at useful decision or delivery
boundaries, including sequential stages with dependencies; concurrent writers need independent
scopes or resource serialization. A large implementation with a clear direction still defaults
to Normal. Requests to improve efficiency call for better workflow and handoffs, not an automatic
Fast selection; apply the model-latency criteria below. Do not retain Deep or Ultra simply
because the investigation or parent project required it.

The ordinary workspace is the already bound shared/main checkout. Do not request or create a Git
worktree merely because a task is large, important, long-running, or changes code. Use an isolated
workspace only for a user-requested checkout, unavoidable concurrent conflicting writes, or a
destructive experiment. Prefer scope/resource serialization because a second checkout can duplicate
dependencies and build products. If isolation is necessary, state why and make integration plus safe
worktree release part of completion; preserve unintegrated work for review.

## Continuation template

For recovery, fallback, or a deliberate follow-up linked by `parent_task_id`, use:

```text
Outcome:
<unchanged end state>

Done:
<verified edits, findings, checks and side effects>

In progress:
<partial files, commands, sessions, builds or deployments and their exact state>

Remaining:
<work still needed and evidence still missing>

Settled decisions:
<choices and constraints that remain binding>

Evidence and dependencies:
<verified results, artifact locations and prerequisite outputs for the next stage>

Suggested approach (when known):
<recommended direction, useful entry points and pitfalls; label unverified hypotheses>

Do not repeat:
<completed or uncertain side effects; inspect before acting>
```

Read the existing workspace and transcript before continuing. A same-session automatic fallback
already receives this protection from the bridge; do not submit a duplicate task while its status
is uncertain.

## Same-task tier checkpoint

When the outcome is unchanged but the remaining reasoning needs or the concrete latency value
have changed, keep the same task and use `task retier`, not cancellation or a new task. Save the
checkpoint outside the repository
with owner-only permissions (`0600`). Example private JSON checkpoint:

```json
{
  "done": ["Confirmed the root cause and reviewed the proposed interface."],
  "remaining": ["Implement the agreed validation rule and run the focused regression checks."],
  "decisions": ["Keep the existing public response format."],
  "evidence": ["Use the verified investigation artifacts linked in the current task."],
  "do_not_repeat": ["Do not redo the completed investigation or repeat external side effects."]
}
```

```sh
delegate-opencode task retier JOB_ID --tier normal \
  --reason 'Root cause and acceptance rule are now known; the remaining work is ordinary bounded implementation.' \
  --handoff-file /private/checkpoint.json --request-id validation-implementation
```

Use the same mechanism for a justified latency move: direction and acceptance are clear,
remaining model-driven work is the current bottleneck, and earlier output will advance a named
near-term milestone or explicit deadline. A generic efficiency request, routine dependency or
resource conflict is insufficient. Describe the actual bottleneck and expected benefit in the
existing `objective` and retier reason; no extra spec fields are needed:

```sh
delegate-opencode task retier JOB_ID --tier fast \
  --reason 'Packaging inputs and resources are ready; generating the bounded regression fix is the current blocker for the release deadline today. Earlier model output lets packaging start sooner.' \
  --handoff-file /private/checkpoint.json --request-id expedite-release-fix
```

Return to Normal once the latency value is gone and meaningful work remains:

```sh
delegate-opencode task retier JOB_ID --tier normal \
  --reason 'The expedited phase is complete; the remaining cleanup has no dependent waiting on it.' \
  --handoff-file /private/checkpoint.json --request-id back-to-normal
```

A private checkpoint must contain actual evidence and
remaining work, not the illustrative claims above. The bridge preserves the session and switches
at a safe continuation boundary; watch `tier_change.status`, not merely HTTP acceptance. While
pending or acknowledgement is uncertain, keep observing the same job and do not submit another
replacement. For genuinely different downstream deliverables, create linked work packages instead.
Carry the same context across tier changes: put the current direction and dependencies in
`remaining`, settled choices in `decisions`, and artifact locations in `evidence`; no additional
checkpoint keys are needed.

## Long operations and UI work

For builds, tests, deployments, migrations and CI expected to exceed three minutes, request phase
reports. Provide a GitHub Actions run URL when one exists. Percentage and ETA require a real item or
step count; otherwise phase-only indeterminate progress is correct. Ordinary tasks without an
explicit progress record must not display a build or CI indicator.

For interface work, pass the observed UI state, target behavior, available automation tools and
acceptance evidence. Workers may implement, run terminal/headless checks and perform lightweight
interaction through tools actually available to them. Lightweight means a clear goal, stable path,
observable result and recoverable failure; it is not a fixed click count. Do not assume a harness
connector, desktop session or Computer Use tool exists in the worker. Use the
[interface operation boundary](operations.md#interface-operation-boundary) for allocation.

For a mixed task, name the observable handoff state between the worker and the upstream harness.
The worker can finish supported implementation, build/deployment and verification while Codex
handles complex device/cross-application interaction and visual/experience judgment. If tools are
missing or a light flow becomes ambiguous, hand over completed actions, current state, available
screenshots or observations, artifact paths, remaining goal and any uncertain side effect. Keep
verified outputs and do not repeat the whole assignment or replay an uncertain action.
