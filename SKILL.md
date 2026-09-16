---
name: delegate-opencode
description: Delegate bounded general-purpose work to a local OpenCode worker pool, including file and repository investigation, summaries, analysis, coding, debugging, documentation, testing and independent review. Use to parallelize useful work or keep bulk reading out of Astra's context. Astra retains UI aesthetics, interaction and product decisions, architecture, and final acceptance.
---

# Delegate OpenCode work

Use this installed skill's `scripts/delegate.py` with Python 3, or
`~/.local/bin/delegate-opencode`. Run `service start` before `doctor` if services are unavailable. Missing OpenCode is installed automatically from the official pinned npm package (or a checksum-verified official release when npm is unavailable). Existing credentials and configuration are preserved. Use `quota` when resource
availability matters. Runtime is a local authenticated service; tasks persist across turns.
`console --open` starts services as needed and opens the local task/usage dashboard.

## Decide and delegate

- All three profiles are general-purpose. Prefer `senior-code` for suitable independent
  background work, `fast-code` for quick feedback and the critical path, and
  `deep-research` for deep reasoning or large, cross-module investigation. These names
  identify resource tiers, not capability limits. `auto` implements this initial routing;
  explicit profiles remain explicit. Actual models live in configuration, not this skill.
- Delegate bulk reading before loading a repository into Astra's context. Request concise,
  evidence-backed findings and retrieve only the relevant original files or detailed results.
- Keep UI aesthetics, key interaction implementation, product meaning, difficult decisions,
  and final acceptance with Astra. Workers can implement specified components and data wiring.
- Supply a concrete objective, context, acceptance conditions, literal writable file/directory
  scopes, and useful test/build commands. Read-only is the default. Workers can investigate,
  reason and implement independently within the task, but cannot expand scope or spawn workers.
- Pass `--group-title` with the user's main task title. The current Codex task ID is captured
  automatically as its group; use `--parent-task-id` when a delegated task follows another.
  This keeps main-task ownership and parent/child relationships visible in the console.
- Normal reversible work already authorized by the user needs no additional confirmation.
  Delegation does not expand authorization for publishing, deployment, account changes or devices.

## Work together

- Shared workspace is the default. Non-overlapping writable scopes can run concurrently;
  overlaps queue. Astra must also avoid files owned by active tasks (`status`).
- Use `--large` or `--workspace isolated` for cross-module refactors, shared interface changes,
  dependency upgrades or uncertain write scope. Isolated tasks require a Git repository root;
  current tracked changes and unignored untracked files are copied into the baseline.
- Use matching `--resource` values for shared build directories, databases, simulators or
  other resources that must not run concurrently. These are cooperative locks, not a sandbox.
- Worker Auto Approve is on by default: tools run without permission prompts. Scope and
  read-only instructions remain binding and require final diff review; they are not enforced
  by a sandbox. Commands are suggested checks. If Auto Approve is disabled in settings,
  native edits use declared scopes and shell calls require exact supplied commands.
- Check progress with `status`, use bounded `wait --seconds 30`, and collect concise results.
  Full outputs stay in protected task artifacts (`collect --full` only when needed).
- `status`, `wait` and `collect` return structured `errors` with source, message, tool/command,
  exit code or HTTP status, retryability and suggested action. `collect` also returns pending
  questions/permissions. Inspect these before choosing to guide, fix or resubmit. Do not
  replay a prompt whose acceptance is uncertain; observe its existing session. An error
  from an earlier tool attempt may remain even after the worker recovers successfully.
- A Worker `completed` state is not final acceptance. Inspect critical evidence and diff,
  confirm tests, and inspect actual rendered UI where relevant. Never infer a device outcome
  from commands or test results. A failed test reported in text still needs Astra's review.
- For an isolated task, review `changes.patch`, run `integrate ID` to check applicability,
  then `integrate ID --apply` only after review. Verify the integrated code. Artifacts and
  worktrees are retained for review; no automatic destructive cleanup.
- On `needs_attention`, fix the scope/context or reassign with a new bounded task. On
  `uncertain`, ownership remains held: inspect/recover the service or request `cancel` and
  wait for confirmed cancellation. Never submit a duplicate merely because a request timed out.

Read [references/operations.md](references/operations.md) for CLI examples, quota behavior,
service maintenance and task artifact semantics.

## Guide and manage sessions

- `steer JOB_ID 'guidance' --request-id STABLE_ID` sends guidance into the running
  OpenCode session without expanding file or shell permissions. It is read at a subsequent
  model-step boundary, not a guaranteed interruption of a tool. Check returned delivery
  status; ambiguous requests must not be blindly repeated with a new ID.
- `sessions --search TEXT` lists native sessions across projects. `session rename ID --title TEXT`,
  `session archive ID`, `session restore ID`, and `session fork ID` manage them.
- `session bind ID --directory /absolute/project` uses OpenCode's native migration with
  file transfer disabled. Busy sessions cannot move. The original task evidence stays bound
  to its original execution directory.
- When the user explicitly requests deletion, `session delete ID --yes` is the authorized
  agent interface; no browser confirmation is needed. Do not delete unrelated sessions.
  Active sessions and active descendants are refused; retained worker evidence is separate.
- `cleanup` previews. `cleanup --apply` follows the enabled low-space policy; `--force`
  additionally bypasses the low-space/enabled gate only for explicitly requested cleanup.
  Age and recent-task retention still apply. The daemon checks enabled cleanup periodically.
  Defaults retain 30 days and the latest 20 tasks, never removing worktrees or final patches/reports.

Existing sessions can switch between worktrees of the same Git project. OpenCode 1.18.30 rejects cross-project migration; create a new session bound to the target project instead.
