# Routing and plan efficiency

[简体中文](routing.zh-CN.md)

Prefer complete outcomes, usable subscription allowance and enough model/context capacity.
Subscriptions are already paid for; direct DeepSeek balance is incremental spend.
Model selection follows the four-tier contract below. Do not replay completed work when changing
models; entering Ultra starts a fresh model session with a checked handoff.

## Four tiers and Kimi quota protection

| Tier | Current model | Use |
| --- | --- | --- |
| Fast | DeepSeek Flash | Clear work whose model latency affects a concrete milestone |
| Normal | K2.8 Preview / Qwen3.8 Flash | Default investigation, implementation and tests |
| Deep | Qwen3.8 Max | Complex work needing strong reasoning |
| Ultra | Kimi K3.1 Pro, standard speed, max reasoning | Hard architecture, stubborn root causes, long-horizon work and ineffective-task escalation |

Old native and Ark K3 models are retired from new-task choices and routing; historical model
records remain accurate. Start clearly extreme reasoning work on Ultra. After two rounds without
material progress, the coordinator reviews a checkpoint before upgrading. Repair authentication,
permissions and environment blockers directly. Use Normal for routine implementation once the
uncertainty is resolved. With `ultra_fallback_to_deep` enabled (the default), confirmed Ultra
quota, five-hour/monthly allowance or balance exhaustion may continue on Qwen3.8 Max. The
bridge preserves the Ultra tier, task ID, session, authority and checkpoint, and never falls
below Max. If both routes are unavailable it waits. Fresh recovery permits return to K3.1 Pro
only at an owned idle checkpoint; completed reports are never rerun. Authentication, permission
and transport failures, disabled profiles and manual exclusions do not establish exhaustion.
Explicit profile pins and `capability_floor=ultra` continue to wait for their required model.
Setting `ultra_fallback_to_deep=false` disables the default capacity fallback.

Entering Ultra preserves the task ID, authority and checkpoint, but creates a fresh model session.
Carry completed work, evidence, decisions and remaining goals; do not replay completed operations.
The base / maximum weekly protection defaults to 30%; only K3.1 Pro can use quota at or below
the current effective threshold. Dynamic release (`kimi_weekly_reserve_dynamic`, default `true`)
keeps the base in the first half-cycle and, during an idle final half, applies
`floor + (base - floor) * clamp(seconds_until_weekly_reset / (7 days / 2), 0, 1)`, where
`floor = min(base, 5)`. A base of 30% therefore protects 30% at 84 hours, 17.5% at 42 hours,
12.14% at 24 hours, and 5% near reset. This follows the real weekly reset, not weekdays.
Queued or active Ultra work, or native K3.1 Pro activity within 30 minutes, pauses release at
the base; unknown or stale weekly, reset or demand signals also keep the base. Missing weekly
quota retains ordinary Kimi admission blocking. Disabling the switch keeps fixed base protection.
Released percentage points are the reduction in the protection threshold, not a spendable balance;
spent quota cannot be reclaimed. The account panel shows the base, effective protection, released
points, pause reason and next weekly reset from backend policy telemetry. Weekly quota, five-hour
quota and account total are independent limits; never estimate the total as four weekly windows.
Missing/stale values remain unknown; official plans without a weekly window show not applicable.
Account total must be obtained from a real account query whose identity matches the inference key.
A successful login alone does not establish that total quota was obtained. This is a scheduling
reserve: in-flight requests and upstream accounting delay can briefly cross the boundary. At or
below 5% weekly remainder with insufficient runway to reset, Ultra concurrency is one.

## Generic routing patterns

Worker Desk does not require any particular provider or number of plans. Configure profiles first,
then choose one of these patterns in **Models & routing**:

- **One model per tier:** the simple `routing` map selects one profile for fast, background, deep and ultra work.
- **Primary + backup:** put each profile in its own ordered stage. The next stage is considered only when every model in the earlier stage is unavailable.
- **Fixed weighted pool:** put two or more profiles in one stage and assign integer weights. The weights stay fixed.
- **Quota-adaptive pool:** explicitly enable a bounded continuous curve for a stage. A
  two-provider stage accepts ratio control points (`ladder`); a stage with two or more
  providers accepts a generalized `curve` where each provider's baseline weight is
  multiplied by `min(1, runway) ** strength`. One on-pace runway is neutral, a zero
  runway removes that provider, and unknown or stale telemetry keeps that provider's
  configured prior instead of erasing known pressure on its peers. Pay-as-you-go
  balances carry no window, so they are never compared as a subscription runway.

