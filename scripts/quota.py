"""Read-only account telemetry and provider billing circuit; selection stays with the coordinator."""
import calendar
import math
import re
from datetime import datetime, timezone
import time
from concurrent.futures import ThreadPoolExecutor
from common import config, STATE, HttpFailure, auth_key, credential_identity, locked, read_json, redact, request, write_json
from providers import ARK_PROVIDER
import ark_quota
import routing

BUDGET_MODES = {'manual_window', 'monetary', 'ignore'}


def validate_budget_signals(value, profiles):
    """Validate optional guidance-only budget sources for providers without telemetry."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError('budget_signals must be an object with at most 32 providers')
    known = {str(p.get('model', '')).split('/', 1)[0] for p in (profiles or {}).values()
             if isinstance(p, dict) and '/' in str(p.get('model', ''))}
    out = {}
    for provider, raw in value.items():
        if not isinstance(provider, str) or provider not in known or not isinstance(raw, dict):
            raise ValueError('budget_signals provider must be configured')
        mode = raw.get('mode')
        if mode not in BUDGET_MODES:
            raise ValueError('budget signal mode must be manual_window, monetary or ignore')
        item = {'mode': mode}
        if mode == 'manual_window':
            remaining = raw.get('remaining_percent')
            duration = raw.get('duration_minutes')
            reset = raw.get('resets_at')
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or \
                    not math.isfinite(remaining) or not 0 <= remaining <= 100:
                raise ValueError('manual window remaining_percent must be between 0 and 100')
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or \
                    not math.isfinite(duration) or not 1 <= duration <= 525600:
                raise ValueError('manual window duration_minutes must be between 1 and 525600')
            try:
                parsed_reset = datetime.fromisoformat(reset.replace('Z', '+00:00')) \
                    if isinstance(reset, str) and len(reset) <= 64 else None
            except ValueError:
                parsed_reset = None
            if parsed_reset is None or parsed_reset.tzinfo is None or routing._iso_seconds(reset) is None:
                raise ValueError('manual window resets_at must be ISO-8601 with an explicit time zone')
            item.update(remaining_percent=float(remaining), duration_minutes=float(duration),
                        resets_at=reset)
        elif mode == 'monetary':
            currency = raw.get('currency')
            threshold = raw.get('low_balance')
            manual = raw.get('manual_balance')
            if not isinstance(currency, str) or not re.match(r'^[A-Z]{3,8}$', currency):
                raise ValueError('monetary budget currency must use uppercase letters')
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or \
                    not math.isfinite(threshold) or threshold < 0:
                raise ValueError('monetary low_balance must be non-negative')
            if manual is not None and (isinstance(manual, bool) or not isinstance(manual, (int, float)) or
                                       not math.isfinite(manual) or manual < 0):
                raise ValueError('manual_balance must be non-negative when supplied')
            item.update(currency=currency, low_balance=float(threshold))
            if manual is not None:
                item['manual_balance'] = float(manual)
        out[provider] = item
    return out


def normalized_budget_signals(value, profiles):
    try:
        return validate_budget_signals(value, profiles)
    except ValueError:
        return {}


def _telemetry_identity(provider):
    """Identity of the credential the quota sampler actually uses.

    Bearer-token providers sample with the same OpenCode API key the data plane
    uses, so the shared credential_identity applies. Ark's control plane is a
    separate account AccessKey: the cache and its fresh-sample clearing use the
    AccessKey hash so a rotated control credential invalidates stale telemetry.
    Billing/model-error circuits deliberately stay bound to the OpenCode
    inference credential (common.credential_identity) via trip/billing_block;
    this helper never feeds them, so an inference-key rotation is never
    mis-attached to a control-plane identity.
    """
    if provider == ARK_PROVIDER:
        return ark_quota.credential_identity()
    return credential_identity(provider)

try:
    import zoneinfo
except ImportError:  # pragma: no cover - Python < 3.9; schedules fail closed without IANA zones.
    zoneinfo = None

ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/user/balance',
    'kimi-for-coding': 'https://api.kimi.com/coding/v1/usages',
}

# Control-plane (AK/SK signed) providers use a private fetch instead of the
# bearer-token ENDPOINTS path; credentials never travel through this module.
CONTROL_PLANE = {ARK_PROVIDER: ark_quota.fetch}


BILLING = 'billing.json'
HISTORY = 'quota-history.json'
HISTORY_RETENTION_SECONDS = 35 * 86400
HISTORY_MAX_SAMPLES = 720
MONTHLY_REASON = 'monthly_usage_limit'
BALANCE_REASON = 'insufficient_balance'
GENERIC_REASON = 'billing_error'
# Explicit short usage-window exhaustion (for example a 5-hour limit). It is a real,
# self-resolving window, not a monthly plan block and not a durable billing circuit.
WINDOW_REASON = 'usage_window_limit'
REASON_SEVERITY = {GENERIC_REASON: 1, BALANCE_REASON: 2, MONTHLY_REASON: 3}
MANUAL_RETRY_NOTE = ('Manual retry authorization: releases the block so new attempts may run until a further '
                     'billing error re-opens it. It is not proof the provider quota was restored. Seen message '
                     'IDs and recovery watermarks are preserved, and a retry that is still exhausted re-blocks.')
MANUAL_RETRY_WARNING = 'Not proven recovery; new attempts may still fail while the provider is exhausted.'
# Kimi's per-window usage ratios and the duration each one represents. Only these
# known keys are accepted as a fallback, so an unknown key is never guessed into a
# window; the duration also deduplicates the ratio against the same limits row.
KNOWN_RATIO_WINDOWS = {'limit_5h': 300, 'limit_7d': 10080}
# The overall usage aggregate already represents the weekly window in this endpoint.
# A valid aggregate stays authoritative and suppresses the duplicate ratio card.
WEEKLY_RATIO_KEYS = {'limit_7d'}
MONTHLY_RESET_KEY = 'kimi_monthly_reset'
MONTHLY_RESET_DEFAULTS = {'enabled': False, 'day': 1, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
SCHEDULED_RESET_RELEASE = 'scheduled_reset'
MODEL_SUCCESS_RELEASE = 'model_success'
_MONTHLY_TIME = re.compile(r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


def _epoch(value):
    """Positive finite epoch seconds from untrusted state, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _zone(name):
    if zoneinfo is None:
        raise ValueError('zoneinfo unavailable')
    return zoneinfo.ZoneInfo(name)


def valid_timezone(name):
    """True only for an IANA zone the host can resolve; never the host's local zone."""
    if not isinstance(name, str) or not name or len(name) > 128 or name.startswith('/') or '..' in name:
        return False
    try:
        _zone(name)
        return True
    except Exception:
        return False


def valid_monthly_time(value):
    return isinstance(value, str) and bool(_MONTHLY_TIME.match(value))


def normalize_monthly_schedule(value):
    """Fail-closed normalization of a stored schedule.

    Malformed or legacy fields fall back to generic defaults and disable the schedule, so an
    invalid record can never release a block. A personal choice such as day 19 is only ever
    active when it was explicitly stored and validated; it is never a public default.
    """
    defaults = dict(MONTHLY_RESET_DEFAULTS)
    if not isinstance(value, dict):
        return defaults
    day = value.get('day')
    time_text = value.get('time')
    zone_name = value.get('timezone')
    enabled = (value.get('enabled') is True and isinstance(day, int) and not isinstance(day, bool)
               and 1 <= day <= 31 and valid_monthly_time(time_text) and valid_timezone(zone_name))
    return {'enabled': enabled,
            'day': day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31
                   else defaults['day'],
            'time': time_text if valid_monthly_time(time_text) else defaults['time'],
            'timezone': zone_name if valid_timezone(zone_name) else defaults['timezone']}


def validate_monthly_schedule(value):
    """Strict settings-API validation; raises ValueError and returns a normalized copy."""
    if not isinstance(value, dict):
        raise ValueError('kimi_monthly_reset must be an object')
    enabled = value.get('enabled')
    if not isinstance(enabled, bool):
        raise ValueError('kimi_monthly_reset.enabled must be boolean')
    day = value.get('day')
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise ValueError('kimi_monthly_reset.day must be an integer between 1 and 31')
    time_text = value.get('time')
    if not valid_monthly_time(time_text):
        raise ValueError('kimi_monthly_reset.time must be 24-hour HH:MM')
    zone_name = value.get('timezone')
    if not valid_timezone(zone_name):
        raise ValueError('kimi_monthly_reset.timezone must be a valid IANA time zone')
    return {'enabled': enabled, 'day': day, 'time': time_text, 'timezone': zone_name}


def configured_monthly_schedule():
    """Normalized schedule from configuration; any read failure fails closed to disabled."""
    try:
        return normalize_monthly_schedule(config().get(MONTHLY_RESET_KEY))
    except Exception:
        return dict(MONTHLY_RESET_DEFAULTS)


def _month_shift(year, month, delta):
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _boundary_epoch(schedule, year, month):
    """Epoch seconds of the clamped monthly boundary in the schedule's own IANA zone."""
    zone = _zone(schedule['timezone'])
    day = min(schedule['day'], calendar.monthrange(year, month)[1])
    return datetime(year, month, day, int(schedule['time'][:2]), int(schedule['time'][3:]), tzinfo=zone).timestamp()


def monthly_boundaries(now, schedule):
    """(most recent elapsed, next) boundary epochs for a normalized enabled schedule."""
    zone = _zone(schedule['timezone'])
    local = datetime.fromtimestamp(now, tz=zone)
    current = _boundary_epoch(schedule, local.year, local.month)
    if current > now:
        year, month = _month_shift(local.year, local.month, -1)
        return _boundary_epoch(schedule, year, month), current
    year, month = _month_shift(local.year, local.month, 1)
    return current, _boundary_epoch(schedule, year, month)


def monthly_reset_view(now=None):
    """Read-only schedule exposure for quota consumers; next_reset_at is ISO8601 when enabled."""
    schedule = configured_monthly_schedule()
    out = dict(schedule)
    out['next_reset_at'] = None
    if schedule['enabled']:
        try:
            _, following = monthly_boundaries(time.time() if now is None else now, schedule)
            out['next_reset_at'] = datetime.fromtimestamp(following, tz=timezone.utc).isoformat()
        except Exception:
            out['enabled'] = False
    return out


def trip(provider, message='', message_id=None, occurred_at=None, reason=None):
    """Open the provider billing circuit on an unequivocal model billing error.

    Durable and credential-aware: the block binds to the current credential. Each seen
    message ID is retained without eviction, and an occurrence watermark at recovery
    ignores errors predating it, so an old session error can never reopen a recovered
    circuit. The reason kind separates a replenishable balance failure from hidden
    monthly plan exhaustion, which fresh usage telemetry must never clear.
    """
    ident = credential_identity(provider)
    if reason not in REASON_SEVERITY:
        reason = GENERIC_REASON
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider, {})
        ids = b.get('message_ids', [])
        if message_id and message_id in ids:
            return  # Already recorded; never relatch from a historical error.
        if occurred_at and b.get('cleared_at') and occurred_at <= b['cleared_at']:
            return  # Predates the recovery watermark.
        if not b.get('cleared_at') and b.get('credential') == ident and \
                REASON_SEVERITY.get(b.get('reason'), 0) > REASON_SEVERITY[reason]:
            # An active monthly block is never downgraded by a generic sibling error.
            reason = b['reason']
        if message_id:
            ids = ids + [message_id]
        blocks[provider] = {'credential': ident, 'opened_at': time.time(),
                            'message': redact(str(message))[:400], 'message_ids': ids,
                            'reason': reason}
        write_json(STATE / BILLING, blocks)


