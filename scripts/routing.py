"""Opt-in ordered fallback and deterministic weighted admission routing.

A ``routing_policy`` configuration optionally maps a work tier (fast, background,
deep) to ordered stages. Each stage is a list of ``{'profile', 'weight'}`` entries.
Dispatch picks the first stage that has at least one enabled candidate whose provider
currently passes quota/circuit admission, then advances a smooth weighted round-robin
over the candidates that are available in that stage. A tier without a policy keeps
the legacy single-profile selection, so omitting the setting changes nothing.

Counters are durable state, not telemetry. They are read for route previews and only
advanced by the daemon after a task actually passes every admission check (owner cap,
provider admission and scope/resource locks); a preview, a status view or a recovery
read never moves them.

The advance rule is credit-based and deliberately does not repay unavailable time:
every available candidate gains its weight as a credit, the largest credit wins (ties
keep policy order), and the winner pays the total weight of the available candidates.
A candidate that is not available neither gains nor pays, so a long outage accrues no
catch-up debt and the returning candidate resumes from its bounded frozen credit.
Every credit stays within one total-weight span, so the durable file cannot grow
without bound across restarts.
"""
from common import STATE, read_json, write_json

TIERS = ('fast', 'background', 'deep')
COUNTERS = 'routing.json'
MAX_STAGES = 8
MAX_STAGE_ENTRIES = 8
MAX_TIER_PROFILES = 16
MAX_WEIGHT = 100
MAX_CREDIT = MAX_WEIGHT * MAX_STAGE_ENTRIES

# Quota-aware dynamic admission weights inside one same-capability stage.
# Stored policy weights stay the baseline preference; only the in-memory advance
# uses the effective weights below, so user configuration is never rewritten by
# telemetry. The signal is a reset-aware runway: for each valid window,
# remaining_fraction / time_fraction_remaining, targeting simultaneous plan
# depletion near each provider's window reset. Unknown, stale or unauthenticated
# telemetry falls back to base weights; a zero authoritative window is enforced
# earlier by provider admission (quota.allowed), which removes that candidate.
#
# Shifting is discrete and modest, never exact-depletion chasing with extreme
# ratios. A two-provider subscription pool only ever moves one step from its
# baseline ratio (allowed ladder per baseline below), with hysteresis so small
# runway differences stay at baseline and recovery returns toward baseline.
RUNWAY_MAX = 4.0            # Runway pressure cap: surplus beyond 4x on-track stops shifting.
RUNWAY_STEP = 0.15          # Material normalized runway-signal gap that moves one ratio step.
RUNWAY_HOLD = RUNWAY_STEP / 2  # Smaller drift than this stays at baseline (hysteresis).

# Economic invariant, encoded as policy — not a generic symmetric max-scale.
#
# Native Kimi subscription is always the PRIMARY source for Kimi models: Ark
# Kimi K3 bills the same models through expensive AFP, so Ark K3 must NEVER
# exceed the native Kimi share. Runway may relax the deep pair from 2:1 toward
# 1:1 when native Kimi is constrained, but never to 1:2 (Ark K3 leading). The
# mid pool compares two DIFFERENT models — native K2.8 versus the cheaper Ark
# Evolving — so after capability validation it may shift from 1:1 toward
# Evolving, but only to 1:2. Ark Auto is a separate discounted/uncertain-routing
# ordered stage and is never priced as fixed Ark K3.
#
# Each ladder is keyed by the stored (left, right) baseline weights, ordered
# from most-left-leaning to most-right-leaning. The baseline sits at index 1 and
# only ONE step either way is reachable; recovery moves back toward baseline.
# There is deliberately no 1:4/1:8 or arbitrary DYNAMIC_MAX_SCALE blowup.
_RATIO_LADDERS = {
    # deep: native Kimi K3 (left) vs Ark Kimi K3 (right). Ark K3 may catch up to
    # parity (1:1) under Kimi constraint but never lead; 3:1 leans harder into
    # the cheaper native subscription when Kimi has surplus runway.
    (2, 1): [(3, 1), (2, 1), (1, 1)],
    # background: native Kimi K2.8 (left) vs Ark Evolving (right). Evolving is
    # cheaper, so it may take the lead to 1:2; 2:1 leans back to native K2.8.
    (1, 1): [(2, 1), (1, 1), (1, 2)],
}


