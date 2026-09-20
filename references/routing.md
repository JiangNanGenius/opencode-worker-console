# Routing and plan efficiency

[简体中文](routing.zh-CN.md)

Prefer complete outcomes, usable subscription allowance and enough model/context capacity.
There is no universal cheapest provider order without remaining quota, reset times, latency
and task quality. A fixed subscription is already paid for; DeepSeek's direct balance is
incremental spend. Do not split a coherent task or replay its context just to change providers.

## Ark Agent Plan

Connect `volcengine-agent-plan` using OpenCode's private auth store. Never paste a credential
into a task, a tracked config, a terminal command argument or a report. When either Ark profile
is configured, Worker Desk supplies the provider definition automatically:

| Profile example | Model | Context / output | Reasoning |
| --- | --- | --- | --- |
| `ark-auto` | `volcengine-agent-plan/ark-code-latest` | 1,024,000 client ceiling / 32,000 | `max` |
| `ark-k3` | `volcengine-agent-plan/kimi-k3` | 1,024,000 / 65,536 | `max` |

The SDK is `@ai-sdk/openai` (Responses API), with base URL
`https://ark.cn-beijing.volces.com/api/plan/v3`. This is the subscription endpoint.
Credentials stay under the provider ID in OpenCode's private `auth.json`; the runtime overlay
contains no key. Configuring this provider does not change the interactive OpenCode default model.

`ark-code-latest` follows the Ark console's selected model. Verify that the console is set to
**Auto** before counting on automatic routing. An Auto response is not proof that K3 served it,
but its backend is not limited by the official OpenCode example's 256,000-token client metadata.
On 2026-09-20, a real Agent Plan Auto request accepted 270,062 input tokens and returned HTTP 200.
Worker Desk therefore advertises a 1,024,000 client ceiling so OpenCode can retain long sessions;
the model selected by Auto remains the authoritative backend limit. Pin `ark-k3` when the task
requires a predictable documented 1,024,000-capable model. Both built-in Ark profiles accepted
`reasoning.effort=max` in real Responses API checks on 2026-09-20.

Official rules checked on 2026-09-20 specify an Auto AFP coefficient of **0.5** through
2026-11-08, versus **10** for fixed K3. At equal input/output token counts, fixed K3 uses
20 times the AFP. This compares allowance, not quality, speed or final monetary cost.
Auto raises its K3 routing proportion during the documented 00:00–08:00 period; it does not
guarantee K3. Recheck the [current billing rules](https://www.volcengine.com/docs/82379/2516283)
and [promotion dates](https://www.volcengine.com/docs/82379/2533565) before making cost decisions.
No price or expiring discount is hard-coded into the scheduler.

Ark's inference key alone does not provide remaining AFP telemetry. Its control-plane
`GetAFPUsage` API uses a separately authorized account AccessKey ID/Secret. Worker Desk does
not invent a balance or silently collect those credentials. Without an adapter, Ark quota is
unknown; execution errors still return to the coordinator. Provider-side overage billing is
a separate account setting; connecting Worker Desk does not enable it.

Sources: [OpenCode integration](https://www.volcengine.com/docs/82379/2373741),
[plan overview](https://www.volcengine.com/docs/82379/2366394),
[CC Switch usage adapter](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/services/coding_plan.rs).

## Ordered stages and weighted pools

An optional `routing_policy` in `~/.config/opencode/delegate-pool.json` overrides the legacy
single-profile `routing` only for `profile=auto`. Each tier contains ordered stages; each stage
contains weighted profile members. Example, after adding and enabling the named profiles:

```json
{
  "routing_policy": {
    "background": [
      [{"profile": "senior-code", "weight": 1}],
      [{"profile": "ark-auto", "weight": 1}],
      [{"profile": "fast-code", "weight": 1}]
    ],
    "deep": [
      [{"profile": "deep-research", "weight": 2}, {"profile": "ark-k3", "weight": 1}],
      [{"profile": "fast-code", "weight": 1}]
    ]
  }
}
```

Only the first stage with available profiles participates. Weighted admissions are durable;
status polling, scope conflicts and capacity waits do not consume turns. If one member is
unavailable, the other receives the work. A 2:1 setting targets **task admissions**, not tokens,
simultaneous running jobs or money. Explicit profiles bypass this policy. Missing tiers retain
legacy routing, and an empty policy restores legacy routing entirely.

For quality-first operation, keep Kimi K3 and Ark K3 in the same weighted deep stage. This uses
both subscriptions continuously and avoids treating Ark K3 as a last resort merely because its
AFP coefficient is higher. Use Ark Auto for tasks where provider-side selection is acceptable,
and pin either K3 when model consistency or deep-context quality matters. A sequential Kimi →
Ark → DeepSeek policy remains available when allowance life is the explicit objective. Exact
optimization across plans additionally requires live remaining allowances and reset schedules;
never infer that an unknown quota is free or unlimited. Deploy config changes only when execution
is idle.

After dispatch, the model stays pinned. If it fails, the coordinator inspects partial work,
chooses an available alternative and continues only the remaining outcome in a new task.
The bridge does not replay accepted prompts or deployed side effects.
