"""Read-only account telemetry and provider billing circuit; selection stays with the coordinator."""
import math
from datetime import datetime, timezone
import time
from concurrent.futures import ThreadPoolExecutor
from common import config, STATE, HttpFailure, auth_key, credential_identity, locked, read_json, redact, request, write_json

ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/user/balance',
    'kimi-for-coding': 'https://api.kimi.com/coding/v1/usages',
}


BILLING = 'billing.json'


def trip(provider, message='', message_id=None, occurred_at=None):
    """Open the provider billing circuit on an unequivocal model billing error.

    Durable and credential-aware: the block binds to the current credential. Each seen
    message ID is retained without eviction, and an occurrence watermark at recovery
    ignores errors predating it, so an old session error can never reopen a recovered
    circuit.
    """
    ident = credential_identity(provider)
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider, {})
        ids = b.get('message_ids', [])
        if message_id and message_id in ids:
            return  # Already recorded; never relatch from a historical error.
        if occurred_at and b.get('cleared_at') and occurred_at <= b['cleared_at']:
            return  # Predates the recovery watermark.
        if message_id:
            ids = ids + [message_id]
        blocks[provider] = {'credential': ident, 'opened_at': time.time(),
                            'message': redact(str(message))[:400], 'message_ids': ids}
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


def clear(provider, sampled_since=None, identity=None):
    """Close the circuit after a fresh positive account query; keep seen message IDs.

    sampled_since/identity guard the race where the account query started before a
    concurrent trip: a stale in-flight sample must never clear a newer circuit.
    """
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        if identity is not None and b.get('credential') is not None and b['credential'] != identity:
            return False
        if sampled_since is not None and b.get('opened_at', 0) > sampled_since:
            return False
        blocks[provider] = {'credential': b.get('credential'), 'cleared_at': time.time(),
                            'message_ids': b.get('message_ids', [])}
        write_json(STATE / BILLING, blocks)
        return True


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
        minutes = duration * units[w['timeUnit']] if duration is not None and w.get('timeUnit') in units else None
        windows.append(window('window_' + str(i), row.get('detail') or {}, minutes))
    if isinstance(body.get('usage'), dict):
        windows.append(window('overall', body['usage']))
    # Do not guess the units of usages.*.used_ratio, or turn missing fields into 0%.
    valid = [w for w in windows if w['valid']]
    return {'windows': windows, 'available': False if any(w['remaining'] == 0 for w in valid) else
            (True if windows and len(valid) == len(windows) else None), 'balances': []}


def fetch_one(provider):
    now = time.time()
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


def refresh(force=False):
    with locked('quota'):
        old = read_json(STATE / 'quota.json', {})
        now = time.time()
        providers = [p for p in ENDPOINTS if any(v['model'].split('/', 1)[0] == p for v in config()['profiles'].values())]
        identities = {p: credential_identity(p) for p in providers}
        same = all(old.get(p, {}).get('_credential') == identities[p] for p in providers)
        recent = old and all(now - old.get(p, {}).get('checked_at', 0) < 60 for p in providers)
        if not force and same and recent:
            return view(old)
        result = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            for p, value in zip(providers, pool.map(fetch_one, providers)):
                last = old.get(p, {}) if identities[p] == old.get(p, {}).get('_credential') else {}
                if value['state'] not in ('ok', 'auth_error') and last.get('sampled_at'):
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
        block = billing_block(p)
        if block:
            # Overlay the active circuit so a stale positive cache is never advertised.
            # The credential hash stays private; only redacted, actionable fields show.
            v.update(available=False, state='billing_blocked',
                     billing={'opened_at': block.get('opened_at'), 'message': block.get('message')})
        out[p] = v
    return out


