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
import math

TIERS = ('fast', 'background', 'deep')
COUNTERS = 'routing.json'
MAX_STAGES = 8
MAX_STAGE_ENTRIES = 8
MAX_TIER_PROFILES = 16
MAX_WEIGHT = 100
MAX_CREDIT = MAX_WEIGHT * MAX_STAGE_ENTRIES
MAX_LADDER_STEPS = 7
MAX_SPILLOVER_SHARE = 50
MAX_LEVEL2_SPILLOVER_SHARE = 100

# Quota-aware dynamic admission weights inside one same-capability stage.
# Stored policy weights stay the baseline preference; only the in-memory advance
# uses the effective weights below, so user configuration is never rewritten by
# telemetry. The signal is a reset-aware runway: for each valid window,
# remaining_fraction / time_fraction_remaining, targeting simultaneous plan
# depletion near each provider's window reset. Unknown, stale or unauthenticated
# telemetry falls back to base weights; a zero authoritative window is enforced
# earlier by provider admission (quota.allowed), which removes that candidate.
#
# Every opted-in two-provider pool has its own continuous load curve. The saved
# ratios are curve control points: the stored policy ratio is the neutral anchor,
# while the first and last points are the economic bounds. A fourfold runway
# advantage reaches a bound; smaller differences interpolate smoothly between
# adjacent control points. A ladder that explicitly opts into a zero endpoint
# reaches it at a twofold runway advantage so it never leaves a cosmetic 1-5%
# allocation; ordinary nonzero ladders keep the gentler fourfold envelope.
# Whole-percent output keeps the scheduler stable without turning the curve
# back into a few fixed steps.
RUNWAY_MAX = 4.0
ZERO_ENDPOINT_RUNWAY_MAX = 2.0
EFFECTIVE_WEIGHT_TOTAL = 100