def billing_block(provider):
    """Active billing block for the provider's current credential, or None."""
    b = read_json(STATE / BILLING, {}).get(provider)
    if not b or b.get('cleared_at'):
        return None
    ident = credential_identity(provider)
    if ident is not None and b.get('credential') is not None and b['credential'] != ident:
        return None  # Credential rotated; the circuit binds to the old credential only.
    return b


def clear(provider, sampled_since=None, identity=None, evidence='quota'):
    """Close the circuit on fresh verified evidence; keep seen message IDs and watermarks.

    evidence is 'quota' (fresh positive account query) or 'manual_retry' (explicit local
    authorization). Monthly plan exhaustion never clears with 'quota': the plan's usage
    endpoint keeps reporting window allowance while the monthly cycle is exhausted, so
    quota telemetry is never accepted as evidence of monthly recovery, and reported reset
    timestamps are never parsed into a recovery inference. Monthly blocks instead use the
    separate, provenance-guarded paths scheduled_release (configured local-zone boundary)
    and observe_model_success (verified completed model response); neither is inferred from
    telemetry or from unverified session replies.

    sampled_since/identity guard the race where an account query started before a
    concurrent trip: a stale in-flight sample must never clear a newer circuit.
    """
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        if b.get('reason') == MONTHLY_REASON and evidence != 'manual_retry':
            return False
        if identity is not None and b.get('credential') is not None and b['credential'] != identity:
            return False
        if sampled_since is not None and b.get('opened_at', 0) > sampled_since:
            return False
        blocks[provider] = {'credential': b.get('credential'), 'cleared_at': time.time(),
                            'message_ids': b.get('message_ids', []), 'reason': b.get('reason'),
                            'opened_at': b.get('opened_at'), 'message': b.get('message'),
                            'released': evidence}
        write_json(STATE / BILLING, blocks)
        return True


def scheduled_release(provider, now=None):
    """Release a monthly block opened strictly before the most recent configured boundary.

    The schedule is the configured IANA-zone monthly boundary, independent of host local
    time and DST. The persisted watermark is that boundary, not this (possibly late) check
    time, so a genuine post-boundary error re-blocks for the rest of the cycle while
    earlier errors stay stale and can never relatch. Only a monthly block bound to the
    current credential is touched; this is authorization to try, never proof of recovery.
    """
    if provider != 'kimi-for-coding':
        return False
    now = time.time() if now is None else now
    schedule = configured_monthly_schedule()
    if not schedule['enabled']:
        return False
    try:
        boundary, _ = monthly_boundaries(now, schedule)
    except Exception:
        return False
    if _epoch(boundary) is None:
        return False
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at') or b.get('reason') != MONTHLY_REASON:
            return False
        opened = _epoch(b.get('opened_at'))
        if opened is None or opened >= boundary:
            return False
        # A known current credential must exactly match the block's binding; a rotated or
        # removed credential (None) never releases, and malformed legacy state fails closed.
        ident = credential_identity(provider)
        if ident is None or b.get('credential') != ident:
            return False
        blocks[provider] = dict(b, cleared_at=boundary, released=SCHEDULED_RESET_RELEASE)
        write_json(STATE / BILLING, blocks)
        return True


def observe_model_success(provider, identity, started_at, completed_at):
    """Clear the current credential's monthly block after a verified completed success.

    The caller owns provenance: it passes the request credential identity captured
    privately for the actual provider plus that request's start and completion times from
    a genuinely finished, error-free model response. The request must have started strictly
    after the block opened, so a success predating the failure can never release it, and
    completion must not precede the start. All timestamps must be positive and finite, and
    the supplied identity must still equal the provider's current credential, so a rotated
    or removed credential can never clear a block. The completion time becomes the
    watermark, preserving message IDs and the credential binding; another block reason
    never clears. Returns True only when this call released the block.
    """
    if not isinstance(provider, str) or not provider:
        return False
    if not isinstance(identity, str) or not identity:
        return False
    started = _epoch(started_at)
    completed = _epoch(completed_at)
    if started is None or completed is None or completed < started:
        return False
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at') or b.get('reason') != MONTHLY_REASON:
            return False
        opened = _epoch(b.get('opened_at'))
        if opened is None or started <= opened:
            return False
        ident = credential_identity(provider)
        if ident is None or identity != ident or b.get('credential') != ident:
            return False
        blocks[provider] = dict(b, cleared_at=completed, released=MODEL_SUCCESS_RELEASE)
        write_json(STATE / BILLING, blocks)
        return True


def known_providers():
    names = set(ENDPOINTS) | set(CONTROL_PLANE)
    try:
        names.update(str(p.get('model', '')).split('/', 1)[0] for p in config().get('profiles', {}).values()
                     if isinstance(p, dict) and '/' in str(p.get('model', '')))
    except Exception:
        pass
    return names


def retry_provider(provider):
    """Explicit local release/recheck authorization; never inferred from telemetry.

    This is one explicit path that may release a monthly block for new attempts, alongside
    the configured scheduled boundary release and a verified completed model success. It is
    labeled as an authorization to try again, not as proven recovery: the release stays
    open until a further billing error re-opens the block, and seen message IDs plus the
    cleared_at watermark keep historical errors from re-latching.
    """
    if not isinstance(provider, str) or provider not in known_providers():
        raise ValueError('Unknown provider: ' + str(provider))
    block = billing_block(provider)
    if not block:
        return {'provider': provider, 'released': False, 'action': 'provider_not_blocked',
                'proven_recovery': False,
                'note': 'No active billing block for this provider; nothing to release.'}
    released = clear(provider, evidence='manual_retry')
    return {'provider': provider, 'released': bool(released), 'action': 'manual_retry_authorized',
            'reason': block.get('reason'), 'proven_recovery': False,
            'message_ids_preserved': len(block.get('message_ids', [])),
            'warning': MANUAL_RETRY_WARNING, 'note': MANUAL_RETRY_NOTE}


def number(x):
    if isinstance(x, bool):
        return None
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _window_key(value):
    return '%s|%s' % (value.get('name', ''), value.get('duration_minutes', ''))


def _record_history(result, now):
    """Retain bounded, credential-scoped quota samples without request content or secrets."""
    history = read_json(STATE / HISTORY, {})
    for provider, value in result.items():
        if value.get('state') != 'ok' or not value.get('sampled_at'):
            continue
        identity = value.get('_credential')
        record = history.get(provider, {}) if history.get(provider, {}).get('_credential') == identity else {}
        samples = record.get('samples', []) if isinstance(record.get('samples'), list) else []
        windows = {}
        for item in value.get('windows', []):
            remaining = number(item.get('remaining_percent'))
            if item.get('valid', True) and remaining is not None:
                windows[_window_key(item)] = {'remaining_percent': remaining,
                                              'resets_at': item.get('resets_at')}
        balances = {}
        for item in value.get('balances', []):
            currency = item.get('currency') if isinstance(item, dict) else None
            remaining = number(item.get('remaining')) if isinstance(item, dict) else None
            if isinstance(currency, str) and 1 <= len(currency) <= 8 and remaining is not None and remaining >= 0:
                balances[currency] = remaining
        sampled = number(value.get('sampled_at'))
        if (windows or balances) and sampled is not None and (not samples or samples[-1].get('time') != sampled):
            samples.append({'time': sampled, 'windows': windows, 'balances': balances})
        samples = [x for x in samples if isinstance(x, dict) and number(x.get('time')) is not None
                   and now - float(x['time']) <= HISTORY_RETENTION_SECONDS][-HISTORY_MAX_SAMPLES:]
        history[provider] = {'_credential': identity, 'samples': samples}
    write_json(STATE / HISTORY, history)


def consumption_estimate(value, samples, now=None):
    """Estimate wall-clock range at observed burn rate, including idle time."""
    now = time.time() if now is None else now
    remaining = number(value.get('remaining_percent'))
    duration = number(value.get('duration_minutes'))
    reset = routing._iso_seconds(value.get('resets_at'))
    if remaining is None or remaining < 0:
        return None
    cycle_rate = None
    cycle_elapsed_hours = None
    if duration and duration > 0 and reset:
        elapsed_hours = max(0.0, (duration * 60 - max(0.0, reset - now)) / 3600)
        cycle_elapsed_hours = elapsed_hours
        used = max(0.0, 100.0 - remaining)
        if elapsed_hours >= 1 / 12 and used >= .01:
            cycle_rate = used / elapsed_hours
    key = _window_key(value)
    points = []
    for sample in samples if isinstance(samples, list) else []:
        stamp = number(sample.get('time')) if isinstance(sample, dict) else None
        row = sample.get('windows', {}).get(key) if isinstance(sample, dict) else None
        prior = number(row.get('remaining_percent')) if isinstance(row, dict) else None
        same_cycle = not value.get('resets_at') or not row or row.get('resets_at') == value.get('resets_at')
        if stamp is not None and prior is not None and same_cycle and 0 < now - stamp <= 24 * 3600:
            points.append((stamp, prior))
    points.sort()
    recent_rate = None
    span_hours = 0.0
    if points:
        oldest = next(((stamp, prior) for stamp, prior in points if now - stamp >= 300), None)
        if oldest:
            span_hours = (now - oldest[0]) / 3600
            recent_rate = max(0.0, oldest[1] - remaining) / span_hours
    if recent_rate is None and cycle_rate is None:
        return {'hours': None, 'rate_percent_per_hour': None, 'source': 'collecting',
                'idle': False, 'sample_span_hours': span_hours}
    if recent_rate is None:
        rate, source = cycle_rate, 'window_average'
    elif cycle_rate is None:
        rate, source = recent_rate, 'recent'
    else:
        recent_weight = min(.85, .35 + span_hours / 12)
        rate = recent_rate * recent_weight + cycle_rate * (1 - recent_weight)
        source = 'idle_adjusted' if recent_rate <= .001 else 'blended'
    smoothing_confidence = 1.0
    duration_hours = duration / 60.0 if duration and duration > 0 else None
    if duration_hours and cycle_elapsed_hours is not None and rate is not None:
        # A newly opened window often receives several tasks at once. Do not
        # extrapolate that first burst across the whole rolling window. Blend
        # toward the window's neutral full-cycle rate until enough wall-clock
        # evidence has accumulated; actual remaining quota is never smoothed.
        baseline_rate = 100.0 / duration_hours
        horizon = min(6.0, max(1.0, duration_hours / 24.0))
        smoothing_confidence = max(0.0, min(1.0, cycle_elapsed_hours / horizon))
        if rate > baseline_rate and smoothing_confidence < 1.0:
            rate = baseline_rate + (rate - baseline_rate) * smoothing_confidence
    if remaining == 0:
        return {'hours': 0.0,
                'rate_percent_per_hour': round(rate, 4) if rate is not None else None,
                'source': 'exhausted', 'idle': False,
                'sample_span_hours': round(span_hours, 2)}
    idle = recent_rate is not None and recent_rate <= .001 and span_hours >= 1 / 6
    # Estimate working pace separately from wall-clock pace. Idle gaps must not
    # make the next batch appear cheaper. Tiny rounding deltas are not activity.
    active_spend = active_hours = 0.0
    for (before_at, before), (after_at, after) in zip(points, points[1:] + [(now, remaining)]):
        dt = (after_at - before_at) / 3600
        drop = before - after
        if 0 < dt <= .5 and drop >= .01:
            active_spend += drop
            active_hours += dt
    # Active-only rate is intentionally slower to enter than the five-minute
    # wall-clock estimate. Short rolling windows need 30 minutes of evidence;
    # daily-or-longer windows need two hours because their counters are coarse
    # and a one-percent tick can otherwise swing the entire routing curve.
    active_min_hours = .5 if not duration_hours or duration_hours <= 24 else 2.0
    raw_active_rate = active_spend / active_hours if active_hours >= active_min_hours else None
    # A short parallel burst must not take over the whole-window forecast at a
    # threshold boundary. Let sustained active burn enter continuously: short
    # windows settle over two hours, long windows over eight. The provider's
    # actual remaining percentage still updates immediately and can block calls
    # at zero, so this inertia affects projection only, never admission safety.
    active_settle_hours = 2.0 if not duration_hours or duration_hours <= 24 else 8.0
    active_confidence = 0.0
    active_rate = None
    if raw_active_rate is not None:
        active_confidence = max(0.0, min(1.0,
            (active_hours - active_min_hours) /
            max(.000001, active_settle_hours - active_min_hours)))
        active_rate = (rate or 0) + max(0.0, raw_active_rate - (rate or 0)) * active_confidence
    forecast_rate = max(rate or 0, active_rate or 0)
    hours = remaining / forecast_rate if forecast_rate > .001 else None
    return {'hours': round(min(hours, 24 * 365), 2) if hours is not None else None,
            'rate_percent_per_hour': round(rate, 4) if rate is not None else None,
            'active_rate_percent_per_hour': round(active_rate, 4) if active_rate is not None else None,
            'raw_active_rate_percent_per_hour': round(raw_active_rate, 4) if raw_active_rate is not None else None,
            'active_sample_hours': round(active_hours, 3),
            'active_min_sample_hours': active_min_hours,
            'active_settle_hours': active_settle_hours,
            'active_confidence': round(active_confidence, 3),
            'smoothing_confidence': round(smoothing_confidence, 3),
            'source': source, 'idle': idle, 'sample_span_hours': round(span_hours, 2)}