def allowed(provider, q, complexity, threshold=0):
    v = q.get(provider, {})
    if billing_block(provider):
        # An observed billing failure overrides even a fresh cached positive sample.
        return False, 'provider_billing_blocked'
    if v.get('state') == 'auth_error':
        return False, 'credential_rejected'
    if v.get('state') == 'rate_limited':
        return False, 'quota_endpoint_rate_limited'
    fresh_enough = time.time() - v.get('sampled_at', 0) <= 900
    if fresh_enough and v.get('available') is False:
        return False, 'quota_exhausted_or_account_unavailable'
    if provider == 'kimi-for-coding' and fresh_enough:
        pcts = [w['remaining_percent'] for w in v.get('windows', []) if w.get('remaining_percent') is not None]
        if pcts and min(pcts) < threshold and complexity != 'deep':
            return False, 'reserve_kimi_for_complex_work'
    return True, 'quota_unknown' if v.get('stale', True) or v.get('available') is None else 'quota_available'


def route(t, c, q):
    requested = t.get('requested_profile', 'auto')
    routing = c.get('routing', {'fast': 'fast-code', 'background': 'senior-code', 'deep': 'deep-research'})
    tier = 'deep' if t.get('complexity') == 'deep' else 'fast' if t.get('urgency') == 'fast' else 'background'
    profile = requested if requested != 'auto' else routing[tier]
    if profile not in c['profiles'] or c['profiles'][profile].get('enabled') is False:
        return None, 'profile_disabled_or_missing'
    provider = c['profiles'][profile]['model'].split('/', 1)[0]
    ok, why = allowed(provider, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0))
    if ok:
        return profile, ('explicit_profile' if requested != 'auto' else tier) + ':' + why
    # Never silently switch profiles or models; the coordinator re-selects deliberately.
    return None, why


def alternatives(t, c, q):
    """Currently dispatchable profiles for deliberate coordinator re-selection."""
    out = []
    threshold = c.get('kimi_reserve_percent', 0)
    for name, p in c['profiles'].items():
        if p.get('enabled') is False or not p.get('model'):
            continue
        ok, why = allowed(p['model'].split('/', 1)[0], q, t.get('complexity', 'normal'), threshold)
        if ok:
            out.append({'profile': name, 'model': p['model'], 'route': why})
    return out


def _options(t, c, q, blocked_reason, partial_work=False):
    """Machine-readable blockage options. Codex autonomously re-selects a viable
    alternative and continues; topping up is the fallback only when none exist."""
    alts = alternatives(t, c, q)
    if alts:
        action = 'inspect_partial_work_and_reselect_profile' if partial_work else 'reselect_profile'
    else:
        action = 'top_up_provider_account_then_resubmit' if partial_work else \
            'top_up_provider_account_or_wait_for_quota'
    return {'blocked_reason': blocked_reason, 'suggested_action': action,
            'alternatives': alts, 'automatic_fallback': False,
            'note': 'Profiles are pinned and prompts are never replayed; submit a new task '
                    'with an explicit alternative profile to continue authorized work.'}


def recovery(t, c, q):
    """Blockage guidance for a queued task; None when routing currently succeeds."""
    profile, why = route(t, c, q)
    if profile:
        return None
    return _options(t, c, q, why)


def billing_failure_recovery(t, c, q):
    """Guidance for a task that failed on an unequivocal provider billing error."""
    return _options(t, c, q, 'provider_billing_error', partial_work=True)


def guidance(t, c, q):
    """Read-only recovery options for a queued blockage or billing-failed task.

    Never mutates the billing circuit: historical compact 402 evidence informs guidance
    only. Queued tasks get live quota blockage options; other statuses are never marked
    blocked by unrelated quota state.
    """
    errors = t.get('errors') or []
    failed_billing = t.get('reason') == 'provider_billing_insufficient_balance' or any(
        isinstance(e, dict) and e.get('source') == 'model' and
        (e.get('billing') or e.get('http_status') == 402 or
         'insufficient balance' in str(e.get('message', '')).lower()) for e in errors)
    if failed_billing and t.get('status') in ('failed', 'needs_attention'):
        return billing_failure_recovery(t, c, q)
    if t.get('status') == 'queued':
        return recovery(t, c, q)
    return None
