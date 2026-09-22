# Routing and plan efficiency

[简体中文](routing.zh-CN.md)

Prefer complete outcomes, usable subscription allowance and enough model/context capacity.
Subscriptions are already paid for; direct DeepSeek balance is incremental spend. Do not split a
coherent task or replay its context merely to change providers.

## Generic routing patterns

Worker Desk does not require any particular provider or number of plans. Configure profiles first,
then choose one of these patterns in **Models & routing**:

- **One model per tier:** the simple `routing` map selects one profile for fast, background and deep work.
- **Primary + backup:** put each profile in its own ordered stage. The next stage is considered only when every model in the earlier stage is unavailable.
- **Fixed weighted pool:** put two or more profiles in one stage and assign integer weights. The weights stay fixed.
- **Quota-adaptive pool:** explicitly enable a bounded continuous curve for one two-provider stage. Its ratios are control points, the saved weight is the neutral anchor, and fresh quota runway interpolates between them. Missing or stale telemetry keeps the baseline.

Ordered fallback and weighted sharing can be mixed independently for each task tier. A user with
one subscription needs no policy at all; a user with one paid plan and one pay-as-you-go backup can
use two one-model stages. The Ark/Kimi policy below is an optional example, not a platform default.

Tier selection is based on unresolved ambiguity, not workload size or a weak-to-strong model
ranking. Fast and Normal are both expected to complete work correctly. Fast can own a complete
feature, fix, test, documentation change, migration or deployment when its direction, material
constraints and acceptance are clear; it may choose ordinary implementation details. Normal is
for a concrete outcome whose execution still needs open investigation or synthesis, even when it reads a large repository, changes
many files, runs for a long time, or includes implementation, tests, documentation, packaging
and deployment. Deep is for an
abstract or unclear objective, an unresolved system-wide root cause, architecture trade-offs or
logic with unusually difficult invariants. Large context and task importance alone are not Deep
criteria. There is no separate total-task cap for work that genuinely needs Deep.

Choose the lowest tier that covers the ambiguity the worker must remove. A firm direction,
governing constraints and acceptance are Fast; the method need not be prescribed line by line.
A clear outcome whose execution still requires open investigation, classification or several
interdependent choices is Normal. Defining the goal or rules, reconciling unclear requirements,
designing a new system structure, finding an unknown system-wide cause or solving unusually
complex logic is Deep. The task's label does not decide the tier: an explicit document move is
Fast, while sorting mixed evidence into keep, merge and delete decisions is Normal.

A practical Fast gate is: the outcome, direction or governing constraints, and observable
completion evidence are known before dispatch. Difficult planning performed earlier does not
make the remaining implementation Normal. Fast may discover local details; use Normal when the
execution still needs open investigation or significant interdependent judgments.

Quota may break a genuine Fast/Normal tie, never redefine capability. Read
`delegate-opencode quota --tier-guidance --compact` once before a coherent batch; it returns
posture, conservation level, confidence and next-refill labels without the full provider dump
(use verbose `quota --tier-guidance` for operator investigation). The primary runway signal is
observed working pace: each window's remaining allowance percent is divided by its fitted active
burn rate to give remaining working hours, then compared with wall-clock time to the window
reset. The legacy `remaining fraction / time-fraction-left` ratio survives only as the cold-start
prior before burn samples exist — with no telemetry, 20% allowance and 20% of the window left is
on pace (`1.0x`), while 20% with 40% left is constrained (`0.5x`). Idle gaps are excluded from
the working rate so an idle-heavy sample cannot look cheap. Within one provider the tightest
window is its bottleneck; conservation pressure across subscription providers is the MAX of
their bottleneck runways only when every configured subscription has a reliable signal. Missing
readings make that combined pressure unknown, not zero. This differs from the console's descriptive combined-pool fit. A near effective
refill can soften the guard-rail thresholds by up to 30%; a full refill-safe bypass is a
separate, stricter condition: the refill arrives within two hours and every currently available
provider reaches it on its own observed-burn forecast — simultaneous wall-clock runtimes are
never summed.
When the Fast and Normal routes use the same constrained plan and no healthier Normal-plan peer
is available, direction-fixed work should lean Fast. When Ark is constrained but Kimi still has
healthy runway, retain eligible Normal work so it can use Kimi instead of putting more load on
the Ark-only Fast stage. The bridge exposes a `quota_posture` label: `fast_preferred` when
constrained combined runway should break a tie toward Fast, `normal_flexible` when a healthy
Normal-only plan allows either tier, and `neutral` when quota has no preference. Under
`normal_flexible`, weigh error and rework risk, urgency and task size; Normal is available but
not mandatory.

