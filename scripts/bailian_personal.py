"""Opt-in, read-only Personal Token Plan console telemetry.

The separately approved console session lives only in macOS Keychain. A sample
is usable only for the bound subscription and the current single inference key;
console authentication failure never declares that inference key invalid.
"""
import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import bailian_pool
import common
import keychain_secrets
import providers

SOURCE = 'personal_console'
_PREFIX = 'zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/'
SUBSCRIPTION, USAGE, ADDON = (_PREFIX + suffix for suffix in
                            ('subscription', 'usage', 'addon/summary'))
_APIS = frozenset((SUBSCRIPTION, USAGE, ADDON))
_URL = 'https://bailian-cs.console.aliyun.com/data/api.json'
_BODY_LIMIT = 1024 * 1024
_ERROR = 'Personal Token Plan quota is unavailable.'


class _QuotaError(Exception):
    def __init__(self, auth=False):
        self.auth = auth
        super().__init__(_ERROR)


def _config(config):
    return common.config() if config is None else config


def _reference(config):
    if not isinstance(config, dict) or config.get('bailian_plan_edition') != 'personal':
        return None
    block = config.get('bailian_personal_quota')
    if not isinstance(block, dict) or block.get('enabled') is not True:
        return None
    if set(block) != {'enabled', 'keychain'}:
        return None
    ref = block['keychain']
    if not isinstance(ref, dict) or set(ref) != {'service', 'account'}:
        return None
    if any(not isinstance(ref[k], str) or not 1 <= len(ref[k]) <= 255 or
           any(ord(c) < 32 or ord(c) == 127 for c in ref[k])
           for k in ('service', 'account')):
        return None
    return ref


def credential_identity(config=None):
    """Private opaque metadata identity; never opens a key or console session."""
    try:
        config = _config(config)
        if not isinstance(config, dict) or config.get('bailian_plan_edition') != 'personal':
            return None
        settings = bailian_pool.pool_settings(config)
        if not settings or len(settings['slots']) != 1:
            return None
        identity = bailian_pool.pool_identity(config)
        return hashlib.sha256(('personal\0' + identity).encode()).hexdigest() if identity else None
    except Exception:
        return None


def _payload(config, ref):
    raw = keychain_secrets.get_bounded(ref['service'], ref['account'], timeout=5)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
        raise _QuotaError()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise _QuotaError()
    for key in ('cookie', 'sec_token', 'instance_code'):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > 32768 or \
                any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise _QuotaError()
    if not isinstance(payload.get('cornerstone'), dict) or not payload['cornerstone']:
        raise _QuotaError()
    digest = payload.get('inference_key_sha256')
    if not isinstance(digest, str) or not re.fullmatch('[a-fA-F0-9]{64}', digest):
        raise _QuotaError()
    settings = bailian_pool.pool_settings(config)
    if not settings or len(settings['slots']) != 1:
        raise _QuotaError()
    key, _generation = bailian_pool._load_key(settings['slots'][0])
    if not isinstance(key, bytes) or not key or not hmac.compare_digest(
            hashlib.sha256(key).hexdigest(), digest.lower()):
        raise _QuotaError()
    if 'inference_identity' in payload:
        identity = common.credential_identity(providers.BAILIAN_PROVIDER)
        if not isinstance(identity, str) or not isinstance(payload['inference_identity'], str) or \
                not hmac.compare_digest(identity, payload['inference_identity']):
            raise _QuotaError()
    return payload