def balance_consumption_estimate(value, samples, now=None, priced=None):
    """Estimate PAYG range from official token prices and observed balance burn.

    A balance increase starts a new observation epoch. Mixing samples from before
    and after a top-up can make net burn look close to zero and inflate remaining
    runtime by hundreds of hours. When both balance burn and token pricing are
    available, use the higher burn rate: this is a range estimate, so one weak or
    idle-heavy signal must not lengthen the displayed endurance.
    """
    now = time.time() if now is None else now
    remaining = number(value.get('remaining')) if isinstance(value, dict) else None
    currency = value.get('currency') if isinstance(value, dict) else None
    if remaining is None or remaining < 0 or not isinstance(currency, str):
        return None
    points = []
    for sample in samples if isinstance(samples, list) else []:
        stamp = number(sample.get('time')) if isinstance(sample, dict) else None
        prior = number((sample.get('balances') or {}).get(currency)) if isinstance(sample, dict) else None
        if stamp is not None and prior is not None and 0 < now - stamp <= 7 * 86400:
            points.append((stamp, prior))
    points.sort()
    # Segment balance history at the latest material increase. Provider balances
    # are rounded, so ignore tiny corrections but treat an actual recharge as a
    # new capacity epoch. Include the current reading to catch a top-up between
    # the most recent stored sample and this response.
    timeline = points + [(now, remaining)]
    epoch_start = 0
    for index in range(1, len(timeline)):
        previous, current = timeline[index - 1][1], timeline[index][1]
        threshold = max(.01, max(previous, current) * .005)
        if current > previous + threshold:
            epoch_start = index
    topup_detected = epoch_start > 0
    epoch_started_at = timeline[epoch_start][0] if topup_detected else None
    points = timeline[epoch_start:-1]
    observed_capacity = max([remaining] + [prior for _, prior in points])
    # A five-minute burst extrapolates a wildly pessimistic range for model APIs.
    # Require one hour of wall-clock evidence, then use the longest available span
    # so idle periods and uneven task dispatch are naturally included.
    oldest = next(((stamp, prior) for stamp, prior in points if now - stamp >= 3600), None)
    priced_rate = number(priced.get('rate_balance_per_hour')) if isinstance(priced, dict) else None
    if not oldest and priced_rate is None:
        return {'hours': 0.0 if remaining == 0 else None,
                'rate_balance_per_hour': None,
                'source': 'exhausted' if remaining == 0 else 'collecting',
                'idle': False, 'sample_span_hours': 0.0,
                'observed_capacity': round(observed_capacity, 6)}
    span_hours = (now - oldest[0]) / 3600 if oldest else number(priced.get('sample_span_hours')) or 0.0
    wall_rate = max(0.0, oldest[1] - remaining) / span_hours if oldest and span_hours > 0 else None
    if wall_rate is not None and priced_rate is not None:
        rate = max(wall_rate, priced_rate)
        source = 'conservative_balance_or_official_pricing'
    elif wall_rate is not None:
        rate, source = wall_rate, 'wall_clock'
    else:
        rate, source = priced_rate, 'official_token_pricing'
    if remaining == 0:
        result = {'hours': 0.0, 'rate_balance_per_hour': round(rate, 6),
                  'source': 'exhausted', 'idle': False,
                  'confidence': 'high' if span_hours >= 24 else 'medium' if span_hours >= 6 else 'low',
                  'sample_span_hours': round(span_hours, 2),
                  'observed_capacity': round(observed_capacity, 6)}
        if topup_detected:
            result['balance_epoch_started_at'] = epoch_started_at
        if isinstance(priced, dict):
            for key in ('estimated_spend_cny', 'tokens', 'task_count', 'pricing_effective'):
                if key in priced:
                    result[key] = priced[key]
        return result
    idle = rate <= .000001
    hours = remaining / rate if not idle else None
    confidence = 'high' if span_hours >= 24 else 'medium' if span_hours >= 6 else 'low'
    result = {'hours': round(min(hours, 24 * 365), 2) if hours is not None else None,
              'rate_balance_per_hour': round(rate, 6), 'source': source, 'idle': idle,
              'confidence': confidence, 'sample_span_hours': round(span_hours, 2),
              'observed_capacity': round(observed_capacity, 6)}
    if topup_detected:
        result['balance_epoch_started_at'] = epoch_started_at
    if isinstance(priced, dict):
        for key in ('estimated_spend_cny', 'tokens', 'task_count', 'pricing_effective'):
            if key in priced:
                result[key] = priced[key]
    return result


def window(name, detail, duration=None):
    limit, remaining = number(detail.get('limit')), number(detail.get('remaining'))
    if remaining is None and 'remaining' not in detail and limit is not None:
        # The live endpoint omits remaining on an exhausted window and reports used
        # instead. Derive remaining only from two valid numbers; malformed input stays
        # unknown rather than silently reading as 0%.
        used = number(detail.get('used'))
        if used is not None and used >= 0:
            remaining = max(0.0, limit - used)
    valid = limit is not None and limit > 0 and remaining is not None and 0 <= remaining <= limit
    resets = detail.get('resetTime')
    if isinstance(resets, (int, float)):
        try:
            resets = datetime.fromtimestamp(resets / 1000 if resets >= 1e12 else resets, timezone.utc).isoformat() if resets > 0 else None
        except (ValueError, OSError, OverflowError):
            resets = None
    return {'name': name, 'limit': limit, 'remaining': remaining,
            'remaining_percent': round(remaining / limit * 100, 3) if valid else None,
            'duration_minutes': duration, 'resets_at': resets, 'valid': valid}


def normalize(provider, body):
    if not isinstance(body, dict):
        raise ValueError('Invalid quota payload')
    if provider == 'deepseek':
        balances = []
        for b in body.get('balance_infos', []):
            amount = number(b.get('total_balance'))
            if isinstance(b.get('currency'), str) and amount is not None:
                balances.append({'currency': b['currency'], 'remaining': amount})
        return {'available': body.get('is_available') if isinstance(body.get('is_available'), bool) else None,
                'balances': balances, 'windows': []}
    windows = []
    units = {'TIME_UNIT_MINUTE': 1, 'TIME_UNIT_HOUR': 60, 'TIME_UNIT_DAY': 1440,
             'TIME_UNIT_SECOND': 1 / 60}
    for i, row in enumerate(body.get('limits', [])):
        w = row.get('window') or {}
        duration = number(w.get('duration'))
        minutes = duration * units[w.get('timeUnit')] if duration is not None and w.get('timeUnit') in units else None
        windows.append(window('window_' + str(i), row.get('detail') or {}, minutes))
    if isinstance(body.get('usage'), dict):
        # Kimi's aggregate is the shared seven-day plan pool. Carry that known
        # duration into the normalized record so reset-aware routing can judge
        # whether a small remainder is actually enough to reach the next reset.
        windows.append(window('overall', body['usage'], KNOWN_RATIO_WINDOWS['limit_7d']))
    # usages.<known key>.used_ratio is an explicit per-window fraction (0..1) and is a
    # fallback only when that window is otherwise absent or unusable. Unknown keys, a
    # ratio outside 0..1 (overage is not evidence of a specific exhausted window), a
    # window already parsed from limits (same duration) and the weekly ratio duplicated
    # by a valid overall aggregate are skipped. Malformed input stays unknown and the
    # console never shows duplicate cards for one real window.
    usages = body.get('usages')
    if isinstance(usages, dict):
        overall_valid = any(w['name'] == 'overall' and w['valid'] for w in windows)
        known = {w['duration_minutes'] for w in windows
                 if w['valid'] and w.get('duration_minutes') is not None}
        for key, detail in usages.items():
            duration = KNOWN_RATIO_WINDOWS.get(key)
            if duration is None or not isinstance(detail, dict) or duration in known:
                continue
            if key in WEEKLY_RATIO_KEYS and overall_valid:
                continue
            ratio = number(detail.get('used_ratio'))
            if ratio is None or not 0 <= ratio <= 1:
                continue
            derived = window('usages.' + key, {
                'limit': 1, 'remaining': 1.0 - ratio,
                'resetTime': detail.get('reset_time', detail.get('resetTime'))}, duration)
            derived['derived'] = True
            windows.append(derived)
            known.add(duration)
    valid = [w for w in windows if w['valid']]
    return {'windows': windows, 'available': False if any(w['remaining'] == 0 for w in valid) else
            (True if windows and len(valid) == len(windows) else None), 'balances': []}


def fetch_one(provider):
    now = time.time()
    control = CONTROL_PLANE.get(provider)
    if control is not None:
        # Signed control-plane adapters own their full failure taxonomy and
        # credential boundary; values are never visible here.
        return control()
    try:
        body = request(ENDPOINTS[provider], headers={'Authorization': 'Bearer ' + auth_key(provider),
                                                  'Accept': 'application/json'})
        value = normalize(provider, body)
        value.update(state='ok', sampled_at=now)
        return value
    except HttpFailure as e:
        state = 'auth_error' if e.status in (401, 403) else 'rate_limited' if e.status == 429 else 'unavailable'
        return {'state': state, 'http_status': e.status, 'attempted_at': now}
    except ValueError:
        return {'state': 'unknown', 'attempted_at': now}


def _providers_in_use():
    """Telemetry-bearing providers referenced by at least one configured profile."""
    try:
        models = [v['model'] for v in config()['profiles'].values()]
    except Exception:
        return []
    known = set(ENDPOINTS) | set(CONTROL_PLANE)
    return [p for p in known if any(str(m).split('/', 1)[0] == p for m in models)]