An optional `quota_spillover` rule fills the gap between healthy weighted routing and total
fallback. Level 1 adds a later pay-as-you-go profile to the first Fast or Normal stage at a
gradually increasing share. Level 2 uses its own faster continuous curve and temporarily replaces
automatic Normal's source stage with the first available Fast source stage. Deep and explicit
profiles never change. The Agent Plan preset uses 38% and 25% baselines with a 33% Level 1 cap
and a 70% Level 2 high-pressure cap while fresh DeepSeek CNY balance remains at least 30.
Off-peak traffic may use that cap throughout Level 2. At peak prices the cap starts at 33% and
rises continuously to 70% only when the source-plan runway falls from the Level 2 threshold to
half that threshold. These are adaptive guard rails, not fixed switch points: the combined work pool is
fitted from observed working-pace burn, with a bounded demand adjustment for the admitted
workload. A near effective refill can soften both thresholds by up to 30%; the separate
refill-safe bypass requires the refill within two hours and every currently available provider
reaching it independently. Protection engages immediately on a reliable low-pressure sample;
recovery is progressive with a five-percentage-point exit margin and five-minute hysteresis so
a brief improvement does not immediately end protection. Hysteresis acts only on reliable signals; unknown data
suppresses new conservation action but does not guarantee the previous level is held. Unknown
calendar, balance or quota telemetry uses the lower cap and never invents a refill. Beijing
weekdays are peak only from 09:00-12:00 and 14:00-18:00; weekends remain off-peak even on
official make-up workdays. A configurable, locally cached subscription identifies weekday public
holidays.

The console's total-work-pool meter treats a pay-as-you-go balance increase as a new observation
epoch. It never nets post-top-up balance against pre-top-up samples. When both observed balance
burn and official token pricing are available, the higher burn rate bounds displayed endurance;
one idle-heavy signal cannot lengthen the estimate. Routing conservation itself still excludes
pay-as-you-go balance and follows subscription runway only.

Budget guidance is provider-neutral. In **Models & routing**, each configured provider can use
live telemetry when available, a manually entered rolling window (remaining percentage, duration
and an explicit-zone reset time), a monetary balance threshold for pay-as-you-go accounts, or
`ignore`. A manual current balance may stand in for a missing balance API. The configurable Fast
tie-breaker runway threshold applies to live and manual windows; monetary providers use their own
low-balance threshold. Unknown signals preserve ordinary routing instead of pretending the budget
is full or empty.
Do not restart productive work merely to change tiers and replay its context.

## Capability floor (optional)

A task spec may set `capability_floor` (`fast`/`normal`/`deep`) with a required, nonempty
`capability_reason` (1-500 characters). The floor must not exceed the requested tier; for a
`normal` floor only profiles in the Normal/Deep first stages are eligible, and for a `deep`
floor only the Deep first stage. Fallback, spillover and in-session rerouting never select a
profile below the floor — if none is available the task queues with
`capability_floor_unavailable` rather than downgrading. Parent-task continuations inherit the
floor. This is a deliberate no-downgrade requirement (for example a task whose correctness
cannot be served by a lighter route regardless of quota pressure), not a routine tier upgrade;
ordinary tasks omit both fields.

The stored shape is deliberately provider-neutral:

