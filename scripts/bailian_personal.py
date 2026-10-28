"""Opt-in, read-only Personal Token Plan console telemetry.

Account AK/SK mint a transient official CLI token. An explicitly selected legacy
browser session may instead live in macOS Keychain. Both sources require the
bound subscription and current single inference key; console authentication
failure never declares that inference key invalid.
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
_EDITIONS = frozenset(('personal', 'sequential'))


class _QuotaError(Exception):
    def __init__(self, auth=False):
        self.auth = auth
        super().__init__(_ERROR)


def _config(config):
    return common.config() if config is None else config


def _reference(config):
    if not isinstance(config, dict) or config.get('bailian_plan_edition') not in _EDITIONS:
        return None
    block = config.get('bailian_personal_quota')
    if not isinstance(block, dict) or block.get('enabled') is not True:
        return None
    if set(block) - {'enabled', 'keychain', 'auth', 'slot_id'} or \
            block.get('auth', 'browser') != 'browser' or 'keychain' not in block:
        return None
    ref = block['keychain']
    if not isinstance(ref, dict) or set(ref) != {'service', 'account'}:
        return None
    if any(not isinstance(ref[k], str) or not 1 <= len(ref[k]) <= 255 or
           any(ord(c) < 32 or ord(c) == 127 for c in ref[k])
           for k in ('service', 'account')):
        return None
    return ref


def _selected_slot(config):
    """Select one named Personal slot; multiple keys are never guessed."""
    settings = bailian_pool.pool_settings(config)
    if not settings or not settings['slots']:
        raise _QuotaError()
    block = config.get('bailian_personal_quota')
    slot_id = block.get('slot_id') if isinstance(block, dict) else None
    if slot_id is None and len(settings['slots']) == 1:
        return settings['slots'][0]
    if not isinstance(slot_id, str) or not slot_id:
        raise _QuotaError()
    selected = [slot for slot in settings['slots'] if slot['id'] == slot_id]
    if len(selected) != 1:
        raise _QuotaError()
    return selected[0]


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[a-fA-F0-9]{64}', value) is not None


def credential_identity(config=None):
    """Private opaque metadata identity; never opens a key or console session."""
    try:
        config = _config(config)
        if not isinstance(config, dict) or config.get('bailian_plan_edition') not in _EDITIONS:
            return None
        selected = _selected_slot(config)
        raw = next(slot for slot in config['bailian_pool']['slots'] if slot['id'] == selected['id'])
        isolated = dict(config, bailian_plan_edition='personal',
                        bailian_pool=dict(config['bailian_pool'], slots=[raw]))
        identity = bailian_pool.pool_identity(isolated)
        if not identity:
            return None
        material = 'personal\0' + identity
        block = config.get('bailian_personal_quota')
        if isinstance(block, dict) and block.get('enabled') is True and \
                block.get('auth', 'browser' if 'keychain' in block else 'access_key') == 'access_key':
            bindings = [block.get(key) for key in ('subscription_instance_hash', 'inference_key_sha256')]
            if not all(_digest(value) for value in bindings):
                return None
            material += '\0' + '\0'.join(value.lower() for value in bindings)
        return hashlib.sha256(material.encode()).hexdigest()
    except Exception:
        return None


def _check_key_binding(config, digest):
    if not _digest(digest):
        raise _QuotaError()
    key, _generation = bailian_pool._load_key(_selected_slot(config))
    if not isinstance(key, bytes) or not key or not hmac.compare_digest(
            hashlib.sha256(key).hexdigest(), digest.lower()):
        raise _QuotaError()


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
    _check_key_binding(config, payload.get('inference_key_sha256'))
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
    # Unknown is never zero: a failed or malformed addon summary stays an explicit
    # unknown display state while the bound monthly allowance keeps working.
    addon_info = {'known': False}
    if isinstance(addon, dict):
        remaining, total, count = (_number(addon.get(field)) for field in
                                   ('remainingCredits', 'totalCredits', 'activeCount'))
        expiry = _epoch(addon.get('nearestExpireTime'))
        addon_known = (remaining is not None and total is not None and count is not None
                       and 0 <= remaining <= total and count >= 0 and count.is_integer()
                       and (remaining == 0 or count > 0))
        if addon_known:
            # used is derived from the active pool only, never lifetime purchases.
            addon_info = {'known': True, 'unit': 'credits', 'remaining': remaining,
                          'limit': total, 'used': max(0.0, total - remaining),
                          'active_count': int(count),
                          'remaining_percent': remaining / total * 100 if total > 0 else None}
            if expiry is not None:
                # Server-supplied nearest expiry across packs: never all packs at once.
                addon_info['nearest_expires_at'] = _iso(expiry)
                if expiry <= now and remaining > 0:
                    # One past pack expiry beside a positive aggregate is an
                    # inconsistent sample, not proof that every pack expired or
                    # that the addon is empty. Show it as unknown, keep monthly.
                    addon_info['inconsistent'] = True
    addon_active = (addon_known and addon_info['remaining'] > 0 and
                    not addon_info.get('inconsistent'))
    if addon_active and fraction == 1:
        # Only after real monthly exhaustion is the addon an admission window.
        windows.append(dict(addon_info, name='Addon', valid=True))
        monthly.update(valid=False, superseded_by_addon=True)
        available = True
    if fraction == 1:
        if addon_known and addon_info['remaining'] == 0:
            # Known-empty active pool is preserved internally as exhaustion evidence.
            available = False
        elif not addon_active:
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
        if not isinstance(config, dict) or config.get('bailian_plan_edition') not in _EDITIONS:
            return _unknown(now)
        block = config.get('bailian_personal_quota')
        if not isinstance(block, dict) or block.get('enabled') is not True:
            return _unknown(now)
        auth = block.get('auth', 'browser' if 'keychain' in block else 'access_key')
        if auth == 'access_key':
            if set(block) - {'enabled', 'auth', 'subscription_instance_hash', 'inference_key_sha256',
                            'slot_id'}:
                return _unknown(now)
            instance_hash = block.get('subscription_instance_hash')
            if not _digest(instance_hash):
                return _unknown(now)
            _check_key_binding(config, block.get('inference_key_sha256'))
            import bailian_personal_ak
            subscription, usage, addon = bailian_personal_ak.fetch_payloads(config)
            instance = subscription.get('instanceCode') if isinstance(subscription, dict) else None
            if not isinstance(instance, str) or not instance or not hmac.compare_digest(
                    hashlib.sha256(instance.encode()).hexdigest(), instance_hash.lower()):
                raise _QuotaError()
            value = _normalize(subscription, usage, addon, {'instance_code': instance}, now)
            value['auth_method'] = 'access_key_cli_token'
            return value
        if auth != 'browser':
            return _unknown(now)
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
        return _unknown(now, getattr(error, 'auth', False) is True)