def refresh(force=False):
    with locked('quota'):
        old = read_json(STATE / 'quota.json', {})
        now = time.time()
        providers = _providers_in_use()
        identities = {p: _telemetry_identity(p) for p in providers}
        # A configured monthly schedule releases the previous cycle's Kimi monthly block on
        # every refresh, including cache hits. It is authorization to try, never quota
        # proof, and it never touches non-monthly or credential-mismatched blocks.
        scheduled_release('kimi-for-coding', now)
        same = all(old.get(p, {}).get('_credential') == identities[p] for p in providers)
        recent = old and all(now - old.get(p, {}).get('checked_at', 0) < 60 for p in providers)
        if not force and same and recent:
            snapshot = view(old)
            observe_load(config(), snapshot, now)
            return snapshot
        result = {}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(providers)))) as pool:
            for p, value in zip(providers, pool.map(fetch_one, providers)):
                last = old.get(p, {}) if identities[p] == old.get(p, {}).get('_credential') else {}
                if value['state'] not in ('ok', 'auth_error', 'no_credential') and last.get('sampled_at'):
                    value = dict(last, **value)
                value.update(checked_at=now, _credential=identities[p])
                result[p] = value
                if value['state'] == 'ok' and value.get('available') is True:
                    # Fresh positive account query recovers; a query started before a
                    # concurrent trip must never clear that newer circuit.
                    clear(p, sampled_since=now, identity=identities[p])
        write_json(STATE / 'quota.json', result)
        _record_history(result, now)
        snapshot = view(result)
        observe_load(config(), snapshot, now)
        return snapshot


def view(values):
    out = {}
    history = read_json(STATE / HISTORY, {})
    now = time.time()
    import workload
    try:
        loads = workload.snapshot(config(), now)
    except Exception:
        loads = {}
    priced = None
    if 'deepseek' in values:
        try:
            import economics
            priced = economics.recent_deepseek_spend(config(), now)
        except Exception:
            priced = None
    for p, v in values.items():
        v = {k: x for k, x in v.items() if not k.startswith('_')}
        v['windows'] = [dict(item) for item in v.get('windows', [])]
        samples = history.get(p, {}).get('samples', []) if isinstance(history.get(p), dict) else []
        for item in v['windows']:
            estimate = consumption_estimate(item, samples, now)
            if estimate is not None:
                item['consumption_estimate'] = estimate
        v['balances'] = [dict(item) for item in v.get('balances', [])]
        for item in v['balances']:
            estimate = balance_consumption_estimate(item, samples, now, priced if p == 'deepseek' else None)
            if estimate is not None:
                item['consumption_estimate'] = estimate
        v['stale'] = v.get('state') != 'ok' or time.time() - v.get('sampled_at', 0) > 900
        if p == ARK_PROVIDER:
            # Safe metadata only: the credential source label, never a value or
            # identity. No AK/SK, hash or reference path leaves this boundary.
            source = v.get('credential_source')
            v['credential_source'] = source if source in ('credential_reference', 'environment') else None
        block = billing_block(p)
        if block:
            # Overlay the active circuit so a stale positive cache is never advertised
            # and an authentic but misleading window snapshot never reads as callable.
            # The credential hash stays private; only redacted, actionable fields show.
            reason = block.get('reason') or GENERIC_REASON
            telemetry_available = v.get('available')
            billing = {'opened_at': block.get('opened_at'), 'message': block.get('message'),
                       'reason': reason, 'telemetry_state': v.get('state'),
                       'telemetry_available': telemetry_available}
            v.update(available=False, state='billing_blocked', billing=billing,
                     billing_reason=reason)
            if reason == MONTHLY_REASON:
                v['monthly_plan_exhausted'] = True
                billing['warning'] = ('Monthly plan quota is exhausted for this billing cycle. Reported '
                                      'window remaining values are authentic account telemetry but do not '
                                      'mean the provider is callable until real evidence of recovery appears.')
        if p == 'kimi-for-coding':
            # Read-only schedule exposure for the console: the same normalized setting plus
            # the next configured boundary in ISO8601 when the schedule is enabled.
            v['monthly_reset'] = monthly_reset_view()
        v['workload'] = loads.get(p, {})
        import capacity
        v['capacity_forecast'] = capacity.provider(v, now)
        fitted = iter(v['capacity_forecast']['windows'])
        for item in v['windows']:
            item['capacity_forecast'] = next(fitted, None) if capacity.window(item, now) is not None else None
        out[p] = v
    return out


def allowed(provider, q, complexity, threshold=0, retry_monthly=False):
    v = q.get(provider, {})
    # Preserve the account endpoint's verdict separately from the monthly-error overlay.
    # Explicit retries only bypass the hidden monthly flag, never confirmed empty quota.
    telemetry = v.get('billing') or {}
    state = telemetry.get('telemetry_state', v.get('state'))
    available = (telemetry.get('telemetry_available') if v.get('state') == 'billing_blocked'
                 else v.get('available'))
    if state == 'auth_error':
        return False, 'credential_rejected'
    if state == 'rate_limited':
        return False, 'quota_endpoint_rate_limited'
    # Ark without control-plane credentials keeps quota unknown: the data plane
    # may still serve, and a zero-window check below only applies to real
    # telemetry. Unknown quota is never treated as unlimited or as empty.
    empty_window = any(w.get('valid', True) and w.get('remaining') == 0
                       for w in v.get('windows', []))
    if available is False or empty_window:
        return False, 'quota_exhausted_or_account_unavailable'
    block = billing_block(provider)
    if block and not (retry_monthly and block.get('reason') == MONTHLY_REASON):
        return False, 'provider_billing_blocked'
    fresh_enough = time.time() - v.get('sampled_at', 0) <= 900
    # kimi_reserve_percent is explicitly Kimi-only by configuration contract;
    # Ark headroom steers dynamic pool weights instead of a reserve threshold.
    if provider == 'kimi-for-coding' and fresh_enough:
        pcts = [w['remaining_percent'] for w in v.get('windows', []) if w.get('remaining_percent') is not None]
        if pcts and min(pcts) < threshold and complexity != 'deep':
            return False, 'reserve_kimi_for_complex_work'
    if block:
        return True, 'explicit_profile_monthly_retry'
    return True, 'quota_unknown' if v.get('stale', True) or available is None else 'quota_available'


def kimi_weekly_remaining_percent(q):
    """Fresh authoritative Kimi weekly/overall remaining percent, else None.

    The live Kimi endpoint currently exposes the weekly plan pool as ``overall``
    without a duration. Some versions instead expose a seven-day window. Prefer
    the explicit overall aggregate, then the exact 10080-minute window; never use
    the much shorter five-hour window as a weekly signal.
    """
    v = q.get('kimi-for-coding', {})
    if not isinstance(v, dict) or v.get('stale', True):
        return None
    windows = v.get('windows') if isinstance(v.get('windows'), list) else []
    for name in ('overall',):
        for w in windows:
            if (isinstance(w, dict) and w.get('name') == name and w.get('valid', True)
                    and isinstance(w.get('remaining_percent'), (int, float))):
                return float(w['remaining_percent'])
    for w in windows:
        if (isinstance(w, dict) and w.get('duration_minutes') == 10080
                and w.get('valid', True)
                and isinstance(w.get('remaining_percent'), (int, float))):
            return float(w['remaining_percent'])
    return None


def kimi_low_weekly(q, c):
    threshold = c.get('kimi_low_weekly_threshold_percent', 5)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 5
    remaining = kimi_weekly_remaining_percent(q)
    if remaining is None or remaining > threshold:
        return False, remaining
    provider = q.get('kimi-for-coding', {})
    weekly = None
    for item in provider.get('windows', []) if isinstance(provider, dict) else []:
        if not isinstance(item, dict):
            continue
        if item.get('name') == 'overall' or item.get('duration_minutes') == 10080:
            weekly = dict(item)
            if not isinstance(weekly.get('duration_minutes'), (int, float)):
                weekly['duration_minutes'] = 10080
            break
    runway = routing._window_runway(weekly, time.time()) if weekly else None
    # A low absolute balance is only guarded when it is also burning too fast
    # to reach reset. Close to reset, a small remainder can still be on pace.
    return runway is None or runway < 1.0, remaining


