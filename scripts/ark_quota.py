"""Ark Agent Plan control-plane quota adapter (Volcengine OpenAPI, AK/SK SigV4).

Endpoint, signing and response shape verified live on 2026-09-20:

* ``POST https://ark.cn-beijing.volcengineapi.com/?Action=...&Version=2024-01-01``
  (query keys sorted), service ``ark``, region ``cn-beijing``, version ``2024-01-01``.
* Signed headers in this exact order: ``host;x-date;x-content-sha256;content-type``
  (Volcengine variant of SigV4: no ``AWS4`` prefixes, scope ends in ``request``).
* Actions ``GetAFPUsage`` (body ``{}``) and ``GetPersonalPlan``
  (body ``{"Plan":"AgentPlan"}``).
* ``GetAFPUsage.Result``: ``PlanType`` plus ``AFPFiveHour``/``AFPDaily``/``AFPWeekly``/
  ``AFPMonthly``, each with ``Quota``, ``Used``, ``SubscribeTime`` and ``ResetTime``
  (epoch milliseconds). ``GetPersonalPlan.Result``: ``PlanType``, ``Status``,
  ``StartTime``, ``EndTime``, ``AutoRenew``.

Credential boundary: the account AccessKey ID/Secret is a separate credential from
the Ark inference key and is never read from this module's configuration files,
never logged and never returned. Values are resolved in-process only, in this
order: (1) metadata-only references registered through ``delegate-opencode
credential`` (fixed names ``volcengine-control-ak`` / ``volcengine-control-sk``),
then (2) process environment variables ``VOLC_ACCESSKEY`` / ``VOLC_SECRETKEY``.
There is no ``get``/``show`` path; only availability metadata is exposed.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

import common
from common import HttpFailure
from providers import ARK_PROVIDER

HOST = 'ark.cn-beijing.volcengineapi.com'
SERVICE = 'ark'
REGION = 'cn-beijing'
VERSION = '2024-01-01'
ACTION_AFP_USAGE = 'GetAFPUsage'
ACTION_PERSONAL_PLAN = 'GetPersonalPlan'
PERSONAL_PLAN_BODY = {'Plan': 'AgentPlan'}
CONTENT_TYPE = 'application/json; charset=utf-8'
SIGNED_HEADERS = 'host;x-date;x-content-sha256;content-type'

CREDENTIAL_REF_NAMES = {'access_key': 'volcengine-control-ak', 'secret_key': 'volcengine-control-sk'}
ENV_NAMES = {'access_key': 'VOLC_ACCESSKEY', 'secret_key': 'VOLC_SECRETKEY'}

# Result keys of GetAFPUsage and the duration each window represents in minutes.
AFP_WINDOWS = (('AFPFiveHour', 300), ('AFPDaily', 1440), ('AFPWeekly', 10080), ('AFPMonthly', 43200))
_PLAN_STATUS_ACTIVE = {'active', 'valid', 'subscribed', 'effective', 'in_use',
                       'normal', 'running'}

# Common Volcengine OpenAPI auth/credential error codes (compared lowercase).
_AUTH_CODES = ('signature', 'accessdenied', 'denied', 'unauthorized', 'forbidden',
               'invalidaccesskey', 'invalidsecretkey', 'missingcredential', 'credential')


def _resolve_part(part, registry):
    """One AK/SK part from a registered reference or the process environment.

    Returns ``(value, source)``; ``(None, None)`` when absent. The value never
    leaves this module: it feeds signing in-memory only.
    """
    ref_name = CREDENTIAL_REF_NAMES[part]
    entry = registry.get(ref_name)
    if isinstance(entry, dict):
        try:
            import credentials
            if entry.get('source') == 'file':
                value = credentials._read_file_value(entry.get('path'))
                if value:
                    return value, 'credential_reference'
            elif entry.get('source') == 'env':
                value = os.environ.get(entry.get('env'), '')
                if value:
                    return value, 'credential_reference'
        except Exception:
            # A broken reference never falls through to the environment: that
            # would silently sign with a different credential than registered.
            return None, 'broken_reference'
    value = os.environ.get(ENV_NAMES[part], '')
    if value:
        return value, 'environment'
    return None, None


def resolve_credentials():
    """Resolve the AK/SK pair in-process.

    Returns ``(access_key, secret_key, source)`` or ``(None, None, None)`` when
    unavailable. Both parts must come from the same source so a rotated pair can
    never be mixed. A registered reference is primary and fail-closed: a broken
    or half-registered pair blocks the environment fallback, which would
    silently sign with a different credential. When no reference is registered
    at all, a complete environment pair is used.
    """
    try:
        import credentials
        registry = credentials._load_registry()
    except Exception:
        registry = {}
    ak, source_ak = _resolve_part('access_key', registry)
    sk, source_sk = _resolve_part('secret_key', registry)
    if 'broken_reference' in (source_ak, source_sk):
        return None, None, None
    if source_ak == 'credential_reference' or source_sk == 'credential_reference':
        # Registered references are primary: only a complete reference pair is
        # used; a half pair never falls through to the environment.
        if source_ak == source_sk == 'credential_reference' and ak and sk:
            return ak, sk, 'credential_reference'
        return None, None, None
    if source_ak == source_sk == 'environment' and ak and sk:
        return ak, sk, 'environment'
    return None, None, None


def credential_source():
    """Safe availability metadata only: 'credential_reference', 'environment' or None."""
    try:
        _, _, source = resolve_credentials()
        return source
    except Exception:
        return None


def credential_identity():
    """Hash identity of the current access key for circuit binding, or None."""
    ak, _, _ = resolve_credentials()
    if not ak:
        return None
    return hashlib.sha256(ak.encode()).hexdigest()


def _uri_encode(text):
    """RFC3986 unreserved characters kept; everything else percent-encoded."""
    out = []
    for byte in text.encode('utf-8'):
        char = chr(byte)
        if char.isalnum() or char in '-_.~':
            out.append(char)
        else:
            out.append('%%%02X' % byte)
    return ''.join(out)


def canonical_query(params):
    """Alphabetically sorted, per-segment encoded canonical query string.

    The same string is used for signing and for the request URL so the two are
    byte-identical (a signature mismatch is otherwise the failure mode).
    """
    return '&'.join(_uri_encode(key) + '=' + _uri_encode(value)
                    for key, value in sorted(params.items()))


def sign(access_key, secret_key, query, body, now):
    """Volcengine SigV4 variant; returns the headers for one signed request.

    Differences from AWS SigV4 (both are load-bearing, verified live): the
    canonical header order is fixed ``host;x-date;x-content-sha256;content-type``
    (not alphabetical), the algorithm string is ``HMAC-SHA256``, the credential
    scope ends in ``request`` and the date key is ``HMAC(SK, date)`` with no
    ``AWS4`` prefix on the secret.
    """
    x_date = now.strftime('%Y%m%dT%H%M%SZ')
    short_date = now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_headers = ('host:' + HOST + '\n' + 'x-date:' + x_date + '\n' +
                         'x-content-sha256:' + payload_hash + '\n' +
                         'content-type:' + CONTENT_TYPE + '\n')
    canonical_request = ('POST\n/\n' + query + '\n' + canonical_headers + '\n' +
                         SIGNED_HEADERS + '\n' + payload_hash)
    scope = short_date + '/' + REGION + '/' + SERVICE + '/request'
    string_to_sign = ('HMAC-SHA256\n' + x_date + '\n' + scope + '\n' +
                      hashlib.sha256(canonical_request.encode()).hexdigest())
    k_date = hmac.new(secret_key.encode(), short_date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, REGION.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, SERVICE.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b'request', hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {'X-Date': x_date, 'X-Content-Sha256': payload_hash, 'Content-Type': CONTENT_TYPE,
            'Authorization': ('HMAC-SHA256 Credential=' + access_key + '/' + scope +
                              ', SignedHeaders=' + SIGNED_HEADERS + ', Signature=' + signature)}


def _redact_auth(headers):
    """Safe headers for diagnostics: the Authorization value is never exposed."""
    safe = dict(headers)
    if 'Authorization' in safe:
        safe['Authorization'] = '<redacted>'
    return safe


def call(action, body_obj, access_key, secret_key, now=None):
    """One signed control-plane call; returns the parsed JSON body.

    Credentials only appear inside signing and the live Authorization header;
    raised errors carry only the endpoint's human-readable error message through
    common.HttpFailure, which is redacted upstream. ``now`` is injectable for
    deterministic tests.
    """
    now = now or datetime.now(timezone.utc)
    query = canonical_query({'Action': action, 'Version': VERSION})
    body = json.dumps(body_obj, ensure_ascii=False, separators=(',', ':')).encode()
    headers = sign(access_key, secret_key, query, body, now)
    req = urllib.request.Request('https://' + HOST + '/?' + query, data=body,
                                 headers=headers, method='POST')
    try:
        with urllib.request.build_opener(common.NoRedirect).open(req, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        message = None
        try:
            value = json.loads(e.read(32768))
            error = _envelope_error(value)
            if error:
                message = error[0] + ': ' + error[1] if error[0] else error[1]
        except (ValueError, OSError):
            pass
        raise HttpFailure(e.code, message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HttpFailure(message=str(e)) from None
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        raise HttpFailure(message='Ark control plane returned a non-JSON body') from None
    if not isinstance(parsed, dict):
        raise HttpFailure(message='Ark control plane returned an unexpected payload') from None
    error = _envelope_error(parsed)
    if error:
        # Volcengine reports business errors with HTTP 200 + ResponseMetadata.Error.
        code, message = error
        status = 403 if _is_auth_code(code) else None
        raise HttpFailure(status, code + ': ' + message if code else message) from None
    return parsed


def _envelope_error(body):
    """(Code, Message) from ResponseMetadata.Error or a top-level Error, else None."""
    if not isinstance(body, dict):
        return None
    error = body.get('ResponseMetadata')
    error = error.get('Error') if isinstance(error, dict) else None
    if error is None:
        error = body.get('Error')
    if not isinstance(error, dict):
        return None
    code = error.get('Code')
    message = error.get('Message')
    code = code if isinstance(code, str) else ''
    message = message if isinstance(message, str) else ''
    return (code, message) if code or message else None


def _is_auth_code(code):
    lowered = str(code).lower()
    return any(fragment in lowered for fragment in _AUTH_CODES)


def _epoch_ms_iso(value):
    """Epoch milliseconds (or seconds) to ISO8601 UTC; None for non-positive input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value >= 1e12 else value
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _time_iso(value):
    """A point in time from the live API: ISO-8601 string or epoch ms/seconds.

    GetPersonalPlan returns StartTime/EndTime as ISO-8601 strings while
    GetAFPUsage windows report epoch milliseconds; both are preserved as a
    validated ISO-8601 UTC instant. Unparseable input is None, never guessed.
    """
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
        return parsed.astimezone(timezone.utc).isoformat()
    return _epoch_ms_iso(value)


