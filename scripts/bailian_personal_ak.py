"""Official Personal console CLI gateway, authenticated by account AK/SK.

Only the existing Alibaba control credentials authenticate quota calls. A CLI
token is minted afresh for each sample and retained only in its local stack;
one token-expiry retry is allowed across the three fixed read operations.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

import aliyun_openapi
import common

HOST = 'modelstudio.cn-beijing.aliyuncs.com'
PATH = '/modelstudio/cli/generateAccessToken'
ACTION = 'GenerateCLIAccessToken'
VERSION = '2026-02-10'
_GATEWAY = 'https://bailian-cs.console.aliyun.com/cli/api.json'
_PREFIX = 'zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/'
SUBSCRIPTION, USAGE, ADDON = (_PREFIX + suffix for suffix in
                            ('subscription', 'usage', 'addon/summary'))
_APIS = frozenset((SUBSCRIPTION, USAGE, ADDON))
_LIMIT = 1024 * 1024
_ERROR = 'Personal Token Plan quota is unavailable.'
_CORNERSTONE = {'protocol': 'V2', 'console': 'ONE_CONSOLE', 'productCode': 'p_efm',
                'switchUserType': 3, 'consoleSite': 'BAILIAN_ALIYUN'}


class QuotaError(Exception):
    def __init__(self, auth=False):
        self.auth = auth
        super().__init__(_ERROR)


def _sign(access_key, secret_key, now=None, nonce=None):
    """Sign the single fixed empty-body POST using the official CLI contract."""
    now = now or datetime.now(timezone.utc)
    empty = hashlib.sha256(b'').hexdigest()
    headers = {'host': HOST, 'content-type': 'application/json',
               'x-acs-action': ACTION, 'x-acs-version': VERSION,
               'x-acs-date': now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               'x-acs-signature-nonce': nonce or uuid.uuid4().hex,
               'x-acs-content-sha256': empty}
    signed = ';'.join(sorted(headers))
    canonical_headers = ''.join(key + ':' + headers[key] + '\n' for key in sorted(headers))
    canonical = '\n'.join(('POST', PATH, '', canonical_headers, signed, empty))
    to_sign = 'ACS3-HMAC-SHA256\n' + hashlib.sha256(canonical.encode()).hexdigest()
    signature = hmac.new(secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    headers['Authorization'] = ('ACS3-HMAC-SHA256 Credential=' + access_key +
                                ',SignedHeaders=' + signed + ',Signature=' + signature)
    return headers


def _read_json(request):
    try:
        with urllib.request.build_opener(common.NoRedirect).open(request, timeout=15) as response:
            if response.status != 200:
                raise QuotaError(response.status in (401, 403))
            raw = response.read(_LIMIT + 1)
        if not raw or len(raw) > _LIMIT:
            raise QuotaError()
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise QuotaError()
        return body
    except urllib.error.HTTPError as error:
        raise QuotaError(error.code in (401, 403)) from None
    except QuotaError:
        raise
    except Exception:
        raise QuotaError() from None


def _mint_token(access_key, secret_key):
    try:
        request = urllib.request.Request('https://' + HOST + PATH, method='POST', data=b'',
                                         headers=_sign(access_key, secret_key))
        token = _read_json(request).get('cliAccessToken')
        if not isinstance(token, str) or not 1 <= len(token) <= 65536 or \
                any(ord(c) < 33 or ord(c) == 127 for c in token):
            raise QuotaError()
        return token
    except QuotaError:
        raise
    except Exception:
        raise QuotaError() from None


def _gateway(api, token):
    if api not in _APIS:
        raise QuotaError()
    try:
        query = urllib.parse.urlencode({'action': 'BroadScopeAspnGateway',
                                       'product': 'sfm_bailian', 'api': api})
        params = {'Api': api, 'V': '1.0', 'Data': {'cornerstoneParam': _CORNERSTONE}}
        form = urllib.parse.urlencode({'params': json.dumps(params, separators=(',', ':')),
                                       'region': 'cn-beijing'}).encode()
        request = urllib.request.Request(_GATEWAY + '?' + query, method='POST', data=form,
            headers={'Authorization': 'Bearer ' + token, 'Accept': '*/*',
                     'Content-Type': 'application/x-www-form-urlencoded'})
        body = _read_json(request)
        outer = body.get('data')
        inner = outer.get('DataV2', {}).get('data') if isinstance(outer, dict) else None
        for layer in (body, outer, inner):
            if isinstance(layer, dict):
                for key in ('errorCode', 'code', 'Code'):
                    code = layer.get(key)
                    if isinstance(code, str) and 'NotLogined' in code:
                        raise QuotaError(True)
        if body.get('code') not in (200, '200') or body.get('successResponse') is not True or \
                not isinstance(inner, dict) or inner.get('success') is not True or \
                inner.get('code') != 'SUCCESS' or not isinstance(inner.get('data'), dict):
            raise QuotaError()
        return inner['data']
    except QuotaError:
        raise
    except Exception:
        raise QuotaError() from None


def fetch_payloads(config=None):
    """Return subscription, usage and optional addon; no tokens escape."""
    try:
        access_key, secret_key, _source = aliyun_openapi.resolve_credentials()
        if not isinstance(access_key, str) or not access_key or \
                not isinstance(secret_key, str) or not secret_key:
            raise QuotaError()
        token = _mint_token(access_key, secret_key)
        refreshed = False

        def read(api):
            nonlocal token, refreshed
            try:
                return _gateway(api, token)
            except QuotaError as error:
                if not error.auth or refreshed:
                    raise
                refreshed = True
                token = _mint_token(access_key, secret_key)
                return _gateway(api, token)

        subscription = read(SUBSCRIPTION)
        usage = read(USAGE)
        try:
            addon = read(ADDON)
        except QuotaError:
            addon = None
        return subscription, usage, addon
    except QuotaError:
        raise
    except Exception:
        raise QuotaError() from None