def apply_kimi_low_weekly_guard(t, c, q, active=()):
    """Return (routing task, guard metadata) for automatic low-weekly protection.

    Explicit profile requests remain an operator override. Automatic normal work
    excludes every Kimi profile. Automatic deep work may still use a Kimi profile
    from the first deep stage while fewer than the configured number are active;
    lower deep stages cannot consume Kimi's last weekly allowance.
    """
    low, remaining = kimi_low_weekly(q, c)
    if not low or (t.get('requested_profile') or 'auto') != 'auto':
        return t, None
    policy = routing.configured(c) or {}
    first_deep_stage = (policy.get('deep') or [[]])[0]
    native_k3_profiles = {
        entry.get('profile') for entry in first_deep_stage
        if isinstance(entry, dict)
        and str((c.get('profiles', {}).get(entry.get('profile')) or {}).get('model', '')).split('/', 1)[0]
        == 'kimi-for-coding'
    }
    native_k3_profiles.discard(None)
    if not native_k3_profiles:
        legacy_deep = (c.get('routing') or {}).get('deep')
        legacy_profile = c.get('profiles', {}).get(legacy_deep) or {}
        if (legacy_deep and str(legacy_profile.get('model', '')).split('/', 1)[0]
                == 'kimi-for-coding'):
            native_k3_profiles.add(legacy_deep)
    all_kimi_profiles = {
        name for name, value in c.get('profiles', {}).items()
        if isinstance(value, dict)
        and str(value.get('model', '')).split('/', 1)[0] == 'kimi-for-coding'
    }
    if not all_kimi_profiles:
        return t, None
    limit = c.get('kimi_low_weekly_k3_limit', 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 16:
        limit = 1
    excluded = set(t.get('excluded_profiles') or []) | all_kimi_profiles
    native_running = sum(1 for item in active if item.get('profile') in native_k3_profiles)
    if tier_of(t) == 'deep' and native_running < limit:
        excluded -= native_k3_profiles
    routed = dict(t, excluded_profiles=sorted(excluded))
    return routed, {'remaining_percent': remaining, 'native_k3_limit': limit,
                    'excluded_profiles': sorted(excluded)}


def tier_of(t):
    """Work tier used by legacy routing and by an opt-in routing_policy."""
    if t.get('tier') in ('fast', 'normal', 'deep'):
        return 'background' if t['tier'] == 'normal' else t['tier']
    return 'deep' if t.get('complexity') == 'deep' else 'fast' if t.get('urgency') == 'fast' else 'background'


def _tier(t):  # Backward-compatible private alias.
    return tier_of(t)


def _profile_name(t, c):
    requested = t.get('requested_profile', 'auto')
    legacy = c.get('routing', {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'})
    profile = requested if requested != 'auto' else legacy.get(tier_of(t))
    return profile if profile in c['profiles'] else None


def _preferred_name(t, c):
    """Most preferred configured profile for a task, following policy order when set.

    Guidance only: this never consumes admission counters and never switches a task.
    """
    requested = t.get('requested_profile', 'auto')
    if requested != 'auto':
        return _profile_name(t, c)
    stages = (routing.configured(c) or {}).get(tier_of(t), [])
    for entry in (e for stage in stages for e in stage):
        p = c['profiles'].get(entry['profile'])
        if isinstance(p, dict) and p.get('enabled') is not False and p.get('model'):
            return entry['profile']
    return _profile_name(t, c)


def _task_provider(t, c):
    """Configured provider for the task's pinned or routed profile, with its name."""
    name = t.get('profile')
    if name not in c['profiles']:
        name = _preferred_name(t, c)
    if not name or name not in c['profiles'] or not c['profiles'][name].get('model'):
        return None, None
    return c['profiles'][name]['model'].split('/', 1)[0], name


def _active_reason(provider):
    block = billing_block(provider) if provider else None
    return (block.get('reason') or GENERIC_REASON) if block else None


def _compact_billing_kind(error):
    kind = str(error.get('billing_reason') or '')
    if kind:
        return kind
    if error.get('billing'):
        return GENERIC_REASON
    try:
        import diagnostics  # Local import avoids a module cycle; classification stays single-sourced.
        return diagnostics.billing_kind_message(error.get('http_status'), error.get('message'))
    except Exception:
        return None


def _task_billing_kind(t):
    reason = str(t.get('reason') or '')
    if reason.startswith('provider_billing_'):
        return reason[len('provider_billing_'):] or GENERIC_REASON
    for e in t.get('errors') or []:
        if isinstance(e, dict) and e.get('source') == 'model':
            kind = _compact_billing_kind(e)
            if kind:
                return kind
    return GENERIC_REASON


def meets_capability_floor(name, t, c):
    floor = t.get('capability_floor')
    if floor not in ('normal', 'deep'):
        return True
    policy = routing.configured(c) or {}
    eligible = set()
    for tier in (('background', 'deep') if floor == 'normal' else ('deep',)):
        stages = policy.get(tier) or []
        if stages:
            eligible.update(entry['profile'] for entry in stages[0])
        elif (c.get('routing') or {}).get(tier):
            eligible.add(c['routing'][tier])
    return name in eligible


def _allowed_profile(name, t, c, q):
    """(provider, ok, why) for one enabled profile, respecting quota and circuits."""
    p = c.get('profiles', {}).get(name)
    if not isinstance(p, dict) or p.get('enabled') is False or not p.get('model'):
        return None, False, 'profile_disabled_or_missing'
    provider = str(p['model']).split('/', 1)[0]
    if not meets_capability_floor(name, t, c):
        return provider, False, 'capability_floor_unavailable'
    if name in (t.get('excluded_profiles') or []):
        return provider, False, 'routing_guard_excluded_profile'
    if provider in (t.get('excluded_providers') or []):
        return provider, False, 'continuation_excluded_provider'
    ok, why = allowed(provider, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0))
    return provider, ok, why


def _first_available_policy_stage(tier, t, c, q, policy):
    """Return the first usable stage without advancing its admission counter."""
    for index, stage in enumerate((policy or {}).get(tier) or []):
        candidates = []
        providers = {}
        for entry in stage:
            provider, ok, why = _allowed_profile(entry['profile'], t, c, q)
            if ok:
                candidates.append((entry, why))
                providers[entry['profile']] = provider
        if candidates:
            return candidates, providers, index
    return [], {}, None


def _level2_conservation_active(tier, spill_config, guidance):
    """Second-level load shedding: Normal temporarily reuses the Fast source pool.

    Level one is the ordinary bounded spillover. Level two is deliberately narrower:
    it affects only automatic Normal work, requires complete reset-aware telemetry,
    and never changes Deep routing or an explicit profile request.
    """
    if tier != 'background' or not spill_config or not spill_config.get('enabled') or \
            tier not in spill_config.get('tiers', ()) or \
            guidance.get('quota_posture') != 'fast_preferred' or \
            guidance.get('conservation_refill_safe'):
        return False
    if guidance.get('conservation_level') == 2:
        return True
    threshold = guidance.get('level2_runway_percent', spill_config.get('level2_runway_percent', 0))
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
        return False
    combined = guidance.get('capacity_pressure_percent', guidance.get('combined_remaining_percent'))
    if (guidance.get('capacity_pressure_percent') is not None or guidance.get('combined_remaining_complete')) and isinstance(combined, (int, float)) and \
            not isinstance(combined, bool) and math.isfinite(combined):
        return float(combined) <= float(threshold)
    runways = guidance.get('runway') or {}
    values = list(runways.values()) if isinstance(runways, dict) else []
    if not values or any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                         not math.isfinite(value) for value in values):
        return False
    return max(float(value) for value in values) <= float(threshold) / 100.0


def _spillover_runway_view(guidance, providers, source_specific=False):
    """Return the pressure signal appropriate for one tier's actual sources.

    Normal's multi-plan stage can use the combined pool because its adaptive pair
    can move work between subscriptions. Fast has only its configured source, so a
    healthy Kimi plan must not hide an exhausted Ark Auto allowance.
    """
    if source_specific:
        runways = guidance.get('provider_runway') or guidance.get('runway') or {}
        return {provider: runways.get(provider) for provider in providers}
    combined = guidance.get('capacity_pressure_percent', guidance.get('combined_remaining_percent'))
    if (guidance.get('capacity_pressure_percent') is not None or guidance.get('combined_remaining_complete')) and isinstance(combined, (int, float)) and \
            not isinstance(combined, bool) and math.isfinite(combined):
        return {provider: max(0.0, float(combined) / 100.0) for provider in providers}
    return guidance.get('runway') or {}


def _spillover_source(guidance, providers, tier, level2):
    """Choose the runway signal and optional source that fallback may replace.

    A multi-plan Normal stage should continue using a healthy subscription, but
    one healthy plan must not force a depleted peer to keep its full share. In
    that mixed state, fallback replaces only the constrained provider. As reset
    approaches, its reset-aware runway rises and the replacement fades away.
    """
    guidance = guidance if isinstance(guidance, dict) else {}
    providers = set(providers)
    source_specific = tier == 'fast' or level2 or len(providers) == 1
    view = _spillover_runway_view(guidance, providers, source_specific)
    replacement = None
    if not source_specific and len(providers) > 1:
        specific = _spillover_runway_view(guidance, providers, True)
        values = {provider: specific.get(provider) for provider in providers}
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) and
               math.isfinite(value) for value in values.values()):
            threshold = float(guidance.get('runway_threshold_percent') or 0) / 100.0
            constrained = min(values, key=values.get)
            if threshold > 0 and values[constrained] < threshold <= max(values.values()):
                source_specific = True
                view = specific
                replacement = constrained
    values = ([view.get(replacement)] if replacement else
              [view.get(provider) for provider in providers])
    runway = max((float(value) for value in values
                  if isinstance(value, (int, float)) and not isinstance(value, bool) and
                  math.isfinite(value)), default=None)
    return source_specific, view, replacement, runway


def _provider_conservation_level(guidance, source_specific, runway):
    """Return one provider pool's load-shedding level, independent of global capacity."""
    if not source_specific or isinstance(runway, bool) or not isinstance(runway, (int, float)) or \
            not math.isfinite(runway):
        return 0
    threshold = guidance.get('runway_threshold_percent', 0) if isinstance(guidance, dict) else 0
    level2 = guidance.get('level2_runway_percent', 0) if isinstance(guidance, dict) else 0
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) for value in (threshold, level2)):
        return 0
    if level2 > 0 and float(runway) <= float(level2) / 100.0:
        return 2
    return 1 if threshold > 0 and float(runway) < float(threshold) / 100.0 else 0


def _level2_spillover_cap(c, q, spill_config, target_provider, runway=None, now=None):
    """Return a price-aware Level 2 cap that yields to severe plan pressure.

    Off-peak may use the configured high cap throughout Level 2. At peak prices
    the cap starts at the Level 1 ceiling, then rises continuously to the same
    high cap once source-plan runway falls below half the Level 2 threshold.
    """
    base = int(spill_config.get('max_share_percent') or 0)
    if target_provider != 'deepseek':
        return base
    high = spill_config.get('level2_offpeak_share_percent', base)
    floor = spill_config.get('level2_min_balance_cny', 0)
    if isinstance(high, bool) or not isinstance(high, int) or high < base:
        return base
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor):
        return base
    record = q.get('deepseek') if isinstance(q, dict) else None
    if not isinstance(record, dict) or record.get('stale', True) or record.get('available') is not True:
        return base
    balances = [item.get('remaining') for item in record.get('balances') or []
                if isinstance(item, dict) and item.get('currency') == 'CNY' and
                isinstance(item.get('remaining'), (int, float)) and
                not isinstance(item.get('remaining'), bool) and math.isfinite(item.get('remaining'))]
    if not balances or sum(balances) < float(floor):
        return base
    try:
        import economics
        peak = economics.deepseek_peak(time.time() if now is None else now)
    except Exception:
        return base
    high = min(routing.MAX_LEVEL2_SPILLOVER_SHARE, high)
    if not peak:
        return high
    threshold = float(spill_config.get('level2_runway_percent') or 0) / 100.0
    if isinstance(runway, bool) or not isinstance(runway, (int, float)) or \
            not math.isfinite(runway) or threshold <= 0:
        return base
    critical = threshold * 0.5
    progress = min(1.0, max(0.0, (threshold - float(runway)) /
                            max(0.000001, threshold - critical)))
    return int(round(base + (high - base) * progress))


def _dynamic_runway_override(tier, provider_by_profile, q, guidance=None, now=None):
    """Use durable per-pool pressure for adaptive Normal and Deep balancing.

    Kimi's short and weekly counters are shared by K2.8 and K3. A Normal burst
    can therefore tighten the five-hour counter even when K3 caused little of
    it. Balancing follows each provider's durable weekly/monthly fit; immediate
    exhausted windows are still filtered by ``allowed`` before this function.

    A smooth local-pressure penalty is applied below the ordinary runway
    threshold. This lets a healthy peer absorb nearly all same-tier work when
    one subscription pool is severely constrained, without falsely declaring
    the combined high-capability pool degraded. When both providers have the
    same pressure their ratio stays unchanged and global conservation remains
    responsible for lower-tier spillover.
    """
    if tier not in ('background', 'deep'):
        return None
    import economics
    now = time.time() if now is None else now
    providers = set(provider_by_profile.values())
    guidance_runways = ((guidance or {}).get('provider_runway') or
                        (guidance or {}).get('runway')) if isinstance(guidance, dict) else None
    values = {}
    for provider in providers:
        runway = guidance_runways.get(provider) if isinstance(guidance_runways, dict) else None
        if isinstance(runway, bool) or not isinstance(runway, (int, float)) or not math.isfinite(runway):
            fitted = economics._window_fit(provider, q.get(provider) or {}, now)
            runway = number(fitted.get('runway')) if fitted.get('status') == 'ok' else None
        values[provider] = runway if runway is not None and runway >= 0 else None
    threshold = (guidance or {}).get('runway_threshold_percent') if isinstance(guidance, dict) else None
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and \
            math.isfinite(threshold) and threshold > 0:
        threshold = float(threshold) / 100.0
        for provider, runway in values.items():
            if runway is None or runway >= threshold:
                continue
            ratio = max(0.0, min(1.0, runway / threshold))
            smooth = ratio * ratio * (3.0 - 2.0 * ratio)
            values[provider] = runway * smooth
    return values