Ordered fallback and weighted sharing can be mixed independently for each task tier. A user with
one subscription needs no policy at all; a user with one paid plan and one pay-as-you-go backup can
use two one-model stages. Keep Ultra's configured policy restricted to physical Ultra models
(K3.1 Pro in this setup). Do not add Max to its primary or backup stages: the bridge derives the
capacity fallback from eligible Qwen3.8 Max profiles in the Deep route and admits it only after
confirmed Ultra exhaustion.

Tier selection weighs unresolved ambiguity and the expected benefit of faster model work, not
workload size or a weak-to-strong model ranking. Normal is the default for ordinary executable
work: a concrete outcome whose direction, material constraints and acceptance are known, including
clear/bounded implementation, investigation, tests, documentation, packaging, migration and
deployment, even when it reads a large repository, changes many files or runs for a long time.
Known direction or low uncertainty alone is not a reason to select or switch Fast. Fast is the
capable latency-priority tier for work whose direction, constraints and acceptance are clear AND
whose faster completion is reasonably expected to materially advance a concrete near-term
milestone: an explicitly urgent delivery or deadline, or a coordinator-identified dependent task
or phase that is actually ready and currently blocked by this work. Fast can own a complete
feature, fix, test, documentation change, migration or deployment, and may choose ordinary local
details; it is not weak or simple-only and needs no line-by-line prescription or fully pre-read
repository. Deep handles complex reasoning. Ultra handles especially difficult architecture,
stubborn root causes and long-horizon reasoning, and is considered after two rounds without
material progress following checkpoint review. Large context, task size and importance alone are not
Deep criteria, and urgency alone cannot bypass unresolved reasoning needs. There is no separate
total-task cap for work that genuinely needs Deep.

Choose one tier from the uncertainty the worker must remove and the expected benefit of faster
model work. Ordinary executable work with a firm direction and clear acceptance is Normal; an
explicit urgency/deadline, or a genuinely identified bottleneck node whose remaining model-driven
work is the consequential current blocker, with clear direction, is
Fast; defining the goal or rules, reconciling unclear requirements,
designing a new system structure, finding an unknown system-wide cause or solving unusually
complex logic is Deep. The task's label does not decide the tier: an explicit document move is
Normal, while sorting mixed evidence into keep, merge and delete decisions is also Normal; a
known patch that directly blocks a ready next build, with clear direction and acceptance, may be
Fast, while preparing a future-beta component whose other prerequisites still run is Normal.

A practical Fast gate is: the outcome, direction or governing constraints, and observable
completion evidence are known before dispatch, AND there is a reasonable, evidence-backed
expectation that faster model work materially advances a concrete near-term milestone — an
actually ready dependent task or phase currently blocked by this work, or an explicitly urgent
delivery or deadline. A routine serial dependency — someone will eventually need the output — is
not by itself a Fast trigger, and owning a critical resource alone does not prove the speed
benefit; do not call each successive routine pipeline stage critical and move the entire chain to
Fast. Queue or resource contention, compilation, external CI/test execution and pure waiting are
not sped up by faster model output: keep Normal unless the remaining model-driven work is itself
the consequential current blocker. Difficult planning performed earlier does not by itself make
the remaining implementation Fast;
resolve Deep uncertainty into Normal unless a concrete latency benefit justifies Fast. Fast may
discover local details; use Normal when the execution still needs open investigation or
significant interdependent judgments.