def _request(api, payload):
    """Fixed read APIs only. No redirect, unbounded read, or response echo."""
    if api not in _APIS:
        raise _QuotaError()
    query = urllib.parse.urlencode({'product': 'sfm_bailian',
                                   'action': 'BroadScopeAspnGateway', 'api': api})
    params = {'Api': api, 'V': '1.0',
              'Data': {'cornerstoneParam': payload['cornerstone']}}
    data = urllib.parse.urlencode({'params': json.dumps(params, separators=(',', ':')),
                                  'sec_token': payload['sec_token'],
                                  'region': 'cn-beijing'}).encode()
    req = urllib.request.Request(_URL + '?' + query, data=data, method='POST', headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://bailian.console.aliyun.com/', 'Cookie': payload['cookie']})
    try:
        with urllib.request.build_opener(common.NoRedirect).open(req, timeout=15) as response:
            if response.status != 200:
                raise _QuotaError(response.status in (401, 403))
            raw = response.read(_BODY_LIMIT + 1)
        if not raw or len(raw) > _BODY_LIMIT:
            raise _QuotaError()
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('code') not in (200, '200') or \
                body.get('successResponse') is not True:
            raise _QuotaError()
        inner = body['data']['DataV2']['data']
        if not isinstance(inner, dict) or inner.get('success') is not True or \
                inner.get('code') != 'SUCCESS' or not isinstance(inner.get('data'), dict):
            raise _QuotaError()
        return inner['data']
    except urllib.error.HTTPError as error:
        raise _QuotaError(error.code in (401, 403)) from None
    except _QuotaError:
        raise
    except Exception:
        raise _QuotaError() from None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _epoch(value):
    value = _number(value)
    if value is None:
        return None
    value = value / 1000 if value >= 1e12 else value
    return value if 946684800 <= value <= 4102444800 else None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _normalize(subscription, usage, addon, payload, now):
    if not isinstance(subscription, dict) or not isinstance(usage, dict) or \
            subscription.get('instanceCode') != payload['instance_code']:
        raise _QuotaError()
    start, end = (_epoch(subscription.get(field)) for field in ('startTime', 'endTime'))
    reset = _epoch(usage.get('per1MonthResetTime'))
    fraction = _number(usage.get('per1MonthPercentage'))
    if subscription.get('status') != 'VALID' or start is None or end is None or \
            not start <= now < end or reset is None or not start < reset <= end or \
            reset <= now or fraction is None or not 0 <= fraction <= 1:
        raise _QuotaError()
    monthly = {'name': 'Monthly', 'unit': 'percent',
               'remaining_percent': 100 * (1 - fraction), 'valid': True,
               'resets_at': _iso(reset), 'subscribed_at': _iso(start),
               'duration_minutes': (reset - start) / 60}
    windows = [monthly]
    available = True if fraction < 1 else None
    addon_known = False
    addon_info = None
    if isinstance(addon, dict):
        remaining, total, count = (_number(addon.get(field)) for field in
                                   ('remainingCredits', 'totalCredits', 'activeCount'))
        addon_known = (remaining is not None and total is not None and count is not None
                       and 0 <= remaining <= total and count >= 0 and count.is_integer()
                       and (remaining == 0 or count > 0))
        if addon_known:
            addon_info = {'unit': 'Credits', 'remaining': remaining, 'limit': total,
                          'active_count': int(count),
                          'remaining_percent': remaining / total * 100 if total > 0 else None}
            if remaining > 0 and fraction == 1:
                windows.append(dict(addon_info, name='Addon', valid=True))
                monthly.update(valid=False, superseded_by_addon=True)
                available = True
    if fraction == 1:
        if addon_known and remaining == 0:
            available = False
        elif available is None:
            monthly.update(valid=False, addon_quota_unknown=True)
    return {'state': 'ok', 'sampled_at': now, 'source': SOURCE, 'plan_edition': 'personal',
            'windows': windows, 'balances': [], 'available': available,
            'addon': addon_info,
            'subscription': {'start_at': _iso(start), 'end_at': _iso(end)}}


def _unknown(now, auth=False):
    return {'state': 'unknown', 'attempted_at': now, 'source': SOURCE,
            'plan_edition': 'personal', 'available': None, 'windows': [], 'balances': [],
            'live_error': {'state': 'unknown', 'kind': 'quota_auth' if auth else 'quota_unavailable',
                           'detail': _ERROR}}


def fetch(config=None):
    """Return a bound monthly sample or unknown, without inference auth changes."""
    now = time.time()
    try:
        config = _config(config)
        ref = _reference(config)
        if ref is None:
            return _unknown(now)
        payload = _payload(config, ref)
        subscription = _request(SUBSCRIPTION, payload)
        usage = _request(USAGE, payload)
        try:
            addon = _request(ADDON, payload)
        except Exception:
            addon = None
        return _normalize(subscription, usage, addon, payload, now)
    except Exception as error:
        return _unknown(now, isinstance(error, _QuotaError) and error.auth)
