# Self-contained handoff contract

Use this contract when an OpenCode worker cannot infer the whole assignment from one short
sentence: broad implementation, continuing work, a multi-stage repair, deployment, release,
migration, CI investigation, or any task whose essential context lives in the upstream chat.

## Cold-start rule

The worker does not see the upstream conversation. Before submitting, ask:

> Could a capable engineer finish this outcome without asking what was already decided or
> repeating work whose result is already known?

If not, the handoff is incomplete. Repository facts may be rediscovered from the workspace;
conversation-only facts must be handed over explicitly.

## Required information

Include the parts that apply:

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

Do not include credentials, tokens, passwords, a raw conversation transcript, irrelevant history,
or a detailed recipe the worker can discover more reliably from the repository. There is no fixed
word limit: be concise for simple work and lossless for substantial work.

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
  "objective": "Outcome:\n...\n\nCurrent state:\n...\n\nSettled decisions:\n...\n\nEvidence and artifacts:\n...\n\nConstraints and authority:\n...\n\nOpen questions:\n...\n\nDo not repeat:\n...",
  "acceptance": [
    "The intended behavior is implemented in the authorized scope.",
    "Relevant checks pass and exact results are reported.",
    "Remaining UI, device, production, or user-only gates are identified precisely."
  ]
}
```

The task stays one coherent outcome. A large implementation with a clear direction may still be
Fast; handoff size and runtime do not determine tier. Split only when deliverables and writable
scopes are genuinely independent.

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

Do not repeat:
<completed or uncertain side effects; inspect before acting>
```

Read the existing workspace and transcript before continuing. A same-session automatic fallback
already receives this protection from the bridge; do not submit a duplicate task while its status
is uncertain.

## Long operations and UI work

For builds, tests, deployments, migrations and CI expected to exceed three minutes, request phase
reports. Provide a GitHub Actions run URL when one exists. Percentage and ETA require a real item or
step count; otherwise phase-only indeterminate progress is correct. Ordinary tasks without an
explicit progress record must not display a build or CI indicator.

For interface work, hand over observed UI facts, target behavior, screenshots or paths, responsive
requirements and acceptance criteria. The worker may implement and run terminal/headless checks;
the upstream harness performs nuanced visual judgment or Computer Use acceptance when required.