Quota never participates in coordinator tier selection. The coordinator chooses Fast, Normal or
Deep solely from unresolved uncertainty and the expected benefit of faster model work,
defaulting to Normal, and
submits `profile=auto`; the bridge applies quota and
provider policy afterward. Automatic task projections follow the same boundary: while any legal
automatic route remains, `status`/`wait`/`collect` omit the selected model, route history,
capacity-wait reasons, individual provider/tier/plan exhaustion and recovery hints. A requested
tier blocked while another tier still has a usable route (or an unknown/unproven reading) stays
an ordinary queued task with no recovery object; `wait` continues, and the base task preserves
its requested tier and capability floor. Only when every configured
automatic route across all tiers is PROVEN exhausted may they expose one generic
`automatic_routes_exhausted` blockage. Unknown/stale telemetry, auth/transport errors, manual
exclusions, capability floors and disabled or unconfigured policies are never promoted to that
message. Explicit pins and capability floors, plus auth, permission and transport failures,
retain actionable detail; operator console and `quota` views retain full telemetry.
`delegate-opencode quota --tier-guidance --compact` returns only this
stable contract. Use verbose `quota --tier-guidance` for operator investigation. The primary runway signal is
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
is available, the bridge can shift more direction-fixed work onto its Fast route. When Ark is
constrained but Kimi still has healthy runway, it retains the Normal route on Kimi instead of
putting more load on the Ark-only Fast route. Operator diagnostics expose `quota_posture` as a
summary of that internal decision; coordinators do not consume it or change the requested tier.

An optional `quota_spillover` rule fills the gap between healthy weighted routing and total
fallback. Level 1 adds a later pay-as-you-go profile to the first Fast or Normal stage at a
gradually increasing share. Level 2 uses its own faster continuous curve and temporarily replaces
automatic Normal's source stage with the first available Fast source stage. Deep and explicit
profiles never change. The Agent Plan preset uses 38% and 25% baselines with a 33% Level 1 cap
and a 100% Level 2 high-pressure cap while fresh DeepSeek CNY balance remains at least 30.
Off-peak traffic may use that cap throughout Level 2. At peak prices the cap starts at 33% and
rises continuously to 100% only when the source-plan runway becomes extreme. These are adaptive
guard rails, not fixed switch points: the combined work pool is
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

When one Normal-stage subscription is healthy and another is below the Level 1 runway threshold,
spillover follows the constrained provider rather than the combined pool. It replaces only that
provider's existing share. The replacement follows the same continuous runway curve and may
consume that whole slice under extreme pressure, but never touches the healthy plan's share.
Same-capability Normal and Deep pairs first move the constrained provider's share to their healthy
subscription peer and can reach a real 100% / 0% endpoint. Direct DeepSeek mainly absorbs a
provider-only stage such as Fast, or a global shortage across subscription pools, and can reach
100% under extreme Level 2 pressure. As the constrained plan's reset gets closer, the same remaining
allowance produces a higher runway, that provider returns and DeepSeek falls continuously to zero.

The subscription-pair curve and fallback-replacement curve form one coupled controller: Kimi and
Ark first divide paid-plan work from their own reset-aware runway, then DeepSeek replaces only the
currently constrained slice. The three balances therefore influence one decision without being
flattened into a misleading common percentage or a hard provider switch.

The console's total-work-pool meter treats a pay-as-you-go balance increase as a new observation
epoch. It never nets post-top-up balance against pre-top-up samples. When both observed balance
burn and official token pricing are available, the higher burn rate bounds displayed endurance;
one idle-heavy signal cannot lengthen the estimate. Routing conservation itself still excludes
pay-as-you-go balance and follows subscription runway only.

Budget guidance is provider-neutral. In **Models & routing**, each configured provider can use
live telemetry when available, a manually entered rolling window (remaining percentage, duration
and an explicit-zone reset time), a monetary balance threshold for pay-as-you-go accounts,
`ignore`, or `prepaid`. A manual current balance may stand in for a missing balance API.

`prepaid` marks refillable credit capacity such as an Alibaba Token Plan seat group, not a
use-it-or-lose-it allowance that must last until its renewal date. The existing provider record
carries it without a new top-level setting:

```json
{"budget_signals": {"bailian-token-plan": {"mode": "prepaid"}}}
```

Positive callable Credits remain a hard-checked admission source, and real zero, expiry and
inference-credential circuits still block. Calendar burn-before-renewal never creates
conservation, provider weight attenuation, proactive downgrades, a Max-to-Flash substitution,
or spillover of that provider's allocation because another renewable plan is low. Its control
runway is neutral (`1.0`, provider level `0`) while other plans continue to protect their own
quotas; global conservation also does not replace a Normal stage that still has usable prepaid
capacity. The console keeps the live Credits reading, observed runtime estimate and real
renewal timestamp, but does not label refillable capacity as about to run out merely because
the renewal is far away.