def _weight(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WEIGHT:
        raise ValueError('routing_policy weight must be an integer between 1 and ' + str(MAX_WEIGHT))
    return value


def validate_policy(value, profiles):
    """Strict settings-API validation; raises ValueError and returns a normalized copy.

    Every referenced profile must exist and be enabled in the same settings body.
    A profile may appear at most once per tier: credits are keyed by profile within a
    tier, so repeating one would make selection ambiguous.
    """
    if not isinstance(value, dict) or not value:
        raise ValueError('routing_policy must be a non-empty object')
    unknown = [key for key in value if not isinstance(key, str) or key not in TIERS]
    if unknown:
        raise ValueError('routing_policy keys must be fast, background or deep')
    clean_profiles = profiles if isinstance(profiles, dict) else {}
    out = {}
    for tier in TIERS:
        if tier not in value:
            continue
        stages = value[tier]
        if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
            raise ValueError('routing_policy.' + tier + ' must have 1 to ' + str(MAX_STAGES) + ' stages')
        seen = set()
        cleaned = []
        for stage in stages:
            if not isinstance(stage, list) or not 1 <= len(stage) <= MAX_STAGE_ENTRIES:
                raise ValueError('routing_policy.' + tier + ' stages must have 1 to ' +
                                 str(MAX_STAGE_ENTRIES) + ' entries')
            entries = []
            for entry in stage:
                if not isinstance(entry, dict):
                    raise ValueError('routing_policy.' + tier + ' entries must be objects')
                name = entry.get('profile')
                profile = clean_profiles.get(name) if isinstance(name, str) else None
                if not isinstance(profile, dict) or profile.get('enabled') is False or not profile.get('model'):
                    raise ValueError('routing_policy.' + tier + ' must reference enabled profiles')
                if name in seen:
                    raise ValueError('routing_policy.' + tier + ' must not repeat a profile')
                seen.add(name)
                entries.append({'profile': name, 'weight': _weight(entry.get('weight'))})
            cleaned.append(entries)
        if len(seen) > MAX_TIER_PROFILES:
            raise ValueError('routing_policy.' + tier + ' supports at most ' + str(MAX_TIER_PROFILES) + ' profiles')
        out[tier] = cleaned
    return out


def normalize_policy(value, profiles):
    """Fail-closed read of a stored policy: a malformed record routes as if unset."""
    try:
        return validate_policy(value, profiles)
    except (ValueError, TypeError):
        return None


def runtime_policy(value, profiles):
    """Fail-soft dispatch read of a stored policy.

    The structure and every weight must still be valid, or the whole policy is treated
    as unset (legacy routing). A profile reference that degraded after the policy was
    saved (removed, disabled or duplicated) is dropped in place instead of silently
    disabling every other tier; a stage left without usable entries disappears too.
    """
    profiles = profiles if isinstance(profiles, dict) else {}
    if not isinstance(value, dict) or not value:
        return None
    if any(not isinstance(key, str) or key not in TIERS for key in value):
        return None
    out = {}
    for tier in TIERS:
        if tier not in value:
            continue
        stages = value[tier]
        if not isinstance(stages, list) or not 1 <= len(stages) <= MAX_STAGES:
            return None
        seen, cleaned = set(), []
        for stage in stages:
            if not isinstance(stage, list) or not 1 <= len(stage) <= MAX_STAGE_ENTRIES:
                return None
            entries = []
            for entry in stage:
                if not isinstance(entry, dict):
                    return None
                name = entry.get('profile')
                profile = profiles.get(name) if isinstance(name, str) else None
                if not isinstance(profile, dict) or profile.get('enabled') is False or not profile.get('model'):
                    continue
                try:
                    weight = _weight(entry.get('weight'))
                except ValueError:
                    return None
                if name in seen:
                    continue
                seen.add(name)
                entries.append({'profile': name, 'weight': weight})
            if entries:
                cleaned.append(entries)
        if cleaned:
            out[tier] = cleaned
    return out or None


def configured(c):
    """Dispatch policy from configuration, or None when absent or malformed."""
    try:
        return runtime_policy(c.get('routing_policy'), c.get('profiles'))
    except Exception:
        return None


def tier_entries(policy, tier):
    """Flattened policy-order entries for one tier; profiles are unique per tier."""
    return [entry for stage in (policy or {}).get(tier, []) for entry in stage]


def tier_weights(policy, tier):
    return {entry['profile']: entry['weight'] for entry in tier_entries(policy, tier)}


def _credit(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(-MAX_CREDIT, min(value, MAX_CREDIT))


def _window_runway(w, now):
    """Reset-aware runway pressure for one valid window, or None when unusable.

    runway = remaining_fraction / time_fraction_remaining, so a provider exactly
    on track to deplete at its window reset reads ~1, a provider burning ahead
    of schedule reads <1 (more constrained) and a provider with surplus relative
    to time-to-reset reads >1. Timing comes from subscribed_at→resets_at when
    both are valid, else duration_minutes ending at resets_at; when no usable
    timing exists it falls back safely to the bare remaining fraction. The value
    is capped at RUNWAY_MAX so surplus stops shifting weights past a bound.
    """
    if not isinstance(w, dict) or w.get('valid') is not True:
        return None
    pct = w.get('remaining_percent')
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    remaining_fraction = max(0.0, min(100.0, float(pct))) / 100.0
    resets = _iso_seconds(w.get('resets_at'))
    start = _iso_seconds(w.get('subscribed_at'))
    duration = w.get('duration_minutes')
    duration_seconds = duration * 60 if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0 else None
    time_fraction = None
    if resets is not None and resets > now:
        total = None
        if start is not None and resets > start:
            total = resets - start
        elif duration_seconds:
            total = duration_seconds
        if total and total > 0:
            left = resets - now
            time_fraction = max(0.0, min(1.0, left / total))
    if time_fraction is None or time_fraction <= 0:
        pressure = remaining_fraction
    else:
        pressure = remaining_fraction / time_fraction
    return max(0.0, min(RUNWAY_MAX, pressure))


def _iso_seconds(value):
    """ISO-8601 or epoch seconds/ms to epoch seconds; None when unparseable."""
    from datetime import datetime, timezone
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value >= 1e12 else value
        return seconds if seconds > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _runway(provider_view, now):
    """Most constraining valid runway signal for one provider, or None.

    Only fresh, positively sampled, unblocked telemetry counts; anything else is
    None so the caller falls back to base weights instead of guessing. A valid
    zero-remaining window yields 0.0 here, but provider admission has already
    removed that candidate entirely, so a starved provider never lingers in a
    stage with a zero effective weight. One provider yields one signal no matter
    how many of its profiles sit in the stage.
    """
    if not isinstance(provider_view, dict) or provider_view.get('stale', True):
        return None
    if provider_view.get('state') != 'ok' or provider_view.get('available') is not True:
        return None
    signals = []
    for w in provider_view.get('windows') or []:
        runway = _window_runway(w, now)
        if runway is not None:
            signals.append(runway)
    if not signals:
        return None
    return min(signals)


def dynamics(entries, provider_by_profile, quota_view=None, now=None):
    """Effective admission weights and a safe reason for one available stage.

    entries are the stage's already-admissible candidates in policy order with
    their stored base weights. provider_by_profile maps profile name → provider
    id; quota_view maps provider id → the provider view from quota.view. ``now``
    is one deterministic timestamp for the whole decision (default: current
    time), so a stage never mixes samples from different instants.

    Provider grouping is load-bearing: every profile on one provider (Ark Auto,
    K3, Evolving, the manual DeepSeek copy) shares a single AFP allowance and a
    single runway signal. Auto consumption lowers the same Ark pressure that
    later K3/Evolving admissions use, so the bounded adjustment naturally shifts
    those pools back toward Kimi. The signal is never counted once per profile,
    and Auto does not create a separate allowance.

    Kimi stays the primary baseline: the discrete ladders below only move one
    step from the stored ratio on material runway imbalance and return toward
    baseline on recovery. The mid 1:1 pool is a same-capability baseline, not a
    1:1 total provider-spend target, and the adjustment never forces
    simultaneous depletion at the expense of Kimi-primary economics. The stored
    policy itself is never touched; this is an in-memory dispatch input.
    """
    import time as _time
    now = _time.time() if now is None else now
    base = {entry['profile']: entry['weight'] for entry in entries}
    total_base = sum(base.values()) or 1
    info = {name: {'base_weight': weight, 'base_share': weight / total_base,
                   'effective_weight': weight, 'share': weight / total_base,
                   'provider': provider_by_profile.get(name), 'runway': None}
            for name, weight in base.items()}
    if len(entries) < 2:
        return list(entries), 'single_candidate', info
    quota_view = quota_view if isinstance(quota_view, dict) else {}
    # One runway sample per provider, shared across all of its profiles.
    providers = []
    for entry in entries:
        provider = provider_by_profile.get(entry['profile'])
        if provider not in providers:
            providers.append(provider)
    if len(providers) < 2:
        # All candidates sit on one provider: headroom cannot differentiate
        # them, so the stored baseline applies unchanged.
        runway = _runway(quota_view.get(providers[0]), now) if providers else None
        for name in base:
            info[name]['runway'] = runway
        return list(entries), 'single_provider', info
    runway_by_provider = {provider: _runway(quota_view.get(provider), now) for provider in providers}
    for name in base:
        info[name]['runway'] = runway_by_provider.get(info[name]['provider'])
    if any(runway is None for runway in runway_by_provider.values()):
        # Telemetry unknown/stale for any provider in the stage: keep the
        # stored baseline rather than loading partial telemetry onto one side.
        return list(entries), 'telemetry_unknown', info
    # Each provider group's baseline weight is the sum of its member profiles'
    # base weights. The normalized runway signal is (group baseline weight ×
    # runway), so equal on-track runway reduces exactly to the stored baseline
    # (deep stays 2:1, background stays 1:1); only material imbalance moves one
    # discrete step.
    group_base = {}
    for entry in entries:
        provider = provider_by_profile.get(entry['profile'])
        group_base[provider] = group_base.get(provider, 0) + entry['weight']
    total_group_base = sum(group_base.values()) or 1
    weighted = {provider: group_base[provider] * runway_by_provider[provider] for provider in providers}
    total_weighted = sum(weighted.values())
    if total_weighted <= 0:
        return list(entries), 'telemetry_unknown', info
    group_signal = {provider: weighted[provider] / total_weighted for provider in providers}
    group_base_share = {provider: group_base[provider] / total_group_base for provider in providers}
    divergence = max(abs(group_signal[provider] - group_base_share[provider]) for provider in providers)
    if divergence < RUNWAY_HOLD:
        return list(entries), 'baseline_balanced', info

    # Discrete bounded ratio for a two-provider subscription pool. Ordered by
    # policy, left is the first provider, right the second.
    left, right = providers[0], providers[1]
    ladder = _RATIO_LADDERS.get((group_base[left], group_base[right]))
    if ladder is None:
        # Not a recognized two-provider baseline: stay at stored weights rather
        # than inventing an unbounded ratio.
        return list(entries), 'baseline_unsupported', info
    baseline_index = ladder.index((group_base[left], group_base[right]))
    # Positive advantage means the left provider has more runway than baseline
    # implies; negative means the right provider does. One step per material
    # imbalance; hysteresis keeps small gaps at baseline.
    advantage = group_signal[left] - group_base_share[left]
    if advantage >= RUNWAY_STEP:
        step = -1 if baseline_index > 0 else 0  # shift share toward left
    elif advantage <= -RUNWAY_STEP:
        step = 1 if baseline_index < len(ladder) - 1 else 0  # shift toward right
    else:
        step = 0
    ratio = ladder[baseline_index + step] if step else ladder[baseline_index]
    if step == 0:
        return list(entries), 'hysteresis_hold', info
    group_effective = {left: ratio[0], right: ratio[1]}
    out = []
    for entry in entries:
        provider = provider_by_profile.get(entry['profile'])
        members = [e for e in entries if provider_by_profile.get(e['profile']) == provider]
        member_base = sum(e['weight'] for e in members) or 1
        share = group_effective[provider] * entry['weight'] / member_base
        out.append(dict(entry, weight=max(1, int(round(share)))))
    total_effective = sum(e['weight'] for e in out) or 1
    for entry in out:
        info[entry['profile']]['effective_weight'] = entry['weight']
        info[entry['profile']]['share'] = entry['weight'] / total_effective
    reason = ('runway_shift_left' if step == -1 else 'runway_shift_right')
    return out, reason, info


def advance(entries, credits):
    """Smooth weighted round-robin advance; returns ``(profile, updated_credits)``.

    Pure: callers decide whether to commit the returned credit map. Candidates that are
    not in ``entries`` (unavailable or not offered) neither gain nor pay, so their
    frozen credit never grows while they are away and there is no catch-up debt.
    """
    if not entries:
        return None, {}
    upgraded = [(entry, _credit(credits.get(entry['profile'], 0)) + entry['weight'])
                for entry in entries]
    total = sum(entry['weight'] for entry in entries)
    best, best_credit = upgraded[0][0], upgraded[0][1]
    for entry, credit in upgraded[1:]:
        if credit > best_credit:  # ties keep policy order
            best, best_credit = entry, credit
    updated = {entry['profile']: credit for entry, credit in upgraded}
    updated[best['profile']] = best_credit - total
    return best['profile'], updated


class Admissions:
    """Durable admission credits for the configured policy.

    A daemon passes one instance to choose_ready, which proposes the next credit map
    only for a real admission and commits it; a blocked task discards the proposal.
    ``save`` is called by the daemon after the admitted tasks are durable. Read-only
    callers simply omit the instance and never touch the stored file.
    """

    def __init__(self, policy=None, credits=None):
        self.policy = policy or {}
        self.credits = credits if isinstance(credits, dict) else {}
        self.dirty = False
        self._pending = None

    @classmethod
    def load(cls, policy=None):
        policy = policy or {}
        credits = {}
        raw = read_json(STATE / COUNTERS, {})
        stored = raw.get('credits') if isinstance(raw, dict) else None
        if isinstance(stored, dict):
            for tier, values in stored.items():
                if tier not in TIERS or not isinstance(values, dict):
                    continue
                credits[tier] = {name: _credit(value) for name, value in values.items()
                                 if isinstance(name, str) and 0 < len(name) <= 64}
        return cls(policy, credits)

    def credits_for(self, tier):
        return self.credits.setdefault(tier, {})

    def propose(self, tier, deltas):
        """Stage a credit map computed from the current, unchanged credits."""
        self._pending = (tier, deltas)

    def commit(self):
        """Apply the staged advance for a task that actually passed every check."""
        if not self._pending:
            return False
        tier, deltas = self._pending
        self._pending = None
        self.credits.setdefault(tier, {}).update(deltas)
        self.dirty = True
        return True

    def discard(self):
        """Drop a staged advance for a task the scheduler could not admit."""
        self._pending = None

    def save(self):
        """Persist only real committed changes; prune credits no longer policy routed."""
        if not self.dirty:
            return False
        credits = self.credits
        if self.policy:
            credits = {}
            for tier in self.policy:
                allowed = tier_weights(self.policy, tier)
                credits[tier] = {name: value for name, value in (self.credits.get(tier) or {}).items()
                                 if name in allowed}
        write_json(STATE / COUNTERS, {'version': 1, 'credits': credits})
        return True