def _ratio(value, field='routing_dynamics ladder'):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(field + ' entries must be [left, right]')
    left, right = value
    if any(isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= MAX_WEIGHT
           for x in (left, right)):
        raise ValueError(field + ' weights must be integers between 0 and ' + str(MAX_WEIGHT))
    if left == 0 and right == 0:
        raise ValueError(field + ' entries cannot both be zero')
    divisor = math.gcd(left, right)
    return (left // divisor, right // divisor)


def validate_dynamics(value, policy, profiles):
    """Validate optional, explicit quota-adaptive ladders for two-provider stages.

    The policy stays completely generic. A stage is fixed-weight unless its tier/index
    appears here, so a public installation with one plan, a plain backup, or an ordinary
    1:1 review pool never changes merely because its weights resemble this project's
    Ark/Kimi setup.

    Shape: ``{tier: {"stage_index": {"ladder": [[2,1],[1,1],[1,2]]}}}``.
    Ratios run from left-leaning to right-leaning and must include the stage's stored
    baseline ratio. Runtime continuously interpolates through these control points.
    """
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise ValueError('routing_dynamics must be an object')
    if not isinstance(policy, dict) or not policy:
        raise ValueError('routing_dynamics requires routing_policy')
    unknown = [key for key in value if key not in TIERS]
    if unknown:
        raise ValueError('routing_dynamics keys must be fast, background or deep')
    profiles = profiles if isinstance(profiles, dict) else {}
    out = {}
    for tier, records in value.items():
        if not isinstance(records, dict) or not records:
            raise ValueError('routing_dynamics.' + tier + ' must map stage indexes')
        stages = policy.get(tier) or []
        cleaned = {}
        for raw_index, record in records.items():
            if not isinstance(raw_index, str) or not raw_index.isdigit() or str(int(raw_index)) != raw_index:
                raise ValueError('routing_dynamics stage indexes must be canonical strings')
            index = int(raw_index)
            if index >= len(stages):
                raise ValueError('routing_dynamics.' + tier + ' references a missing stage')
            stage = stages[index]
            if len(stage) != 2:
                raise ValueError('adaptive routing requires exactly two profiles in the stage')
            providers = []
            for entry in stage:
                model = (profiles.get(entry['profile']) or {}).get('model', '')
                providers.append(str(model).split('/', 1)[0])
            if not all(providers) or providers[0] == providers[1]:
                raise ValueError('adaptive routing requires two different providers')
            if not isinstance(record, dict) or set(record) != {'ladder'}:
                raise ValueError('routing_dynamics entries require only ladder')
            ladder = record.get('ladder')
            if not isinstance(ladder, list) or not 2 <= len(ladder) <= MAX_LADDER_STEPS:
                raise ValueError('routing_dynamics ladder must contain 2 to ' + str(MAX_LADDER_STEPS) + ' ratios')
            ratios = [_ratio(item) for item in ladder]
            if len(set(ratios)) != len(ratios):
                raise ValueError('routing_dynamics ladder ratios must be unique')
            shares = [left / (left + right) for left, right in ratios]
            if any(shares[i] <= shares[i + 1] for i in range(len(shares) - 1)):
                raise ValueError('routing_dynamics ladder must run from left-leaning to right-leaning')
            baseline = _ratio([stage[0]['weight'], stage[1]['weight']], 'routing_policy baseline')
            if baseline not in ratios:
                raise ValueError('routing_dynamics ladder must include the stage baseline ratio')
            cleaned[raw_index] = {'ladder': [list(ratio) for ratio in ratios]}
        out[tier] = cleaned
    return out or None


def runtime_dynamics(value, policy, profiles):
    """Fail closed to fixed weights when optional adaptive configuration is invalid."""
    try:
        return validate_dynamics(value, policy, profiles)
    except (ValueError, TypeError):
        return None


def configured_dynamics(c, policy=None):
    policy = policy if policy is not None else configured(c)
    return runtime_dynamics(c.get('routing_dynamics'), policy, c.get('profiles'))


def validate_spillover(value, policy, profiles):
    """Validate optional early fallback sharing while plan runway is constrained.

    The target must already be a later fallback in every selected tier. This keeps
    spillover within the user's ordered policy and prevents an arbitrary profile from
    being injected into automatic routing. The percentage is a ceiling, not a fixed
    share: runtime grows from zero toward it as the best remaining plan runway falls.
    """
    if value in (None, {}):
        return None
    required = {'enabled', 'profile', 'tiers', 'max_share_percent'}
    optional = {'level2_runway_percent', 'level2_offpeak_share_percent',
                'level2_min_balance_cny'}
    if not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(required | optional):
        raise ValueError('quota_spillover requires enabled, profile, tiers and max_share_percent')
    enabled = value.get('enabled')
    if not isinstance(enabled, bool):
        raise ValueError('quota_spillover.enabled must be boolean')
    profile = value.get('profile')
    profile_value = (profiles or {}).get(profile) if isinstance(profile, str) else None
    if not isinstance(profile_value, dict) or profile_value.get('enabled') is False or not profile_value.get('model'):
        raise ValueError('quota_spillover.profile must reference an enabled profile')
    tiers = value.get('tiers')
    if not isinstance(tiers, list) or not tiers or len(tiers) != len(set(tiers)) or \
            any(not isinstance(tier, str) or tier not in TIERS for tier in tiers):
        raise ValueError('quota_spillover.tiers must be unique fast, background or deep values')
    maximum = value.get('max_share_percent')
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_SPILLOVER_SHARE:
        raise ValueError('quota_spillover.max_share_percent must be an integer between 1 and ' +
                         str(MAX_SPILLOVER_SHARE))
    level2 = value.get('level2_runway_percent')
    if level2 is not None and (isinstance(level2, bool) or not isinstance(level2, int) or
                               not 0 <= level2 <= 100):
        raise ValueError('quota_spillover.level2_runway_percent must be an integer between 0 and 100')
    offpeak = value.get('level2_offpeak_share_percent')
    if offpeak is not None and (isinstance(offpeak, bool) or not isinstance(offpeak, int) or
                                not maximum <= offpeak <= MAX_LEVEL2_SPILLOVER_SHARE):
        raise ValueError('quota_spillover.level2_offpeak_share_percent must be an integer between ' +
                         str(maximum) + ' and ' + str(MAX_LEVEL2_SPILLOVER_SHARE))
    floor = value.get('level2_min_balance_cny')
    if floor is not None and (isinstance(floor, bool) or not isinstance(floor, (int, float)) or
                              not math.isfinite(floor) or not 0 <= floor <= 10000000):
        raise ValueError('quota_spillover.level2_min_balance_cny must be a non-negative number')
    if not isinstance(policy, dict) or not policy:
        raise ValueError('quota_spillover requires routing_policy')
    for tier in tiers:
        stages = policy.get(tier) or []
        positions = [index for index, stage in enumerate(stages)
                     if any(entry.get('profile') == profile for entry in stage)]
        if not positions or positions[0] == 0:
            raise ValueError('quota_spillover.profile must be a later fallback in routing_policy.' + tier)
    out = {'enabled': enabled, 'profile': profile, 'tiers': list(tiers),
           'max_share_percent': maximum}
    if level2 is not None:
        out['level2_runway_percent'] = level2
    if offpeak is not None:
        out['level2_offpeak_share_percent'] = offpeak
    if floor is not None:
        out['level2_min_balance_cny'] = float(floor)
    return out


def runtime_spillover(value, policy, profiles):
    """Fail closed when a stored spillover rule no longer matches its policy."""
    try:
        return validate_spillover(value, policy, profiles)
    except (ValueError, TypeError):
        return None


def configured_spillover(c, policy=None):
    policy = policy if policy is not None else configured(c)
    return runtime_spillover(c.get('quota_spillover'), policy, c.get('profiles'))


def dynamic_stage(c, tier, index, policy=None):
    configured_value = configured_dynamics(c, policy)
    return ((configured_value or {}).get(tier) or {}).get(str(index))


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

    Observed working-pace hours divided by time until reset is the primary
    signal. With no burn samples, remaining fraction / remaining window time
    fraction is a cold-start prior. Values below 1 predict exhaustion before
    reset; surplus is capped by the shared forecast. Expired data is unusable.
    """
    import capacity
    forecast = capacity.window(w, now)
    return forecast['runway'] if forecast is not None and not forecast['expired'] else None


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
    import capacity
    forecast = capacity.provider(provider_view, now)
    if not forecast['fresh'] or provider_view.get('available') is not True:
        return None
    return forecast['runway']


def dynamics(entries, provider_by_profile, quota_view=None, now=None, adaptive=None,
             runway_override=None):
    """Effective admission weights and a safe reason for one available stage.

    entries are the stage's already-admissible candidates in policy order with
    their stored base weights. provider_by_profile maps profile name → provider
    id; quota_view maps provider id → the provider view from quota.view. ``now``
    is one deterministic timestamp for the whole decision (default: current
    time), so a stage never mixes samples from different instants.

    Provider grouping is load-bearing: profiles on one provider share one quota
    runway signal, so several models never make one subscription appear to have
    extra allowance. Dynamic balancing is explicitly opt-in per stage through an
    ``adaptive`` ladder. Without it the configured weights are fixed, including
    ordinary 1:1 review pools and primary/fallback setups.

    Each adaptive stage owns a continuous curve. Its saved ratio list supplies
    ordered control points, including the stored baseline anchor and the two
    economic bounds. Runtime interpolates between them from the reset-aware runway
    ratio and never moves outside the configured envelope or rewrites the policy.
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
    if not isinstance(adaptive, dict):
        return list(entries), 'fixed_weights', info
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
    runway_by_provider = ({provider: runway_override.get(provider) for provider in providers}
                          if isinstance(runway_override, dict) else
                          {provider: _runway(quota_view.get(provider), now) for provider in providers})
    for name in base:
        info[name]['runway'] = runway_by_provider.get(info[name]['provider'])
    if any(runway is None for runway in runway_by_provider.values()):
        # Telemetry unknown/stale for any provider in the stage: keep the
        # stored baseline rather than loading partial telemetry onto one side.
        return list(entries), 'telemetry_unknown', info
    # Each provider group's baseline weight is the sum of its member profiles'
    # base weights. Equal on-track runway reduces exactly to the stored baseline
    # (deep stays 2:1, background stays 1:1). Relative runway then moves along
    # this stage's own configured curve.
    group_base = {}
    for entry in entries:
        provider = provider_by_profile.get(entry['profile'])
        group_base[provider] = group_base.get(provider, 0) + entry['weight']
    if max(runway_by_provider.values()) <= 0:
        return list(entries), 'telemetry_unknown', info
    left, right = providers[0], providers[1]
    left_runway = max(0.0, runway_by_provider[left])
    right_runway = max(0.0, runway_by_provider[right])
    if math.isclose(left_runway, right_runway, rel_tol=1e-9, abs_tol=1e-12):
        return list(entries), 'baseline_balanced', info

    # Ordered by policy, left is the first provider and right is the second.
    # A log runway ratio treats 2x and 1/2x symmetrically. Smoothstep makes the
    # response gentle around the neutral anchor while still reaching the
    # configured bound at a fourfold advantage for ordinary ladders. A ladder
    # with an explicit zero endpoint opts into a twofold bound so the bridge can
    # actually remove a severely constrained provider instead of showing 99:1.
    try:
        ladder = [_ratio(value) for value in adaptive.get('ladder', [])]
        baseline = _ratio((group_base[left], group_base[right]))
        baseline_index = ladder.index(baseline)
    except (ValueError, TypeError):
        # Runtime is fail-closed: malformed optional dynamics never change the
        # ordinary stored weights.
        return list(entries), 'adaptive_invalid', info
    runway_max = (ZERO_ENDPOINT_RUNWAY_MAX
                  if any(left_weight == 0 or right_weight == 0
                         for left_weight, right_weight in ladder)
                  else RUNWAY_MAX)
    if left_runway <= 0:
        pressure = 1.0
    elif right_runway <= 0:
        pressure = -1.0
    else:
        pressure = max(-1.0, min(1.0,
            math.log(right_runway / left_runway) / math.log(runway_max)))
    direction = 1 if pressure > 0 else -1
    magnitude = abs(pressure)
    magnitude = magnitude * magnitude * (3.0 - 2.0 * magnitude)
    end_index = len(ladder) - 1 if direction > 0 else 0
    position = baseline_index + magnitude * (end_index - baseline_index)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    shares = [pair[0] / (pair[0] + pair[1]) for pair in ladder]
    left_share = shares[lower] if lower == upper else \
        shares[lower] + (shares[upper] - shares[lower]) * fraction
    left_weight = max(0, min(EFFECTIVE_WEIGHT_TOTAL,
                             int(round(left_share * EFFECTIVE_WEIGHT_TOTAL))))
    group_effective = {left: left_weight, right: EFFECTIVE_WEIGHT_TOTAL - left_weight}
    # Reset every displayed candidate before rebuilding the effective stage.
    # Candidates at a real 0% endpoint are deliberately omitted from ``out``;
    # leaving their baseline metadata behind would make the console report a
    # phantom share even though dispatch can no longer choose them.
    for name in info:
        info[name]['effective_weight'] = 0
        info[name]['share'] = 0.0
    out = []
    for entry in entries:
        provider = provider_by_profile.get(entry['profile'])
        members = [e for e in entries if provider_by_profile.get(e['profile']) == provider]
        member_base = sum(e['weight'] for e in members) or 1
        share = group_effective[provider] * entry['weight'] / member_base
        weight = max(0, int(round(share)))
        if weight > 0:
            out.append(dict(entry, weight=weight))
    total_effective = sum(e['weight'] for e in out) or 1
    for entry in out:
        info[entry['profile']]['effective_weight'] = entry['weight']
        info[entry['profile']]['share'] = entry['weight'] / total_effective
    reason = ('runway_curve_left_' if direction < 0 else 'runway_curve_right_') + \
        str(group_effective[left]) + '_' + str(group_effective[right])
    return out, reason, info


def _scaled_entry_weights(entries, budget):
    """Scale positive entry weights to one integer budget in policy order."""
    if not entries or budget <= 0:
        return []
    total = sum(entry['weight'] for entry in entries) or 1
    raw = [budget * entry['weight'] / total for entry in entries]
    scaled = [max(1, int(math.floor(value))) for value in raw]
    delta = budget - sum(scaled)
    order = sorted(range(len(raw)), key=lambda index: (raw[index] - math.floor(raw[index]), -index),
                   reverse=True)
    cursor = 0
    while delta > 0:
        scaled[order[cursor % len(order)]] += 1
        cursor += 1
        delta -= 1
    while delta < 0:
        candidates = [index for index, value in enumerate(scaled) if value > 1]
        if not candidates:
            return []
        scaled[candidates[-1]] -= 1
        delta += 1
    return scaled


def spillover(entries, provider_by_profile, target_profile, runway_by_provider,
              threshold_percent, max_share_percent, level2_threshold_percent=None,
              level2_max_share_percent=None, replace_provider=None):
    """Blend a later pay-as-you-go fallback into a constrained plan stage.

    The best known runway among the currently available plan providers controls the
    ordinary blend. A multi-plan stage may instead name one constrained provider:
    fallback then replaces only that provider's share, preserving the healthy paid
    plan while preventing the constrained peer from draining early. Unknown telemetry
    fails closed to the original stage. Returned weights total 100 for a stable,
    human-readable effective percentage.
    """
    original = [dict(entry) for entry in entries]
    if not original or target_profile in {entry.get('profile') for entry in original}:
        return original, 'spillover_not_needed', None
    try:
        threshold = float(threshold_percent) / 100.0
        maximum = int(max_share_percent)
    except (TypeError, ValueError):
        return original, 'spillover_invalid', None
    if threshold <= 0 or not 1 <= maximum <= MAX_SPILLOVER_SHARE:
        return original, 'spillover_disabled', None
    source_providers = []
    for entry in original:
        provider = provider_by_profile.get(entry['profile'])
        if provider and provider not in source_providers:
            source_providers.append(provider)
    providers = ([replace_provider] if replace_provider in source_providers else
                 source_providers)
    values = [runway_by_provider.get(provider) for provider in providers]
    if not providers or any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                            not math.isfinite(value) for value in values):
        return original, 'spillover_telemetry_unknown', None
    best = max(0.0, max(float(value) for value in values))
    if best >= threshold:
        return original, 'spillover_runway_healthy', None
    share = maximum * (1.0 - best / threshold)
    effective_maximum = maximum
    level2_active = False
    try:
        level2_threshold = float(level2_threshold_percent) / 100.0
        level2_maximum = int(level2_max_share_percent)
        level2_active = 0 < level2_threshold < threshold and best <= level2_threshold and \
            maximum <= level2_maximum <= MAX_LEVEL2_SPILLOVER_SHARE
    except (TypeError, ValueError):
        level2_active = False
    if level2_active:
        # Continue smoothly from the Level 1 curve at the Level 2 boundary,
        # then ramp faster to the Level 2 cap. The cap is reached when fitted
        # runway falls to 90 percent of the Level 2 threshold, not only at zero.
        # This gives a genuinely empty constrained-plan endpoint instead of
        # leaving a token 1 percent allocation under severe provider pressure.
        entry_share = maximum * (1.0 - level2_threshold / threshold)
        full_share_runway = level2_threshold * 0.90
        progress = min(1.0, max(0.0, (level2_threshold - best) /
                                max(0.000001, level2_threshold - full_share_runway)))
        share = entry_share + (level2_maximum - entry_share) * progress
        effective_maximum = level2_maximum
    share = int(round(share))
    if share <= 0:
        return original, 'spillover_below_one_percent', None
    share = min(effective_maximum, share)
    if replace_provider:
        normalized = _scaled_entry_weights(original, 100)
        if not normalized:
            return original, 'spillover_invalid_weights', None
        replace_entries = [entry for entry in original
                           if provider_by_profile.get(entry['profile']) == replace_provider]
        replace_budget = sum(normalized[index] for index, entry in enumerate(original)
                             if provider_by_profile.get(entry['profile']) == replace_provider)
        # In a mixed paid-plan pool the cash fallback replaces only the
        # constrained provider's existing slice. The replacement is continuous
        # and may consume that whole slice under extreme pressure, leaving the
        # healthy subscription untouched and the constrained plan available for
        # another tier such as Fast. It never grows beyond the replaced slice.
        share = min(share, replace_budget)
        if share <= 0:
            return original, 'spillover_below_one_percent', None
        replacement_weights = _scaled_entry_weights(replace_entries, replace_budget - share)
        replacement_by_profile = {entry['profile']: replacement_weights[index]
                                  for index, entry in enumerate(replace_entries)
                                  if index < len(replacement_weights)}
        out = []
        for index, entry in enumerate(original):
            if provider_by_profile.get(entry['profile']) == replace_provider:
                weight = replacement_by_profile.get(entry['profile'], 0)
            else:
                weight = normalized[index]
            if weight > 0:
                out.append(dict(entry, weight=weight))
    else:
        source_budget = 100 - share
        if source_budget == 0:
            out = []
        else:
            scaled = _scaled_entry_weights(original, source_budget)
            if not scaled:
                return original, 'spillover_invalid_weights', None
            out = [dict(entry, weight=scaled[index]) for index, entry in enumerate(original)]
    out.append({'profile': target_profile, 'weight': share})
    info = {entry['profile']: {'effective_weight': entry['weight'],
                               'share': entry['weight'] / 100.0,
                               'provider': provider_by_profile.get(entry['profile']),
                               'runway': runway_by_provider.get(provider_by_profile.get(entry['profile']))}
            for entry in out}
    prefix = 'quota_spillover_level2_' if level2_active else 'quota_spillover_'
    return out, prefix + str(share) + 'pct', info


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
