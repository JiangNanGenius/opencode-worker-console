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
- **Quota-adaptive pool:** explicitly enable a bounded ratio ladder for one two-provider stage. Fresh quota runway may move one step from the saved baseline; missing or stale telemetry keeps the baseline.

Ordered fallback and weighted sharing can be mixed independently for each task tier. A user with
one subscription needs no policy at all; a user with one paid plan and one pay-as-you-go backup can
use two one-model stages. The Ark/Kimi policy below is an optional example, not a platform default.

Tier selection is based on reasoning shape, not workload size or a weak-to-strong model ranking.
Fast can own a complete bounded feature, known fix, test, documentation change or routine
deployment when its path and acceptance are clear. Normal is the capable default when a concrete
outcome needs broader investigation or synthesis, even when it reads a large repository, changes
many files, runs for a long time, or includes implementation, tests, documentation, packaging
and deployment. Deep is for an
abstract or unclear objective, an unresolved system-wide root cause, architecture trade-offs or
logic with unusually difficult invariants. Large context and task importance alone are not Deep
criteria. There is no separate total-task cap for work that genuinely needs Deep.

Choose the lowest tier that covers the decisions the worker must make. Predetermined actions and
rules are Fast. A clear outcome that still requires bounded investigation, classification or
implementation choices is Normal. Defining the goal or rules, reconciling unclear requirements,
designing a new system structure, finding an unknown system-wide cause or solving unusually
complex logic is Deep. The task's label does not decide the tier: an explicit document move is
Fast, while sorting mixed evidence into keep, merge and delete decisions is Normal.

A practical Fast gate is: the exact outcome, bounded method or area, and observable completion
evidence are all known before dispatch. Difficult planning performed earlier does not make the
remaining implementation Normal. If the worker must discover one of those answers, use Normal.
Do not restart productive work merely to change tiers and replay its context.

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
  }
}
```

Adaptive records are opt-in and tied to a stage index. Ordinary weights never become dynamic just
because they happen to be `1:1` or `2:1`.

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
economic trade. Fresh reset-aware quota telemetry can move the ratios by one step only:

| Pool | Baseline | Allowed range |
| --- | --- | --- |
| native Kimi K3 : Ark K3 | 2:1 | 3:1, 2:1 or 1:1; Ark K3 never leads |
| native Kimi K2.8 : Ark Evolving | 1:1 | 2:1, 1:1 or 1:2 |

For each valid window, runway is `remaining fraction / time fraction until reset`. The most
constrained window represents the provider. Stale, missing or unauthenticated telemetry keeps the
baseline. Ark Auto, Evolving and K3 share one runway, so heavy Auto use naturally reduces later
Ark share. Endpoint-confirmed zero removes the provider. A confirmed quota/window stop or
model-origin HTTP 429 can continue within the same OpenCode session on the next route. The
transition keeps the transcript and workspace, adds the failed provider to the exclusion set,
and instructs the new model not to repeat completed or external side effects.

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