Prepaid recovery is current-key specific. A positive whole-account Credits aggregate never
clears a confirmed exhausted or rejected inference key, because the aggregate may include a
different seat. Replacing the inference key changes its opaque file identity and detaches the
old block; a verified completed success on the current key or the explicit
`quota --retry-provider` path can also recover. Rotating the separate quota-query AK/SK pair is
never inference recovery, and a failed control query is unknown telemetry rather than an
inference-key failure.

### Manual allowance snapshots

A subscription without a supported balance API (for example Alibaba Cloud Token Plan Credits)
can be tracked with an imported console snapshot instead of fabricated live telemetry:

```sh
delegate-opencode quota --import-snapshot /private/path/snapshot.json
```

The payload carries the provider, the plan limit and remaining amount, the reset time, the
capture time, an optional source label and an optional freshness horizon in minutes. The
snapshot is shown with explicit freshness and is never labeled as a live reading. A reading
whose reset has passed, or whose capture is older than its freshness horizon when imported,
is history only: it cannot release a recorded depletion or a billing circuit. Only a newer
positive reading from its still-current period, a verified completed model success, or an
explicit manual retry proves recovery. Out-of-order imports are rejected, and an exact
re-import of the same reading is an idempotent no-op. Alibaba Max and Flash share the one
Credits snapshot; each plan pool keeps its own separate signal. The configurable Fast
tie-breaker runway threshold applies to live and manual windows; monetary providers use their own
low-balance threshold. Unknown signals preserve ordinary routing instead of pretending the budget
is full or empty.
Do not restart productive work merely to change tiers and replay its context.

## Capability floor (optional)

Deep always has an effective Deep floor, including retained tasks; omitting the field or
supplying a weaker floor does not permit Deep-to-Flash downgrades. Ultra defaults to a Deep
minimum only for its guarded capacity fallback to Qwen3.8 Max. An explicit Ultra floor removes
that exception; quota pressure never overrides it.

A task spec may set `capability_floor` (`fast`/`normal`/`deep`/`ultra`) with a required, nonempty
`capability_reason` (1-500 characters). The floor must not exceed the requested tier; for a
`normal` floor only profiles in the Normal/Deep/Ultra first stages are eligible, and for a `deep`
floor only the Deep/Ultra first stages. An `ultra` floor permits only physically Ultra-capable
models in the Ultra first stage; putting a known weaker model there cannot satisfy the floor.
Fallback, spillover and in-session rerouting never select a
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
    "level2_offpeak_share_percent": 100,
    "level2_min_balance_cny": 30
  }
}
```

Adaptive records are opt-in and tied to a stage index. Ordinary weights never become dynamic just
because they happen to be `1:1` or `2:1`.
`level2_offpeak_share_percent` is retained as the compatible configuration key; it now represents
the Level 2 high-pressure ceiling used immediately off-peak and reached progressively at peak.

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
nominal AFP-equivalent per million. It cannot separate individual models without per-model usage or
multiple independently observed mixes.

Cost estimates use the currently enabled models and observed per-model tokens. Do not
reuse retired-model ratios. Official live usage remains authoritative; the console keeps actual
usage, estimates and nominal costs separate.

### Stable telemetry and source-pool protection

Active burn is measured across complete 30-minute observation blocks, including
unchanged quota counters. Increasing polling frequency must not inflate the rate.
Gaps over 30 minutes and counter increases break the block; partial blocks do not
become a long-term forecast. Actual zero allowance still blocks admission immediately.
When durable windows exist, only their observed replenishment releases global
conservation; a five-hour reset does not replenish a weekly budget.

For adaptive Normal/Deep stages with a usable peer, a sole Fast source has an
additional smooth reservation curve. At severe pressure its expensive-tier share
can reach zero even if its peer is also under pressure. Fast cash spillover retains
the configured balance floor and ceiling; at peak prices it reaches that ceiling
at 75% of the Level 2 runway threshold. A configured 100% ceiling permits temporary
full cash takeover. Explicit model pins and single-provider setups remain supported.
