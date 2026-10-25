"""Read-only Alibaba Token Plan control-plane transport.

GetSubscriptionStats and ACS3 signing were verified against the official API on
2026-10-17. Model API keys and Volcengine credentials are deliberately unrelated.
Only local credential references or the standard Alibaba SDK environment pair
are resolved. Credentials never enter returned metadata or error messages.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
import uuid

import common

HOST = 'modelstudio.cn-beijing.aliyuncs.com'
PATH = '/tokenplan/subscription/stats'
ACTION = 'GetSubscriptionStats'
VERSION = '2026-02-10'
ALGORITHM = 'ACS3-HMAC-SHA256'
CREDENTIAL_REF_NAMES = {'access_key': 'aliyun-control-ak', 'secret_key': 'aliyun-control-sk'}
ENV_NAMES = {'access_key': 'ALIBABA_CLOUD_ACCESS_KEY_ID',
             'secret_key': 'ALIBABA_CLOUD_ACCESS_KEY_SECRET'}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def resolve_credentials():
    """Return the complete (AK, SK, source) pair in-process, or three Nones.

    Any registered part makes references authoritative. Missing, unreadable or
    half-registered pairs never fall back to a different environment identity.
    """
    try:
        import credentials
        registry = credentials._load_registry()
        entries = [registry.get(CREDENTIAL_REF_NAMES[k]) for k in ('access_key', 'secret_key')]
        if any(entry is not None for entry in entries):
            values = []
            for entry in entries:
                if not isinstance(entry, dict):
                    return None, None, None
                if entry.get('source') == 'file':
                    value = credentials._read_file_value(entry.get('path'))
                elif entry.get('source') == 'env':
                    value = os.environ.get(entry.get('env'), '')
                else:
                    return None, None, None
                if not value:
                    return None, None, None
                values.append(value)
            return values[0], values[1], 'credential_reference'
    except Exception:
        return None, None, None
    ak, sk = (os.environ.get(ENV_NAMES[k], '') for k in ('access_key', 'secret_key'))
    return (ak, sk, 'environment') if ak and sk else (None, None, None)


def credential_source():
    """Return availability metadata only, never the reference paths or values."""
    return resolve_credentials()[2]


def credential_identity():
    """Private identity changes on rotation of either credential component."""
    ak, sk, _ = resolve_credentials()
    if not ak or not sk:
        return None
    return hashlib.sha256((ak + '\0' + sk).encode()).hexdigest()


def sign(access_key, secret_key, now=None, nonce=None):
    """Build signed headers; the result is private and must never be logged."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Signing time must have a timezone')
    empty = hashlib.sha256(b'').hexdigest()
    headers = {
        'host': HOST,
        'x-acs-action': ACTION,
        'x-acs-content-sha256': empty,
        'x-acs-date': now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'x-acs-signature-nonce': nonce or uuid.uuid4().hex,
        'x-acs-version': VERSION,
    }
    signed = ';'.join(sorted(headers))
    canonical_headers = ''.join(k + ':' + headers[k] + '\n' for k in sorted(headers))
    canonical_request = '\n'.join(['GET', PATH, '', canonical_headers, signed, empty])
    to_sign = ALGORITHM + '\n' + hashlib.sha256(canonical_request.encode()).hexdigest()
    signature = hmac.new(secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = (ALGORITHM + ' Credential=' + access_key +
                                ',SignedHeaders=' + signed + ',Signature=' + signature)
    return headers


def _safe_code(body, access_key, secret_key):
    code = body.get('Code') if isinstance(body, dict) else None
    if not isinstance(code, (str, int)) or isinstance(code, bool):
        return 'UnknownError'
    code = str(code)
    for value in (access_key, secret_key):
        if value:
            code = code.replace(value, 'REDACTED')
    return code if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code) else 'UnknownError'


def call(access_key, secret_key, now=None, nonce=None):
    """GET the fixed official endpoint with bounded time, body and no redirects.

    Service error bodies and transport exceptions are never returned verbatim:
    they may contain request headers or credential identifiers.
    """
    headers = sign(access_key, secret_key, now, nonce)
    request = urllib.request.Request('https://' + HOST + PATH, headers=headers, method='GET')
    try:
        with urllib.request.build_opener(common.NoRedirect).open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise common.HttpFailure(error.code, 'Alibaba Token Plan quota request failed') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise common.HttpFailure(message='Alibaba Token Plan quota endpoint is unreachable') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise common.HttpFailure(message='Alibaba Token Plan quota response exceeded the size limit')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise common.HttpFailure(message='Alibaba Token Plan quota response is not valid JSON') from None
    if not isinstance(body, dict):
        raise common.HttpFailure(message='Alibaba Token Plan quota response has an unexpected shape')
    if body.get('Success') is not True:
        code = _safe_code(body, access_key, secret_key)
        auth = any(part in code.lower() for part in
                   ('signature', 'accesskey', 'unauthorized', 'forbidden', 'accessdenied', 'permission'))
        raise common.HttpFailure(403 if auth else None,
                                 'Alibaba Token Plan quota request failed (' + code + ')')
    return body


def get_subscription_stats():
    """Query with registered credentials; the caller normalizes account fields."""
    ak, sk, source = resolve_credentials()
    if not source:
        raise common.HttpFailure(message='Alibaba control-plane AK/SK are not configured or unavailable')
    return call(ak, sk)