def _count(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    import math
    return number if math.isfinite(number) else None


def _window(name, detail, duration_minutes):
    """One AFP window normalized to the shared quota.window shape."""
    if not isinstance(detail, dict):
        detail = {}
    quota = _count(detail.get('Quota'))
    used = _count(detail.get('Used'))
    remaining = None
    if quota is not None and used is not None:
        remaining = max(0.0, quota - used)
    valid = quota is not None and quota > 0 and used is not None and used >= 0
    out = {'name': name, 'limit': quota if valid else None,
           'remaining': remaining if valid else None,
           'remaining_percent': round(remaining / quota * 100, 3) if valid and quota > 0 else None,
           'duration_minutes': duration_minutes,
           'resets_at': _epoch_ms_iso(detail.get('ResetTime')),
           'valid': bool(valid)}
    subscribe = _epoch_ms_iso(detail.get('SubscribeTime'))
    if subscribe:
        out['subscribed_at'] = subscribe
    return out


def normalize_afp_usage(result):
    """GetAFPUsage Result → shared quota shape. All four windows are normalized.

    A window with a non-positive or malformed quota stays present but invalid,
    so malformed input is never silently read as exhausted or as unlimited.
    Availability is False when any valid window is exhausted, True when every
    present window is valid with allowance, else None (unknown).
    """
    if not isinstance(result, dict):
        raise ValueError('Invalid Ark AFP usage payload')
    windows = []
    for key, duration in AFP_WINDOWS:
        if key in result:
            windows.append(_window(key, result.get(key), duration))
    valid = [w for w in windows if w['valid']]
    if windows and any(w['valid'] and w['remaining'] == 0 for w in windows):
        available = False
    elif windows and len(valid) == len(windows):
        available = True
    else:
        available = None
    plan_type = result.get('PlanType')
    return {'windows': windows, 'available': available, 'balances': [],
            'plan': {'type': plan_type if isinstance(plan_type, str) and plan_type else None}}


def normalize_personal_plan(result):
    """GetPersonalPlan Result → safe plan metadata (no account identifiers)."""
    if not isinstance(result, dict):
        raise ValueError('Invalid Ark personal plan payload')
    plan_type = result.get('PlanType')
    status = result.get('Status')
    auto_renew = result.get('AutoRenew')
    return {'type': plan_type if isinstance(plan_type, str) and plan_type else None,
            'status': status if isinstance(status, str) and status else None,
            'active': status.lower() in _PLAN_STATUS_ACTIVE if isinstance(status, str) and status else None,
            'auto_renew': auto_renew if isinstance(auto_renew, bool) else None,
            'started_at': _time_iso(result.get('StartTime')),
            'ends_at': _time_iso(result.get('EndTime'))}


def fetch():
    """Live control-plane sample for the shared quota cache.

    Returns a dict in the same shape quota.fetch_one produces for bearer-token
    providers, plus ``credential_source`` metadata. AK/SK values never appear in
    the returned structure. Raises nothing: every failure is a state dict.
    """
    import time
    now = time.time()
    access_key, secret_key, source = resolve_credentials()
    if not access_key:
        return {'state': 'no_credential', 'attempted_at': now,
                'credential_source': None,
                'note': 'Ark control-plane AK/SK not registered; quota unknown. '
                        'Inference (data plane) is unaffected.'}
    try:
        usage = call(ACTION_AFP_USAGE, {}, access_key, secret_key)
        usage_result = usage.get('Result')
        value = normalize_afp_usage(usage_result if isinstance(usage_result, dict) else {})
    except HttpFailure as e:
        state = 'auth_error' if e.status in (401, 403) else \
            'rate_limited' if e.status == 429 else 'unavailable'
        return {'state': state, 'http_status': e.status, 'attempted_at': now,
                'credential_source': source}
    except ValueError:
        return {'state': 'unknown', 'attempted_at': now, 'credential_source': source}
    plan = None
    try:
        personal = call(ACTION_PERSONAL_PLAN, PERSONAL_PLAN_BODY, access_key, secret_key)
        personal_result = personal.get('Result')
        if isinstance(personal_result, dict):
            plan = normalize_personal_plan(personal_result)
    except (HttpFailure, ValueError):
        plan = None  # Usage telemetry stays authoritative; plan metadata is enrichment.
    if plan:
        merged = dict(value.get('plan') or {})
        merged.update({k: v for k, v in plan.items() if v is not None})
        value['plan'] = merged
    value.update(state='ok', sampled_at=now, credential_source=source)
    return value
