# Alibaba Token Plan seat ledger

The Alibaba Token Plan (`bailian-token-plan`) can hold independent allowance
seats that share one subscription cycle. The live control-plane query
`GetSubscriptionStats` reports each seat-type group separately, for example:

| Seat type | SeatCredits | SeatRemainingCredits | TotalSeats | AssignedSeats | SeatRefreshTime |
| --- | --- | --- | --- | --- | --- |
| max | 217742 | 217742 | 1 | 1 | 2026-11-16T10:00:00Z |
| pro | 100000 | 13027.237225 | 1 | 1 | 2026-11-16T10:00:00Z |

Both seats share the same expiry, so the subscription entitlement for the cycle
is **317742 Credits with 230769.237225 remaining (about 72.63%)** — not the
scarcest seat (13%) and not anything multiplied by `TotalSeats`.

## Normalized shape

`scripts/bailian_quota.py` normalizes the envelope into the shared quota record:

* `windows` — exactly **one** `Credits(pool)` window when two or more independent
  groups share one `SeatRefreshTime`:
  * `limit` = sum of the group `SeatCredits`;
  * `remaining` = sum of the group `SeatRemainingCredits`;
  * `remaining_percent` = summed remaining / summed limit;
  * `resets_at` = the common expiry; `duration_minutes` = the shared full cycle;
  * `seat_count`, `seat_types`, and summed `seats` are display metadata only.
* `seat_groups` (sibling of `windows`, never inside it) — the untouched per-group
  rows for optional UI detail (`seat_type`, limit, remaining, percentage,
  expiry/degraded flags). Consumers must not treat them as additional quota
  constraints.
* A single reported group keeps the legacy `Credits(<seat>)` window exactly as
  before, with no `seat_groups` key.

Aggregation rules:

1. **Additive, not minimum, not multiplied.** `SeatCredits` and
   `SeatRemainingCredits` are already the group aggregate (`TotalSeats` is never a
   multiplier). Independent groups on the same cycle add up.
2. **Same cycle only.** Groups with a different or missing `SeatRefreshTime` are
   different cycles; they are never guessed into one sum and keep their previous
   separate windows.
3. **Unknown stays unknown.** If any member's remaining is null
   (`CreditsService` degraded), the summed remaining and percentage are unknown
   (`remaining: null`, `valid: false`, `credits_service_degraded: true`). The
   summed limit is still reported because the entitlement is known.
4. **Invalid is never hidden.** A member with an out-of-range remaining, a
   non-positive limit, or an expired cycle makes the combined window invalid
   (`valid: false`, `invalid_member` / `expired`). A plausible sum of invalid
   rows is never presented as a valid aggregate.
5. **Availability.** `available` is `false` only when the combined remaining is
   genuinely zero (total exhaustion). One exhausted seat next to a funded seat
   leaves the pool available. Unknown/invalid telemetry leaves availability
   unknown, never "unlimited" and never "exhausted".
6. **Manual snapshots and prepaid mode are unchanged.** A manual snapshot still
   imports as a `CreditsPool` anchor with its own freshness. Prepaid mode still
   neutralizes calendar conservation for Alibaba while keeping zero/expiry and
   credential gates.

## Admission and alerts

All existing consumers (admission, tier guidance, capacity forecast, the
console balance, the host/small-display meters, notifications) read `windows`.
Because the ledger exposes the combined window once, the minimum-based code
paths now see the combined percentage instead of the weakest seat. The console
shows the aggregate balance once and offers per-seat detail behind
`Seat details`; the small display receives one combined Credits row.

The Alibaba low-Credits warning is edge-triggered on the combined reading
(`≤10%`), and the exhaustion alert fires only on total exhaustion, with wording
that names all ledger seats. The dedupe key is still the opaque inference
credential identity, so a replacement key re-arms the edge.

## Recovery security

Account-level Credits telemetry is not proof that a specific inference key
recovered. With a multi-seat ledger, `quota.account_api_recovery` refuses to
clear a circuit from the positive aggregate, because the sum can come entirely
from another seat. A blocked or rejected inference key is released only by a
verified current-key model success, a genuine credential rotation or an explicit
operator retry — never by another seat's positive balance. `AUTH_REASON` was
already excluded from aggregate recovery for the same reason.

## Optional data-plane pool (separate component)

The ledger above describes accounting and admission only. Sequential key use
(primary until confirmed exhaustion, then the next eligible seat) is implemented
by the optional Alibaba loopback inference proxy (`scripts/bailian_pool.py`,
service/server wiring owned by that package). The ledger does not require the
proxy: single-key installs keep their existing `provider_api_key_files`
reference and behavior. When the proxy is enabled, the proxy maintains per-slot
state and per-request/per-model/per-seat provenance, and the ledger's combined
window remains the authoritative account-level aggregate.

## Tests

* `tests/test_bailian_aggregate.py` — additive aggregation, no `TotalSeats`
  multiplier, legacy single-seat shape, one-exhausted-seat admission, total
  exhaustion block, unknown/invalid handling, different-expiry non-aggregation,
  expired cycles, aggregate-recovery guards, view/host-meter surfaces, and alert
  wording/edges.
* `tests/bailian_seat_view.test.js` — combined balance rendering and optional
  seat detail (including degraded/unknown and legacy single-seat payloads).
* Existing coverage: `tests/test_bailian_recovery_edges.py`,
  `tests/test_multi_pool.py`, `tests/test_prepaid.py`,
  `tests/test_notifications.py`, `tests/test_host_quota_cache.py`, and
  `tests/aliyun_quota_view.test.js` keep passing unchanged.

## Honest limits

* The ledger cannot know a seat that the account API does not report; only
  reported groups are summed, never an inferred or purchased extra seat.
* Seat-type group names are not a guaranteed mapping to Keychain slot names; the
  ledger never guesses which inference key belongs to which group.
* If the account API is unavailable, the last genuine live reading (with its
  original sample time) or the manual snapshot anchor stays visible with its
  explicit freshness and `live_error`; nothing is fabricated.


## Personal Edition is a separate allowance

Set the private `bailian_plan_edition` setting to `personal` when the inference
pool contains the Personal Edition subscription key. The default remains `team`
for existing installations. Do not mix Team and Personal keys in the same active
pool: their allowances are not one ledger.

Personal Edition never queries Team `GetSubscriptionStats`. Its cache, manual
anchors, burn history and accounting-controller epoch are bound to the selected
edition and current inference-pool identity. Old Team readings remain stored but
cannot appear as Personal balance or stop new Personal requests. Switching back
to Team requires a new Team reading rather than using a Personal sample.

The optional, separately authorized `bailian_personal` adapter reads the fixed
Personal subscription, monthly usage and usage-bundle console APIs. Its optional
`bailian_personal_quota` setting contains only `enabled` and a Keychain
`service`/`account` reference. The separately approved login session is bound to
the subscription instance and the current single inference key; its values never
enter configuration or logs. Until it is connected, Personal
quota is unknown and real inference admission follows the current key/pool and
billing circuits. A quota-login failure does not invalidate the inference key;
a confirmed current-key quota zero or authentication rejection remains enforced.

Personal month usage is a percentage, not a raw Credits amount. The console
shows the monthly percentage and any independently reported usage bundle without
inventing a total Credits balance or synthesizing weekly/five-hour windows.
Manual imports made while Personal is selected get the current plan binding;
they remain explicitly marked snapshots and never inherit Team depletion latches.