def _policy_route(t, c, q, stages, batch=None):
    """First stage with an admissible candidate, then a weighted choice within it.

    Never consumes counters here: with a daemon batch the advance is only proposed and
    choose_ready commits it after the owner/scope checks pass, while a preview call
    (batch=None) reads the stored credits without writing them. Within one batch each
    committed proposal moves the credits, so subsequent choices distribute correctly.

    Inside a stage the stored weights are the baseline; quota-aware dynamics scale
    them in memory by each candidate's current valid headroom. The stored policy is
    never rewritten, explicit profiles stay pinned and a running task is never
    migrated.
    """
    preview = None
    first_reason = None
    tier = tier_of(t)
    policy = routing.configured(c)
    spill_config = routing.configured_spillover(c, policy)
    spill_guidance = None
    for index, stage in enumerate(stages):
        candidates = []
        for entry in stage:
            _, ok, why = _allowed_profile(entry['profile'], t, c, q)
            if ok:
                candidates.append((entry, why))
            elif first_reason is None:
                first_reason = why
        if candidates:
            base_entries = [entry for entry, _ in candidates]
            provider_by_profile = {}
            for entry, _ in candidates:
                profile = c['profiles'].get(entry['profile']) or {}
                provider_by_profile[entry['profile']] = str(profile.get('model', '')).split('/', 1)[0]
            level2 = False
            if spill_config and spill_config['enabled'] and index == 0:
                spill_guidance = spill_guidance or tier_guidance(c, q)
                if _level2_conservation_active(tier, spill_config, spill_guidance):
                    fast_candidates, fast_providers, fast_stage_index = _first_available_policy_stage(
                        'fast', t, c, q, policy)
                    if fast_candidates:
                        candidates = fast_candidates
                        base_entries = [entry for entry, _ in candidates]
                        provider_by_profile = fast_providers
                        level2 = True
            adaptive = routing.dynamic_stage(
                c, 'fast' if level2 else tier,
                fast_stage_index if level2 else index, policy)
            if adaptive and spill_guidance is None:
                spill_guidance = tier_guidance(c, q)
            effective_tier = 'fast' if level2 else tier
            entries, dynamic_reason, _ = routing.dynamics(
                base_entries, provider_by_profile, q, adaptive=adaptive,
                runway_override=_dynamic_runway_override(
                    effective_tier, provider_by_profile, q, spill_guidance))
            if level2:
                dynamic_reason = 'quota_conservation_level2:' + dynamic_reason
            # A later fallback may absorb a bounded share before subscriptions hit
            # zero. Explicit profile requests never reach this function, and only
            # stage zero is blended; once a stage is unavailable, normal ordered
            # fallback semantics remain in charge.
            if spill_config and spill_config['enabled'] and index == 0 and tier in spill_config['tiers']:
                target = spill_config['profile']
                target_provider, target_ok, target_reason = _allowed_profile(target, t, c, q)
                source_providers = set(provider_by_profile.values())
                if target_ok and target_provider not in source_providers:
                    spill_guidance = spill_guidance or tier_guidance(c, q)
                    source_specific, runway_view, replacement_provider, source_runway = \
                        _spillover_source(spill_guidance, source_providers, tier, level2)
                    global_level = int(spill_guidance.get('conservation_level') or 0)
                    # A mixed same-tier stage first moves work to its healthy
                    # subscription peer. Cash fallback for only the constrained
                    # member remains a global-conservation action; provider-local
                    # spillover is reserved for a tier whose stage has no peer.
                    source_level = (_provider_conservation_level(
                        spill_guidance, source_specific, source_runway)
                        if replacement_provider is None else 0)
                    active_level = max(global_level, source_level)
                    if active_level > 0 and \
                            (source_specific or spill_guidance.get('quota_posture') == 'fast_preferred'):
                        provider_by_profile[target] = target_provider
                        level2_active = level2 or (
                            source_specific and active_level >= 2)
                        entries, spill_reason, _ = routing.spillover(
                            entries, provider_by_profile, target,
                            runway_view,
                            spill_guidance.get('runway_threshold_percent', 38),
                            spill_config['max_share_percent'],
                            level2_threshold_percent=(spill_guidance.get('level2_runway_percent')
                                                      if level2_active else None),
                            level2_max_share_percent=(_level2_spillover_cap(
                                c, q, spill_config, target_provider, source_runway)
                                if level2_active else None),
                            replace_provider=replacement_provider)
                        if source_level > global_level:
                            dynamic_reason += ':quota_source_level' + str(source_level)
                        dynamic_reason += ':' + spill_reason
                        if any(entry['profile'] == target for entry in entries):
                            candidates.append(({'profile': target, 'weight': 1}, target_reason))
            if batch is not None:
                chosen, credits = routing.advance(entries, batch.credits_for(tier))
                batch.propose(tier, credits)
            else:
                if preview is None:
                    preview = routing.Admissions.load().credits
                chosen, _ = routing.advance(entries, preview.get(tier, {}))
            why = next(reason for entry, reason in candidates if entry['profile'] == chosen)
            return chosen, 'routing_policy:' + tier + ':stage' + str(index) + ':' + why + ':' + dynamic_reason
    return None, 'routing_policy:' + (first_reason or 'no_enabled_candidate')


def route(t, c, q, batch=None):
    """Profile selection for a task. Previews never mutate durable admission counters.

    An explicit requested_profile always pins the task and keeps the existing monthly
    retry behavior. With requested_profile=auto and a configured routing_policy for the
    task's tier, the first stage that has an enabled candidate passing provider
    admission wins and the candidate is picked by weighted round robin. Without a
    policy the legacy single-profile routing is unchanged.
    """
    requested = t.get('requested_profile', 'auto')
    if requested != 'auto':
        profile = _profile_name(t, c)
        if not profile or c['profiles'][profile].get('enabled') is False:
            return None, 'profile_disabled_or_missing'
        if not meets_capability_floor(profile, t, c):
            return None, 'capability_floor_unavailable'
        provider = c['profiles'][profile]['model'].split('/', 1)[0]
        ok, why = allowed(provider, q, t.get('complexity', 'normal'),
                          c.get('kimi_reserve_percent', 0), retry_monthly=True)
        if ok and why == 'explicit_profile_monthly_retry':
            return profile, why
        if ok:
            return profile, 'explicit_profile:' + why
        # Never silently switch profiles or models; the coordinator re-selects deliberately.
        return None, why
    stages = (routing.configured(c) or {}).get(tier_of(t))
    if stages:
        return _policy_route(t, c, q, stages, batch)
    profile = _profile_name(t, c)
    if not profile or c['profiles'][profile].get('enabled') is False:
        return None, 'profile_disabled_or_missing'
    if not meets_capability_floor(profile, t, c):
        return None, 'capability_floor_unavailable'
    if profile in (t.get('excluded_profiles') or []):
        return None, 'routing_guard_excluded_profile'
    provider = c['profiles'][profile]['model'].split('/', 1)[0]
    ok, why = allowed(provider, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0))
    if ok:
        return profile, tier_of(t) + ':' + why
    return None, why


def _unrecovered_provider(provider, kind):
    """Provider a billing failure still discredits in guidance until recovery is recorded.

    Historical evidence can predate this classification (or a circuit may have been
    pruned), so guidance stays conservative read-only: the failed shared provider is not
    offered as a candidate until a cleared circuit records a verified recovery path
    (fresh quota replenishment for a balance block, an explicit manual retry
    authorization, a configured scheduled monthly release, or a verified completed model
    success). This never trips, clears or mutates the circuit.
    """
    if not provider or not kind or billing_block(provider):
        return None
    b = read_json(STATE / BILLING, {}).get(provider)
    if b and b.get('cleared_at'):
        released = b.get('released')
        if released == 'manual_retry':
            return None
        if kind == MONTHLY_REASON and released in (SCHEDULED_RESET_RELEASE, MODEL_SUCCESS_RELEASE):
            return None
        if kind != MONTHLY_REASON and released == 'quota':
            return None
    return provider


def _profile_order(t, c, policy):
    """Profiles in recovery preference order for the selected task tier.

    Without a policy this is exactly the configured profile order, preserving legacy
    guidance. With a policy, only the task's declared stages participate; recovery
    never changes capability tier merely to reach another profile.
    """
    names = list(c.get('profiles', {}))
    if not policy:
        return names
    ordered, seen = [], set()

    def add(name):
        if name in c['profiles'] and name not in seen:
            seen.add(name)
            ordered.append(name)

    tier = tier_of(t)
    for entry in routing.tier_entries(policy, tier):
        add(entry['profile'])
    return ordered


def alternatives(t, c, q, exclude=None):
    """Currently dispatchable profiles for deliberate coordinator re-selection.

    Provider-level billing blocks apply to every profile on that provider, so sibling
    Kimi profiles (K2.8 and K3) can never be alternatives to each other while blocked.
    Ordering follows routing_policy preference when configured; the configured variant
    is carried so a continuation preserves maximum reasoning.
    """
    out = []
    threshold = c.get('kimi_reserve_percent', 0)
    for name in _profile_order(t, c, routing.configured(c)):
        p = c['profiles'][name]
        if p.get('enabled') is False or not p.get('model') or not meets_capability_floor(name, t, c):
            continue
        provider = str(p['model']).split('/', 1)[0]
        if provider == exclude:
            continue
        ok, why = allowed(provider, q, t.get('complexity', 'normal'), threshold)
        if ok:
            out.append({'profile': name, 'model': p['model'], 'provider': provider,
                        'variant': p.get('variant'), 'route': why})
    return out


def _options(t, c, q, blocked_reason, partial_work=False, billing_reason=None, provider=None,
             exclude=None, window=False):
    """Machine-readable recovery that preserves the task tier and bridge routing."""
    if exclude is None:
        exclude = _unrecovered_provider(provider, billing_reason)
    alts = alternatives(t, c, q, exclude=exclude)
    task_tier = 'normal' if tier_of(t) == 'background' else tier_of(t)
    retry_command = 'delegate-opencode quota --retry-provider ' + provider if provider else None
    retry_allowed = bool(provider and not window and billing_reason == MONTHLY_REASON and
                         allowed(provider, q, t.get('complexity', 'normal'),
                                 c.get('kimi_reserve_percent', 0), retry_monthly=True)[0])
    if alts:
        action = ('inspect_partial_work_and_resubmit_same_tier_auto' if partial_work else
                  'resubmit_same_tier_auto')
        autonomous = {
            'action': 'resubmit_same_tier_auto',
            'instruction': ('Continue autonomously: do not ask the user and do not wait for quota while a '
                            'viable alternative exists. Review partial work and submit a continuation with '
                            'the same tier, profile=auto and this failed task as parent. The bridge inherits '
                            'the failed provider exclusion and chooses the next configured stage. Never '
                            'replay prompt text or deployed side effects; carry forward only the remaining '
                            'authorized work.'),
            'tier': task_tier, 'profile': 'auto',
            'requires_user_approval': False, 'wait_for_quota': False,
            'retry_command': None, 'retry_is_proof': False}
    else:
        action = ('retry_or_reselect_after_usage_window' if window else
                  'top_up_provider_account_then_resubmit' if partial_work else
                  'top_up_provider_account_or_wait_for_quota')
        instruction = ('No viable route in this task tier is currently dispatchable. Report this specific '
                       'blockage; do not expect this task to resume automatically.')
        if billing_reason == MONTHLY_REASON and retry_allowed:
            # Waiting on window telemetry cannot prove a monthly cycle recovered.
            action = 'top_up_or_authorize_manual_retry'
            instruction = ('No viable alternative profile is currently dispatchable. The monthly plan is '
                           'exhausted for this billing cycle; K2.8 and K3 share the same provider block, so '
                           'neither is an alternative while it is open. Monthly exhaustion is advisory: a task '
                           'may still be submitted with this provider as an explicit requested_profile to try, '
                           'and a verified successful reply clears the flag. Do not infer a monthly reset from '
                           'usage-window telemetry or reported reset timestamps; the configured schedule or a '
                           'local retry authorization' + (' (' + retry_command + ')' if retry_command else '') +
                           ' can release the block for new attempts.')
        autonomous = {'action': action, 'instruction': instruction,
                      'requires_user_approval': False,
                      'wait_for_quota': False if window else not retry_allowed,
                      'retry_command': retry_command, 'retry_is_proof': False}
    out = {'blocked_reason': blocked_reason, 'billing_reason': billing_reason,
           'explicit_retry_allowed': retry_allowed,
           'blocked_provider': provider, 'suggested_action': action, 'alternatives': alts,
           'automatic_fallback': False, 'autonomous_reselection': bool(alts),
           'autonomous_next_action': autonomous,
           'note': ('automatic_fallback=false means no automatic transition occurred for this terminal task; '
                    'autonomous_reselection=' + ('true' if alts else 'false') + '. ' +
                    ('Continue the same tier with profile=auto without asking or waiting.' if alts else
                     'Report the blockage; no route in this tier is dispatchable right now.') +
                    ' Original prompts are never replayed; a continuation lets the bridge choose the '
                    'next stage while preserving prior evidence.')}
    if window:
        # Explicit short-window exhaustion: never a durable circuit, never a monthly
        # block, and never a guessed recovery time. It is coordinator guidance only.
        out['usage_window_exhausted'] = True
        out['note'] = ('automatic_fallback=false means no automatic transition occurred for this terminal task. '
                       'The provider reported an explicit usage-window exhaustion, which is not a durable '
                       'billing block and not a monthly plan block; no reset time is inferred and the '
                       'provider is not blocked permanently. ' +
                       ('Continue the same tier with profile=auto and the failed task as parent.' if alts else
                        'Report the blockage and retry later or with another provider.'))
    return out


