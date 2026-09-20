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
        windows.append(window('overall', body['usage']))
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
            return view(old)
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
        return view(result)


def view(values):
    out = {}
    for p, v in values.items():
        v = {k: x for k, x in v.items() if not k.startswith('_')}
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


def _allowed_profile(name, t, c, q):
    """(provider, ok, why) for one enabled profile, respecting quota and circuits."""
    p = c.get('profiles', {}).get(name)
    if not isinstance(p, dict) or p.get('enabled') is False or not p.get('model'):
        return None, False, 'profile_disabled_or_missing'
    provider = str(p['model']).split('/', 1)[0]
    if provider in (t.get('excluded_providers') or []):
        return provider, False, 'continuation_excluded_provider'
    ok, why = allowed(provider, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0))
    return provider, ok, why


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
            adaptive = routing.dynamic_stage(c, tier, index)
            entries, dynamic_reason, _ = routing.dynamics(
                base_entries, provider_by_profile, q, adaptive=adaptive)
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
        if p.get('enabled') is False or not p.get('model'):
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
           'note': ('automatic_fallback=false means the bridge never switches or replays by itself; '
                    'autonomous_reselection=' + ('true' if alts else 'false') + '. ' +
                    ('Continue the same tier with profile=auto without asking or waiting.' if alts else
                     'Report the blockage; no route in this tier is dispatchable right now.') +
                    ' Running tasks are pinned and prompts are never replayed; a continuation lets the '
                    'bridge choose the next stage.')}
    if window:
        # Explicit short-window exhaustion: never a durable circuit, never a monthly
        # block, and never a guessed recovery time. It is coordinator guidance only.
        out['usage_window_exhausted'] = True
        out['note'] = ('automatic_fallback=false means the bridge never switches or replays by itself. '
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
            provider_by_profile = {}
            for entry in candidates:
                profile = c['profiles'].get(entry['profile']) or {}
                provider_by_profile[entry['profile']] = str(profile.get('model', '')).split('/', 1)[0]
            adaptive = routing.dynamic_stage(c, tier, index, policy)
            _, reason, info = routing.dynamics(
                candidates, provider_by_profile, q, adaptive=adaptive)
            if info:
                stage_views.append({'reason': reason, 'members': info})
        if stage_views:
            out[tier] = stage_views
    return out or None


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