```json
{
  "routing_policy": {
    "background": [
      [{"profile": "plan-a", "weight": 2}, {"profile": "plan-b", "weight": 1}],
      [{"profile": "backup", "weight": 1}]
    ]
  },
  "routing_dynamics": {
    "background": {
      "0": {"ladder": [[3, 1], [2, 1], [1, 1]]}
    }
  },
  "quota_spillover": {
    "enabled": true,
    "profile": "backup",
    "tiers": ["fast", "background"],
    "max_share_percent": 33,
    "level2_runway_percent": 25,
    "level2_offpeak_share_percent": 70,
    "level2_min_balance_cny": 30
  }
}
```

Adaptive records are opt-in and tied to a stage index. Ordinary weights never become dynamic just
because they happen to be `1:1` or `2:1`.
`level2_offpeak_share_percent` is retained as the compatible configuration key; it now represents
the Level 2 high-pressure ceiling used immediately off-peak and reached progressively at peak.

## Ark Agent Plan

Connect `volcengine-agent-plan` through OpenCode's private auth store. Never paste a credential
into a task, tracked config, command argument or report. Worker Desk supplies this Responses API
provider when a matching profile is enabled:

| Profile example | Model | Context / output | Reasoning |
| --- | --- | --- | --- |
| `ark-auto` | `volcengine-agent-plan/ark-code-latest` | 1,024,000 client ceiling / 32,000 | `max` |
| `ark-evolving` | `volcengine-agent-plan/doubao-seed-evolving` | 1,024,000 / 65,536 | `max` |
| `ark-k3` | `volcengine-agent-plan/kimi-k3` | 1,024,000 / 65,536 | `max` |
| manual only | `volcengine-agent-plan/deepseek-v4.1-flash` | 1,024,000 / 65,536 | `max` |

The SDK is `@ai-sdk/openai` and the subscription base URL is
`https://ark.cn-beijing.volces.com/api/plan/v3`. Provider keys stay in OpenCode's private
authentication store; the generated runtime overlay contains no key. `ark-code-latest` follows
Ark's console selection and can route unpredictably, so use fixed Evolving or K3 when model
identity matters. The 1,024,000 client ceiling prevents local compaction at 256K; the selected
backend remains authoritative.

AFP telemetry is separate. `GetAFPUsage` and `GetPersonalPlan` use a Volcengine control-plane
AccessKey ID/Secret. Register the two metadata-only references described in
[credentials](credentials.md). Worker Desk signs the fixed Ark OpenAPI requests in-process and
returns only plan/window metadata. Missing telemetry leaves quota unknown without breaking
inference. The four windows share one provider runway, and all Ark profiles consume it.