def recovery(t, c, q):
    """Blockage guidance for a queued task; None when routing currently succeeds."""
    profile, why = route(t, c, q)
    if profile:
        return None
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, why, billing_reason=_active_reason(provider), provider=provider)


def billing_failure_recovery(t, c, q):
    """Guidance for a task that failed on an unequivocal provider billing error."""
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, 'provider_billing_error', partial_work=True,
                    billing_reason=_task_billing_kind(t), provider=provider)


def _task_window_exhausted(t):
    """True only for explicit short usage-window exhaustion recorded on the task."""
    if str(t.get('reason') or '') == 'provider_usage_window_limit':
        return True
    return any(isinstance(e, dict) and e.get('source') == 'model' and e.get('usage_window')
               for e in t.get('errors') or [])


def window_failure_recovery(t, c, q):
    """Guidance for a task stopped by an explicit usage-window exhaustion.

    The failed provider is excluded from the offered candidates for this guidance only
    (a sibling profile on the same provider shares the window), no circuit is tripped
    and no reset time is inferred, so the provider is never blocked permanently.
    """
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, 'provider_usage_window_limit', partial_work=True,
                    billing_reason=None, provider=provider, exclude=provider, window=True)


def routing_status(c, q):
    """Read-only effective dynamic admission shares per same-capability stage.

    Safe console metadata only: base/effective weights and shares plus the
    reason; never quota secrets, credential material or raw endpoint data. The
    stored policy is never read as telemetry and never rewritten here.
    """
    policy = routing.configured(c)
    if not policy:
        return None
    spill_config = routing.configured_spillover(c, policy)
    spill_guidance = tier_guidance(c, q) if spill_config and spill_config['enabled'] else None
    out = {}
    for tier in routing.TIERS:
        stages = policy.get(tier)
        if not stages:
            continue
        stage_views = []
        for index, stage in enumerate(stages):
            candidates = []
            for entry in stage:
                profile = c.get('profiles', {}).get(entry['profile'])
                if not isinstance(profile, dict) or profile.get('enabled') is False:
                    continue
                provider = str(profile.get('model', '')).split('/', 1)[0]
                complexity = 'deep' if tier == 'deep' else 'normal'
                ok, _ = allowed(provider, q, complexity, c.get('kimi_reserve_percent', 0))
                if ok:
                    candidates.append(entry)
            level2 = False
            if spill_config and spill_config['enabled'] and index == 0 and \
                    _level2_conservation_active(tier, spill_config, spill_guidance):
                fast_candidates, _, fast_stage_index = _first_available_policy_stage(
                    'fast', {'complexity': 'normal'}, c, q, policy)
                if fast_candidates:
                    candidates = [entry for entry, _ in fast_candidates]
                    level2 = True
            provider_by_profile = {}
            for entry in candidates:
                profile = c['profiles'].get(entry['profile']) or {}
                provider_by_profile[entry['profile']] = str(profile.get('model', '')).split('/', 1)[0]
            adaptive = routing.dynamic_stage(
                c, 'fast' if level2 else tier,
                fast_stage_index if level2 else index, policy)
            if adaptive and spill_guidance is None:
                spill_guidance = tier_guidance(c, q)
            effective_tier = 'fast' if level2 else tier
            effective, reason, info = routing.dynamics(
                candidates, provider_by_profile, q, adaptive=adaptive,
                runway_override=_dynamic_runway_override(
                    effective_tier, provider_by_profile, q, spill_guidance))
            if level2:
                reason = 'quota_conservation_level2:' + reason
            source_providers = set(provider_by_profile.values())
            source_specific, runway_view, replacement_provider, source_runway = \
                _spillover_source(spill_guidance, source_providers, tier, level2)
            global_level = int((spill_guidance or {}).get('conservation_level') or 0)
            source_level = (_provider_conservation_level(
                spill_guidance, source_specific, source_runway)
                if replacement_provider is None else 0)
            active_level = max(global_level, source_level)
            if spill_config and index == 0 and tier in spill_config['tiers'] and \
                    active_level > 0 and \
                    (source_specific or spill_guidance.get('quota_posture') == 'fast_preferred'):
                target = spill_config['profile']
                target_profile = c.get('profiles', {}).get(target) or {}
                target_provider = str(target_profile.get('model', '')).split('/', 1)[0]
                complexity = 'deep' if tier == 'deep' else 'normal'
                target_ok, _ = allowed(target_provider, q, complexity, c.get('kimi_reserve_percent', 0))
                if target_ok and target_provider not in set(provider_by_profile.values()):
                    provider_by_profile[target] = target_provider
                    level2_active = level2 or (
                        source_specific and active_level >= 2)
                    effective, spill_reason, spill_info = routing.spillover(
                        effective, provider_by_profile, target,
                        runway_view,
                        spill_guidance.get('runway_threshold_percent', 38),
                        spill_config['max_share_percent'],
                        level2_threshold_percent=(spill_guidance.get('level2_runway_percent')
                                                  if level2_active else None),
                        level2_max_share_percent=(_level2_spillover_cap(
                            c, q, spill_config, target_provider, source_runway)
                            if level2_active else None),
                        replace_provider=replacement_provider)
                    if spill_info:
                        if source_level > global_level:
                            reason += ':quota_source_level' + str(source_level)
                        reason += ':' + spill_reason
                        info = spill_info
            if info:
                stage_views.append({'reason': reason, 'members': info})
        if stage_views:
            out[tier] = stage_views
    return out or None


