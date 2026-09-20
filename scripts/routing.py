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