Sources: [OpenCode integration](https://www.volcengine.com/docs/82379/2373741),
[plan overview](https://www.volcengine.com/docs/82379/2366394),
[GetAFPUsage](https://www.volcengine.com/docs/82379/2479847),
[GetPersonalPlan](https://www.volcengine.com/docs/82379/2546382).

## Optional Ark/Kimi subscription-first preset

A fresh `--preset ark-agent-plan` installation creates:

```json
{
  "routing_policy": {
    "fast": [
      [{"profile": "ark-auto", "weight": 1}],
      [{"profile": "fallback", "weight": 1}]
    ],
    "background": [
      [{"profile": "senior-code", "weight": 1}, {"profile": "ark-evolving", "weight": 1}],
      [{"profile": "ark-auto", "weight": 1}],
      [{"profile": "fallback", "weight": 1}]
    ],
    "deep": [
      [{"profile": "deep-research", "weight": 2}, {"profile": "ark-k3", "weight": 1}],
      [{"profile": "senior-code", "weight": 1}, {"profile": "ark-evolving", "weight": 1}],
      [{"profile": "ark-auto", "weight": 1}],
      [{"profile": "fallback", "weight": 1}]
    ]
  }
}
```

Only the first stage with an available candidate participates. Coordinators submit a tier with
`profile=auto`; explicit profiles are reserved for user-required model pins and controlled tests.
Weighted admission is durable and counts admitted jobs, not tokens or money. Waiting for owner
capacity, scope locks or provider recovery consumes no turn.

The preset also defaults to a 5% Kimi weekly/overall guard with one global native-K3 slot.
At or below that threshold, automatic Normal work excludes Kimi; automatic Deep work may use
one native K3 job while other jobs select Ark K3 or later stages. The bridge prefers the valid
`overall` aggregate and otherwise an exact seven-day window, never the five-hour window. Both
the threshold and slot count are editable under **Models & routing**.

If automatic routing reaches the configured `fallback` after skipping at least one preferred
stage, task status exposes `fallback_used: true` and a routing notice. Ordinary model choices are
silent; coordinators tell the user only when this final fallback is actually used.

The baseline favors native Kimi because buying the same Kimi model through Ark is usually a poor
economic trade. Each pool has its own continuous, reset-aware curve:

| Pool | Baseline | Allowed range |
| --- | --- | --- |
| native Kimi K3 : Ark K3 | 2:1 | continuously from 3:1 through 2:1 to 1:1; Ark K3 never leads |
| native Kimi K2.8 : Ark Evolving | 1:1 | continuously from 2:1 through 1:1 to 1:2 |

Runway is estimated from observed working-pace burn: remaining allowance hours at the fitted
active burn rate divided by wall-clock time to the reset. The tightest of a provider's windows
is its bottleneck; conservation pressure across subscription providers is the MAX of their
bottleneck runways when all configured subscription signals are reliable; otherwise combined
pressure is unknown. The console shows a separate descriptive combined-pool fit. The old `remaining fraction / time fraction
left` ratio is only the cold-start prior before burn samples exist. Stale, missing or
unauthenticated telemetry is excluded from the combined signal and keeps the baseline ratio. A 2x runway difference produces an intermediate ratio; a 4x
difference reaches the configured outer bound. The Fast tier's fallback share, level 1
conservation and level 2 conservation use separate curves. When level 2 reuses a
multi-provider Fast pool, it also inherits that pool's own curve. Ark Auto, Evolving and K3
share one runway, so heavy Auto use naturally reduces later Ark share. Endpoint-confirmed zero
removes the provider. A confirmed quota/window stop or model-origin HTTP 429 can continue
within the same OpenCode session on the next route. The transition keeps the transcript and
workspace, adds the failed provider to the exclusion set, and instructs the new model not to
repeat completed or external side effects.

When conservation is active, an optional long-task rule can also queue one same-session model
change after the configured age for automatically routed Fast or Normal tasks. This waits for the
current model turn to finish and never aborts it. Deep tasks and explicit profile pins remain
stable. The task keeps its ID, workspace and transcript; route history records
`queued_boundary_switch`, and the continuation must inspect existing work before acting. The
console can send Bark notifications for conservation changes, provider exhaustion/recovery and
these model transitions. Ordinary bridge-owned changes are silent to the coordinator and remain
in route history for diagnostics; only final fallback or an inability to continue is surfaced.
Notifications observe routing decisions; they never make them.

## AFP-equivalent

Worker Desk stores cost assumptions separately from real quota. Defaults are configurable in the
console:

```text
1 AFP-equivalent = CNY 0.002
CNY 1 = 500 AFP-equivalent
Kimi CNY 699 = 349,500 AFP-equivalent (nominal purchase-cost comparison only)
```

This does **not** mean Kimi supplies 349,500 AFP or a known token allowance. If a completed Kimi
billing cycle processed `M` million observed tokens, `349500 / M` is only that cycle's blended
nominal AFP-equivalent per million. It cannot separate K3 from K2.8 without per-model usage or
multiple independently observed mixes.

With configurable assumptions Auto=50, Evolving=250 and Ark K3=1000 AFP per million tokens, and
baseline shares K3 2:1 plus K2.8/Evolving 1:1:

```text
Ark AFP = 333.33D + 125N + 50S
```

`D`, `N` and `S` are millions of deep, normal and small-task tokens. Under dynamic shares the
general expression is `1000*rD*D + 250*rN*N + 50*S`, where `rD` is 1/4, 1/3 or 1/2 and `rN` is
1/3, 1/2 or 2/3. These coefficients are estimates and promotion terms can change. The live Ark
`Used` value is authoritative; the console shows it separately from estimates and nominal cost.