def _control_policy(c):
    import hashlib
    import json
    fields = ('profiles', 'routing_policy', 'routing', 'routing_dynamics', 'budget_signals', 'quota_spillover', 'fast_bias_runway_percent')
    # Bump when the meaning of the persisted conservation level changes. Version 2
    # separates renewable short-window pressure from durable plan capacity, so an
    # old level derived from the former mixed signal must not remain latched.
    payload = {'controller_schema': 4, **{k: c.get(k) for k in fields}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def observe_load(c, q, now=None):
    """One writer at telemetry refresh; previews never move controller state."""
    import capacity
    now = time.time() if now is None else now
    raw = tier_guidance(c, q, _raw=True)
    previous = read_json(STATE / 'load-control.json', {})
    signature = _control_policy(c)
    if previous.get('policy') != signature:
        previous = {}
    observation = _plan_window_observation(c, q)
    replenished = _observed_replenishment(previous.get('quota_observation'), observation)
    state = capacity.transition(raw['conservation_level'], raw['capacity_pressure_percent'],
                                [raw['runway_threshold_percent'], raw['level2_runway_percent']],
                                previous, now, replenished)
    state['level_since'] = previous.get('level_since', now) \
        if previous.get('level') == state['level'] else now
    state['policy'] = signature
    state['quota_observation'] = observation
    state['replenished_at'] = now if replenished else previous.get('replenished_at')
    write_json(STATE / 'load-control.json', state)
    return state


def _plan_window_observation(c, q):
    """Return the persisted, non-secret counters used to prove a real refill."""
    providers = set()
    policy = routing.configured(c)
    spill = routing.configured_spillover(c, policy)
    spill_model = ((c.get('profiles') or {}).get((spill or {}).get('profile')) or {}).get('model')
    spill_provider = spill_model.split('/', 1)[0] if isinstance(spill_model, str) and '/' in spill_model else None
    for tier in ('fast', 'background'):
        for stage in (policy or {}).get(tier) or []:
            for entry in stage:
                model = ((c.get('profiles') or {}).get(entry.get('profile')) or {}).get('model')
                if isinstance(model, str) and '/' in model:
                    provider = model.split('/', 1)[0]
                    if provider != spill_provider:
                        providers.add(provider)
    result = {}
    for provider in providers:
        record = q.get(provider) or {}
        if record.get('stale', True) or record.get('state') != 'ok':
            continue
        for index, window in enumerate(record.get('windows') or []):
            if not isinstance(window, dict) or window.get('valid', True) is not True:
                continue
            value = window.get('remaining_percent')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            name = str(window.get('name') or f'window-{index}')
            duration = window.get('duration_minutes')
            key = f'{provider}|{name}|{duration}'
            result[key] = {'remaining_percent': float(value), 'resets_at': window.get('resets_at')}
    return result


def _observed_replenishment(previous, current):
    """True only when an authoritative quota counter has actually increased."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    for key, value in current.items():
        old = previous.get(key)
        if not isinstance(old, dict) or not isinstance(value, dict):
            continue
        before, after = old.get('remaining_percent'), value.get('remaining_percent')
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (before, after)) and float(after) >= float(before) + 0.5:
            return True
    return False


def tier_guidance(c, q, _raw=False):
    """Quota-aware tie-breaker for work that is genuinely eligible for Fast or Normal.

    This never changes a submitted tier and must never downgrade work that needs Normal.
    It only tells the coordinator when the configured Fast plan deserves more of the
    overlap: particularly when the preferred Normal pool has lost a provider or all
    Normal alternatives to the Fast provider are also below the configured window floor.
    """
    threshold_base = c.get('fast_bias_runway_percent', 38)
    if isinstance(threshold_base, bool) or not isinstance(threshold_base, (int, float)):
        threshold_base = 38
    threshold_base = max(0.0, min(400.0, float(threshold_base)))
    now = time.time()
    policy = routing.configured(c)

    spill_config = routing.configured_spillover(c, policy)
    level2_base = (spill_config or {}).get('level2_runway_percent', 0)
    if isinstance(level2_base, bool) or not isinstance(level2_base, (int, float)):
        level2_base = 0
    level2_base = max(0.0, min(100.0, float(level2_base)))

    # Shared, provider-neutral burn forecasts supply effective refills. The UI
    # pool percentage and conservation pressure use each subscription's durable
    # weekly/monthly window. Short rolling windows remain authoritative for
    # immediate admission and per-stage balancing, but a burst inside a renewable
    # five-hour window must not make an otherwise full weekly plan look depleted.
    configured_provider_ids = set()
    for tier_name in ('fast', 'background'):
        for stage in (policy or {}).get(tier_name) or []:
            for entry in stage:
                model = ((c.get('profiles') or {}).get(entry.get('profile')) or {}).get('model')
                if isinstance(model, str) and '/' in model:
                    configured_provider_ids.add(model.split('/', 1)[0])
    pool_fit = None
    spill_provider = str(((c.get('profiles') or {}).get((spill_config or {}).get('profile')) or {}).get('model', '')).split('/')[0]
    plan_provider_ids = {p for p in configured_provider_ids if (q.get(p) or {}).get('windows') and p != spill_provider}
    durable_fits = {}
    if plan_provider_ids:
        import economics
        candidate = economics.work_pool(c, q, now=now, include_payg_balance=False)
        if candidate.get('complete') and isinstance(candidate.get('remaining_percent'), (int, float)):
            pool_fit = candidate
        durable_fits = {provider: economics._window_fit(provider, q.get(provider) or {}, now)
                        for provider in plan_provider_ids}
    refill = (pool_fit.get('refills') or [None])[0] if pool_fit else None
    relief = 0.0
    if refill and isinstance(refill.get('hours_until'), (int, float)) and \
            isinstance(refill.get('projected_remaining_percent'), (int, float)):
        current = float(pool_fit['remaining_percent'])
        gain = max(0.0, float(refill['projected_remaining_percent']) - current)
        imminence = max(0.0, min(1.0, 1.0 - float(refill['hours_until']) / 24.0))
        relief = imminence * max(0.0, min(1.0, gain / 30.0))
    threshold_percent = threshold_base * (1.0 - 0.30 * relief)
    level2_threshold = level2_base * (1.0 - 0.30 * relief)
    threshold = threshold_percent / 100.0

    def configured_stages(tier):
        if policy and policy.get(tier):
            return policy[tier]
        name = (c.get('routing') or {}).get(tier)
        return [[{'profile': name, 'weight': 1}]] if isinstance(name, str) else []

    def provider_for(entry):
        profile = (c.get('profiles') or {}).get(entry.get('profile'))
        if not isinstance(profile, dict) or profile.get('enabled') is False:
            return None
        model = profile.get('model')
        return model.split('/', 1)[0] if isinstance(model, str) and '/' in model else None

    def stage_state(tier):
        for index, stage in enumerate(configured_stages(tier)):
            configured = []
            available = []
            for entry in stage:
                provider = provider_for(entry)
                if not provider:
                    continue
                if provider not in configured:
                    configured.append(provider)
                ok, _ = allowed(provider, q, 'normal', c.get('kimi_reserve_percent', 0))
                if ok and provider not in available:
                    available.append(provider)
            if available:
                return {'index': index, 'configured': configured, 'available': available}
        return {'index': None, 'configured': [], 'available': []}

    def remaining(provider):
        durable = durable_fits.get(provider)
        if durable and isinstance(durable.get('source_amount'), (int, float)):
            return float(durable['source_amount'])
        values = []
        for window in (q.get(provider) or {}).get('windows') or []:
            if not isinstance(window, dict):
                continue
            value = window.get('remaining_percent')
            if window.get('valid', True) and isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(value):
                values.append(float(value))
        return min(values) if values else None

    budget_settings = normalized_budget_signals(c.get('budget_signals'), c.get('profiles'))

    def signal(provider):
        setting = budget_settings.get(provider) or {}
        mode = setting.get('mode', 'telemetry')
        if mode == 'ignore':
            return {'mode': mode, 'value': None, 'low': None}
        if mode == 'manual_window':
            window = {'valid': True, 'remaining_percent': setting['remaining_percent'],
                      'duration_minutes': setting['duration_minutes'], 'resets_at': setting['resets_at']}
            value = routing._window_runway(window, now)
            return {'mode': mode, 'value': value, 'low': value <= threshold if value is not None else None,
                    'remaining_percent': setting['remaining_percent'], 'resets_at': setting['resets_at']}
        if mode == 'monetary':
            amount = setting.get('manual_balance')
            source = 'manual'
            if amount is None:
                source = 'telemetry'
                matches = [item.get('remaining') for item in (q.get(provider) or {}).get('balances') or []
                           if isinstance(item, dict) and item.get('currency') == setting['currency'] and
                           isinstance(item.get('remaining'), (int, float)) and
                           not isinstance(item.get('remaining'), bool)]
                amount = sum(matches) if matches else None
            low = amount <= setting['low_balance'] if amount is not None else None
            return {'mode': mode, 'value': amount, 'low': low, 'source': source,
                    'currency': setting['currency'], 'low_balance': setting['low_balance']}
        value = routing._runway(q.get(provider), now)
        return {'mode': 'telemetry', 'value': value,
                'low': value <= threshold if value is not None else None}

    fast = stage_state('fast')
    normal = stage_state('background')
    fast_providers = set(fast['available'])
    normal_providers = set(normal['available'])
    overlap = fast_providers & normal_providers
    alternate = normal_providers - fast_providers
    providers = sorted(fast_providers | normal_providers)
    configured_providers = sorted(set(fast['configured']) | set(normal['configured']) | configured_provider_ids)
    readings = {provider: remaining(provider) for provider in configured_providers}
    signals = {provider: signal(provider) for provider in configured_providers}
    runways = {provider: item['value'] if item['mode'] in ('telemetry', 'manual_window') else None
               for provider, item in signals.items()}
    combined_remaining = pool_fit.get('remaining_percent') if pool_fit else None
    combined_complete = bool(pool_fit)
    plan_signals = {
        provider: (fit.get('runway') if fit.get('status') == 'ok' and not fit.get('stale') else None)
        for provider, fit in durable_fits.items()
    }
    # Fresh authoritative zero is a valid capacity observation despite unavailability.
    for p in plan_provider_ids:
        record = q.get(p) or {}
        if not record.get('stale', True) and record.get('state') == 'ok' and record.get('available') is False:
            plan_signals[p] = 0.0
    reliable = bool(plan_signals) and all(v is not None for v in plan_signals.values())
    provider_runways = {
        provider: plan_signals.get(provider, runways.get(provider))
        for provider in configured_providers
    }
    provider_levels = {
        provider: _provider_conservation_level(
            {'runway_threshold_percent': threshold_percent,
             'level2_runway_percent': level2_threshold}, True, runway)
        for provider, runway in provider_runways.items()
    }
    pressure_percent = max(plan_signals.values()) * 100 if reliable else None
    # Forecast refills are display-only. They must never release conservation
    # before the authoritative provider telemetry actually reports a top-up.
    refill_safe = False
    lost_normal = set(normal['configured']) - normal_providers
    fast_low = bool(overlap) and any(signals.get(provider, {}).get('low') is True for provider in overlap)
    alternates_healthy = any(signals.get(provider, {}).get('low') is False for provider in alternate)
    alternates_low = bool(alternate) and all(signals.get(provider, {}).get('low') is True
                                             for provider in alternate)
    combined_low = not refill_safe and pressure_percent is not None and pressure_percent <= threshold_percent
    fast_bias = threshold > 0 and bool(overlap) and not alternates_healthy and \
        (fast_low or alternates_low or combined_low)
    if fast_bias and lost_normal and fast_low:
        reason = 'normal_pool_provider_unavailable'
    elif fast_bias and alternates_low:
        reason = 'normal_alternative_windows_low'
    elif fast_bias:
        reason = 'shared_fast_plan_window_low'
    elif overlap and alternates_healthy:
        reason = 'healthier_normal_plan_available'
    else:
        reason = 'no_fast_bias'
    posture = ('fast_preferred' if fast_bias else
               'normal_flexible' if reason == 'healthier_normal_plan_available' else 'neutral')
    level2_low = False
    if spill_config and spill_config.get('enabled') and 'background' in spill_config.get('tiers', ()) and \
            combined_low and posture == 'fast_preferred' and level2_threshold > 0:
        if pressure_percent is not None:
            level2_low = pressure_percent <= level2_threshold
        else:
            values = list(runways.values())
            level2_low = bool(values) and all(isinstance(value, (int, float)) and
                                               not isinstance(value, bool) and math.isfinite(value)
                                               for value in values) and \
                max(float(value) for value in values) <= level2_threshold / 100.0
    conservation_level = (2 if level2_low else
                          1 if spill_config and spill_config.get('enabled') and
                          posture == 'fast_preferred' and combined_low else 0)
    raw_level = conservation_level
    if not _raw:
        control = read_json(STATE / 'load-control.json', {})
        if control.get('policy') == _control_policy(c) and reliable:
            held = int(control.get('level', raw_level))
            if held > raw_level:
                conservation_level = held
                posture, fast_bias = 'fast_preferred', True
    return {'prefer_fast_when_both_fit': fast_bias, 'quota_posture': posture, 'reason': reason,
            'conservation_level': conservation_level,
            'raw_conservation_level': raw_level,
            'capacity_pressure_percent': pressure_percent,
            'capacity_confidence': ('observed' if reliable and all(
                durable_fits.get(p, {}).get('fit_source') == 'observed_burn'
                for p in plan_provider_ids if (q.get(p) or {}).get('available') is True)
                else 'prior' if reliable else 'unknown'),
            'runway_threshold_percent': threshold_percent,
            'runway_threshold_base_percent': threshold_base,
            'level2_runway_percent': level2_threshold,
            'level2_runway_base_percent': level2_base,
            'threshold_refill_relief': relief,
            'next_refill': refill,
            'conservation_refill_safe': refill_safe,
            'fast_stage': fast,
            'normal_stage': normal, 'remaining_percent': readings, 'runway': runways,
            'provider_runway': provider_runways,
            'provider_conservation_level': provider_levels,
            'combined_remaining_percent': combined_remaining,
            'combined_remaining_complete': combined_complete,
            'budget_signals': signals,
            'rule': ('Quota is only a tie-breaker. fast_preferred leans Fast when both tiers fit; '
                     'normal_flexible permits Fast or Normal based on error/rework risk while a '
                     'Normal-only subscription is healthy; neutral means classify by task needs.')}


def compact_guidance(g):
    """Coordinator contract: classify the work; keep capacity routing internal."""
    return {
        'instruction': (
            'Choose Fast, Normal or Deep only from the unresolved uncertainty in the task. '
            'Keep profile=auto. The bridge privately owns provider balancing, conservation, '
            'fallback and same-session continuation.'
        )
    }


def guidance(t, c, q):
    """Read-only recovery options for a queued blockage or failed task.

    Never mutates the billing circuit: historical compact billing evidence informs
    guidance only, and a monthly failure stays sticky without any live mutation. An
    explicit short usage-window exhaustion gets policy-ordered alternatives without a
    durable block. Queued tasks get live quota blockage options; other statuses are
    never marked blocked by unrelated quota state.
    """
    billing_kind = None
    reason = str(t.get('reason') or '')
    if reason.startswith('provider_billing_'):
        billing_kind = reason[len('provider_billing_'):] or GENERIC_REASON
    else:
        for e in t.get('errors') or []:
            if isinstance(e, dict) and e.get('source') == 'model':
                billing_kind = _compact_billing_kind(e)
                if billing_kind:
                    break
    if billing_kind and t.get('status') in ('failed', 'needs_attention'):
        return billing_failure_recovery(t, c, q)
    if _task_window_exhausted(t) and t.get('status') in ('failed', 'needs_attention'):
        return window_failure_recovery(t, c, q)
    if t.get('status') == 'queued':
        return recovery(t, c, q)
    return None
