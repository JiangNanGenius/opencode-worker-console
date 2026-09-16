"""Read-only account telemetry; capability selection remains with Astra."""
import math
from datetime import datetime, timezone
import time
from concurrent.futures import ThreadPoolExecutor
from common import config, STATE, HttpFailure, auth_key, credential_identity, locked, read_json, request, write_json

ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/user/balance',
    'kimi-for-coding': 'https://api.kimi.com/coding/v1/usages',
}


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
        write_json(STATE / 'quota.json', result)
        return view(result)


def view(values):
    out = {}
    for p, v in values.items():
        v = {k: x for k, x in v.items() if not k.startswith('_')}
        v['stale'] = v.get('state') != 'ok' or time.time() - v.get('sampled_at', 0) > 900
        out[p] = v
    return out


def allowed(provider, q, complexity, threshold=20):
    v = q.get(provider, {})
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
    requested = t['requested_profile']
    routing = c.get('routing', {'fast': 'fast-code', 'background': 'senior-code', 'deep': 'deep-research'})
    tier = 'deep' if t['complexity'] == 'deep' else 'fast' if t['urgency'] == 'fast' else 'background'
    profile = requested if requested != 'auto' else routing[tier]
    if profile not in c['profiles'] or c['profiles'][profile].get('enabled') is False:
        return None, 'profile_disabled_or_missing'
    provider = c['profiles'][profile]['model'].split('/', 1)[0]
    ok, why = allowed(provider, q, t['complexity'], c.get('kimi_reserve_percent', 20))
    if ok:
        return profile, ('explicit_profile' if requested != 'auto' else tier) + ':' + why
    if requested == 'auto' and tier != 'deep':
        for fallback in dict.fromkeys([routing['fast'], routing['background']]):
            other = c['profiles'].get(fallback, {})
            if fallback == profile or other.get('enabled') is False or not other.get('model'):
                continue
            yes, reason = allowed(other['model'].split('/', 1)[0], q, t['complexity'], c.get('kimi_reserve_percent', 20))
            if yes:
                return fallback, 'quota_fallback:' + why + ':' + reason
    return None, why
