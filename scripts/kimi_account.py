"""Account quota through the authenticated official Kimi Code loopback API.

OAuth and refresh tokens remain owned by the official CLI. Account quota is
display-only until its account has been linked to the inference credential.
No method in this module changes inference authentication or remote control.
"""
import hashlib
import base64
import json
import math
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import common
import keychain_secrets

SOURCE = 'kimi_code_account'
WEB_SOURCE = 'kimi_web_membership'
WEB_KEYCHAIN_SERVICE = 'com.workerdesk.kimi-web-session'
WEB_KEYCHAIN_ACCOUNT = 'default'
VAULT_TIMEOUT = 5
VAULT_PYTHON = '/Library/Developer/CommandLineTools/usr/bin/python3'
# Only this short-lived trusted interpreter enters Security.framework. Never
# execute the vault call in the console's Python process: legacy Keychain IPC
# can block despite kSecUseAuthenticationUIFail. Child stdout is a captured
# private pipe, never inherited by the console or command logs.
VAULT_HELPER = r'''
import base64, ctypes, json, sys
sys.path.insert(0, sys.argv[1])
import keychain_secrets
try:
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    sec.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    sec.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
    if sec.SecKeychainSetUserInteractionAllowed(False) != 0:
        sys.exit(3)
    request = json.loads(sys.stdin.buffer.read(100000))
    if request['action'] == 'get':
        value = keychain_secrets.get(request['service'], request['account'])
        sys.stdout.buffer.write(value)
    elif request['action'] == 'put':
        value = base64.b64decode(request['payload'], validate=True)
        keychain_secrets.put(request['service'], request['account'], value)
    else:
        sys.exit(3)
except keychain_secrets.KeychainError as error:
    sys.exit(4 if 'status -25300' in str(error) else 3)
except Exception:
    sys.exit(3)
'''
WEB_STATS_URL = 'https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats'
WEB_USER_URL = 'https://www.kimi.com/apiv2/kimi.gateway.account.v1.UserService/GetCurrentUser'
WEB_REFRESH_URL = 'https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken'
HOME = Path(os.environ.get('KIMI_CODE_HOME', '~/.kimi-code')).expanduser()
RUNTIME = common.STATE / 'runtime' / 'kimi-code'
TIMEOUT = 12
MAX_BODY = 1024 * 1024
WINDOWS = (('limit5h', 'short', 300), ('limit7d', 'weekly', 10080),
           ('monthTotal', 'month_total', None), ('monthCode', 'month_code', None))


class AccountError(RuntimeError):
    """Only fixed safe error codes, never upstream messages or tokens."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _private(path):
    if path.is_symlink() or not path.is_file():
        raise AccountError('private_file_missing')
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise AccountError('private_file_permissions')
    return path.read_text()


def _origin(host, port):
    if host != '127.0.0.1' or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise AccountError('unsafe_account_service')
    return 'http://127.0.0.1:' + str(port)


def _instances():
    values = []
    for path in (HOME / 'server' / 'instances').glob('*.json'):
        try:
            row = json.loads(path.read_text())
            origin = _origin(row.get('host'), row.get('port'))
            pid = row.get('pid')
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
                continue
            os.kill(pid, 0)
            values.append((origin, pid))
        except (OSError, ValueError, TypeError, AccountError):
            continue
    return values


def _http(origin, path, method='GET', body=None, authenticated=True):
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise AccountError('unsafe_account_service')
    try:
        _origin(parsed.hostname, parsed.port)
    except ValueError:
        raise AccountError('unsafe_account_service') from None
    if path not in {'/api/v1/meta', '/api/v1/auth', '/api/v1/oauth/usage',
                    '/api/v1/oauth/userinfo', '/api/v1/oauth/login', '/openapi.json'}:
        raise AccountError('unsupported_account_endpoint')
    headers = {'Accept': 'application/json'}
    if authenticated:
        token = _private(HOME / 'server.token').strip()
        if not token or chr(10) in token or chr(13) in token:
            raise AccountError('invalid_service_token')
        headers['Authorization'] = 'Bearer ' + token
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise AccountError('account_response_too_large')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AccountError('invalid_account_response')
        return value
    except urllib.error.HTTPError as error:
        raise AccountError('account_http_' + str(error.code)) from None
    except (OSError, ValueError):
        raise AccountError('account_service_unavailable') from None


def _service():
    for origin, pid in _instances():
        try:
            meta = _http(origin, '/api/v1/meta')
            data = meta.get('data')
            if meta.get('code') == 0 and isinstance(data, dict) and data.get('dangerous_bypass_auth') is False:
                return origin, pid
        except AccountError:
            continue
    raise AccountError('account_service_unavailable')


def _call(path, method='GET', body=None):
    origin, _pid = _service()
    result = _http(origin, path, method, body)
    if result.get('code') != 0 or not isinstance(result.get('data'), dict):
        raise AccountError('account_request_failed')
    data = result['data']
    if data.get('kind') == 'error':
        status_code = data.get('status')
        raise AccountError('account_auth_required' if status_code in (401, 403) else 'account_usage_unavailable')
    return data


def ready():
    try:
        _service()
        return True
    except AccountError:
        return False


def _launch_command(binary):
    # Resolve Node absolutely for launchd callers with a minimal PATH.
    candidates = [RUNTIME / 'node_modules' / 'node' / 'bin' / 'node',
                  Path('/opt/homebrew/bin/node'), Path('/usr/local/bin/node')]
    found = shutil.which('node')
    if found:
        candidates.append(Path(found))
    for node in candidates:
        if node.is_file() and os.access(node, os.X_OK):
            return [str(node.resolve()), str(binary.resolve())]
    raise AccountError('node_runtime_unavailable')


def start(wait_seconds=15):
    """Reuse authenticated loopback only, otherwise start the isolated CLI."""
    if ready():
        return {'state': 'ready', 'source': SOURCE, 'owned': False}
    binary = RUNTIME / 'node_modules' / '.bin' / 'kimi'
    if not binary.is_file():
        return {'state': 'not_installed', 'source': SOURCE}
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with common.locked('kimi-account-service'):
        if ready():
            return {'state': 'ready', 'source': SOURCE, 'owned': False}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        for key in ('KIMI_CODE_BASE_URL', 'KIMI_CODE_OAUTH_HOST', 'KIMI_OAUTH_HOST', 'KIMI_CODE_CUSTOM_HEADERS'):
            env.pop(key, None)
        env['KIMI_CODE_HOME'] = str(HOME)
        try:
            command = _launch_command(binary)
        except AccountError:
            return {'state': 'not_installed', 'source': SOURCE, 'error': 'node_runtime_unavailable'}
        env['PATH'] = str(Path(command[0]).parent) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
        process = subprocess.Popen(command + ['web', '--host', '127.0.0.1', '--port', str(port), '--no-open'],
                                   cwd=str(RUNTIME), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        common.write_json(RUNTIME / 'account-service.json', {'pid': process.pid, 'port': port, 'started_at': time.time()})
    deadline = time.monotonic() + max(0, min(wait_seconds, 30))
    while time.monotonic() < deadline:
        if ready():
            return {'state': 'ready', 'source': SOURCE, 'owned': True}
        if process.poll() is not None:
            return {'state': 'start_failed', 'source': SOURCE}
        time.sleep(.2)
    return {'state': 'starting', 'source': SOURCE}


def status():
    result = {'source': SOURCE, 'sampled_at': time.time(), 'authenticated': False}
    try:
        data = _call('/api/v1/auth')
        managed = data.get('managed_provider') or {}
        state = managed.get('status')
        result.update(state=state if state in ('authenticated', 'expired', 'revoked', 'unauthenticated') else 'unauthenticated', service='ready')
        result['authenticated'] = state == 'authenticated'
        origin, _pid = _service()
        meta = _http(origin, '/api/v1/meta').get('data', {})
        result['service_info'] = {'host': '127.0.0.1', 'port': urllib.parse.urlsplit(origin).port,
                                  'authenticated_api': True,
                                  'version': str(meta.get('server_version', 'unknown'))[:32]}
    except AccountError as error:
        result.update(state='unavailable', authenticated=False, service='unavailable', error=str(error))
    return result


def stop():
    """Stop only the exact CLI process started and recorded by this adapter."""
    record = common.read_json(RUNTIME / 'account-service.json', {})
    pid, port = record.get('pid'), record.get('port')
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return {'state': 'not_owned', 'source': SOURCE}
    try:
        origin = _origin('127.0.0.1', port)
        if (origin, pid) not in _instances():
            return {'state': 'not_owned', 'source': SOURCE}
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                                 text=True, timeout=3, check=False).stdout.strip()
        expected = str((RUNTIME / 'node_modules' / '.bin' / 'kimi').resolve())
        if expected not in command or '--port ' + str(port) not in command or '--host 127.0.0.1' not in command:
            return {'state': 'not_owned', 'source': SOURCE}
        os.kill(pid, signal.SIGTERM)
        return {'state': 'stopping', 'source': SOURCE}
    except (OSError, ValueError, subprocess.SubprocessError, AccountError):
        return {'state': 'not_owned', 'source': SOURCE}


def _login_result(data):
    state = data.get('status')
    result = {'state': state if state in ('pending', 'authenticated', 'denied', 'expired', 'cancelled') else 'unknown', 'source': SOURCE}
    # Deliberately strip verification_uri_complete query codes and all OAuth tokens.
    url = data.get('verification_uri')
    if isinstance(url, str):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname in ('www.kimi.com', 'kimi.com', 'www.kimi.ai', 'kimi.ai', 'auth.kimi.com', 'auth.kimi.ai') and not parsed.username and not parsed.password:
            result['verification_url'] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    code = data.get('user_code')
    if isinstance(code, str) and 1 <= len(code) <= 32 and all(c.isalnum() or c == '-' for c in code):
        result['user_code'] = code
    interval = data.get('interval')
    if isinstance(interval, (int, float)) and not isinstance(interval, bool) and 1 <= interval <= 300:
        result['interval'] = interval
    timestamp = _timestamp(data.get('expires_at'))
    if timestamp:
        result['expires_at'] = timestamp
    return result


def login_start(region='mainland-cn'):
    if region not in ('mainland-cn', 'global'):
        raise ValueError('Unsupported Kimi region')
    return _login_result(_call('/api/v1/oauth/login', 'POST', {'region': region}))


def login_status():
    try:
        return _login_result(_call('/api/v1/oauth/login'))
    except AccountError as error:
        return {'state': 'unavailable', 'source': SOURCE, 'error': str(error)}


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def normalize(quota):
    """Normalize only official named account windows; absence is not zero."""
    if not isinstance(quota, dict) or not isinstance(quota.get('usages'), dict):
        raise AccountError('invalid_account_quota')
    usages = quota['usages']
    result = {'state': 'ok', 'source': SOURCE, 'sampled_at': time.time(), 'stale': False,
              'windows': [], 'balances': [], 'weekly_applicability': 'unknown'}
    for name, kind, duration in WINDOWS:
        entry = usages.get(name)
        if not isinstance(entry, dict):
            continue
        ratio = entry.get('usedRatio')
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
            continue
        result['windows'].append({'name': name, 'kind': kind, 'limit': 1, 'remaining': 1 - ratio,
                                  'remaining_percent': round((1 - ratio) * 100, 3), 'duration_minutes': duration,
                                  'resets_at': _timestamp(entry.get('resetAt')), 'valid': True, 'source': SOURCE})
    if any(w['kind'] == 'weekly' for w in result['windows']):
        result['weekly_applicability'] = 'applicable'
    elif 'limit7d' not in usages and any(w['kind'] == 'month_total' for w in result['windows']):
        result['weekly_applicability'] = 'not_applicable'
    result['available'] = (all(w['remaining'] > 0 for w in result['windows']) if result['windows'] else None)
    return result


def _account_id():
    data = _call('/api/v1/oauth/userinfo')
    info = data.get('userInfo')
    value = (info.get('userId') or info.get('globalId')) if isinstance(info, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise AccountError('account_identity_unavailable')
    return _hash(value), info


def credential_identity():
    """Background-only OAuth-account/inference-key identity, no rotating secrets."""
    try:
        account, _info = _account_id()
        key = common.credential_identity('kimi-for-coding')
        return _hash(account + ':' + key) if key else None
    except (AccountError, ValueError, OSError):
        return None


def _inference_identity():
    """Read official API-key ownership; redirects cannot receive the key."""
    try:
        key = common.auth_key('kimi-for-coding')
        request = urllib.request.Request('https://api.kimi.com/coding/v1/me',
                                        headers={'Authorization': 'Bearer ' + key,
                                                 'Accept': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            return {}
        return {out: value[source] for out, source in (('userId', 'user_id'), ('globalId', 'global_id'))
                if isinstance(value.get(source), str) and value[source].strip()}
    except (OSError, ValueError, TypeError):
        return {}


def _binding(account, info=None):
    key = common.credential_identity('kimi-for-coding')
    # Compare freshly authenticated official identities, never JWT payloads or
    # coincident quota percentages. Neither IDs nor API keys leave this call.
    inference = _inference_identity()
    if isinstance(info, dict) and inference:
        comparisons = [info[field] == inference[field] for field in ('userId', 'globalId')
                       if isinstance(info.get(field), str) and info[field].strip()
                       and isinstance(inference.get(field), str) and inference[field].strip()]
        if comparisons:
            return {'state': 'verified', 'method': 'official_identity_match'} if any(comparisons) else {'state': 'mismatch'}
    stored = common.read_json(common.STATE / 'kimi-account-binding.json', {})
    if not stored:
        return {'state': 'unverified'}
    if stored.get('account_hash') == account and stored.get('inference_hash') == key and key:
        return {'state': 'verified', 'method': 'user_confirmed', 'confirmed_at': stored.get('confirmed_at')}
    return {'state': 'mismatch'}


def bind_current_account(confirmed=False):
    """Call only following explicit human confirmation of this account/key pair."""
    if confirmed is not True:
        raise ValueError('Explicit same-account confirmation is required')
    account, _info = _account_id()
    key = common.credential_identity('kimi-for-coding')
    if not key:
        raise AccountError('inference_credential_unavailable')
    common.write_json(common.STATE / 'kimi-account-binding.json',
                      {'account_hash': account, 'inference_hash': key, 'confirmed_at': time.time()})
    return _binding(account)


def fetch():
    try:
        data = _call('/api/v1/oauth/usage')
        if data.get('kind') != 'ok':
            raise AccountError('account_usage_unavailable')
        result = normalize(data.get('quota'))
        try:
            account, info = _account_id()
            result['account_binding'] = _binding(account, info)
            # Only allow a short human-readable nickname/plan, never contact details.
            result['account'] = {k: str(info[k])[:80] for k in ('nickname', 'userLevelName') if isinstance(info.get(k), str)}
        except AccountError:
            result['account_binding'] = {'state': 'unverified'}
        oauth = {'state': 'ok', 'source': SOURCE}
    except AccountError as error:
        result = {'state': 'unavailable', 'source': SOURCE, 'sampled_at': time.time(), 'stale': True,
                'available': None, 'windows': [], 'balances': [], 'weekly_applicability': 'unknown',
                'account_binding': {'state': 'unverified'}, 'error': str(error)}
        oauth = {'state': 'unavailable', 'source': SOURCE, 'error': str(error)}
    # Website quota is independent of the local CLI's liveness and OAuth state.
    web = fetch_web_total()
    result['oauth_account'] = oauth
    result['web_account'] = {key: value for key, value in web.items()
                             if key not in ('windows', 'available', 'sampled_at')}
    if web.get('state') == 'ok' and web.get('account_binding', {}).get('state') == 'verified':
        result['windows'] = [w for w in result['windows'] if w.get('kind') != 'month_total'] + web['windows']
        result['available'] = all(w['remaining'] > 0 for w in result['windows'])
        if oauth['state'] != 'ok':
            result.update(state='ok', stale=False, sampled_at=web['sampled_at'],
                          account_binding=web['account_binding'])
            result.pop('error', None)
    return result


def _valid_web_tokens(payload):
    if isinstance(payload, bytes):
        if len(payload) > 65536:
            raise AccountError('invalid_web_session')
        try:
            payload = json.loads(payload)
        except (ValueError, UnicodeError):
            raise AccountError('invalid_web_session') from None
    if not isinstance(payload, dict):
        raise AccountError('invalid_web_session')
    result = {}
    for name in ('access_token', 'refresh_token'):
        value = payload.get(name, payload.get('accessToken' if name == 'access_token' else 'refreshToken'))
        if not isinstance(value, str) or not 1 <= len(value) <= 24000 or any(c in value for c in '\r\n\0'):
            raise AccountError('invalid_web_session')
        result[name] = value
    return result


def _vault(action, payload=None):
    request = {'action': action, 'service': WEB_KEYCHAIN_SERVICE, 'account': WEB_KEYCHAIN_ACCOUNT}
    if action == 'put':
        if not isinstance(payload, bytes) or len(payload) > 65536:
            raise AccountError('invalid_web_session')
        request['payload'] = base64.b64encode(payload).decode('ascii')
    try:
        result = subprocess.run([VAULT_PYTHON, '-c', VAULT_HELPER, str(Path(__file__).resolve().parent)],
                                input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=VAULT_TIMEOUT,
                                check=False, close_fds=True)
    except subprocess.TimeoutExpired:
        # TimeoutExpired may hold private partial stdout; never chain/return it.
        raise AccountError('web_keychain_timeout') from None
    except (OSError, subprocess.SubprocessError):
        raise AccountError('web_keychain_unavailable') from None
    if result.returncode == 4 and action == 'get':
        return None
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise AccountError('web_keychain_unavailable')
    return result.stdout


def _vault_get():
    return _vault('get')


def _vault_put(payload):
    _vault('put', payload)


def _web_load():
    try:
        raw = _vault_get()
        if raw is None:
            return None
        value = json.loads(raw)
        tokens = _valid_web_tokens(value)
        for name in ('account_hash', 'inference_hash', 'imported_at', 'verified_at'):
            if name in value:
                tokens[name] = value[name]
        return tokens
    except AccountError as error:
        if str(error) != 'invalid_web_session':
            raise
        return None
    except (ValueError, TypeError):
        return None


def _web_save(session):
    try:
        _vault_put(json.dumps(session, separators=(',', ':')).encode())
    except keychain_secrets.KeychainError:
        raise AccountError('web_keychain_unavailable') from None


def _web_request(url, body, access_token=None):
    """Exact public website contracts only; no caller-provided destinations."""
    if url not in (WEB_STATS_URL, WEB_USER_URL, WEB_REFRESH_URL):
        raise AccountError('unsupported_web_endpoint')
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
               'Connect-Protocol-Version': '1', 'x-msh-platform': 'web'}
    if url != WEB_REFRESH_URL:
        if not isinstance(access_token, str) or not access_token or any(c in access_token for c in '\r\n\0'):
            raise AccountError('invalid_web_session')
        headers['Authorization'] = 'Bearer ' + access_token
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise AccountError('invalid_web_response')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AccountError('invalid_web_response')
        return value
    except urllib.error.HTTPError as error:
        raise AccountError('web_http_' + str(error.code)) from None
    except (OSError, ValueError):
        raise AccountError('web_service_unavailable') from None


def _web_call(session, url, body, persist=True):
    try:
        return _web_request(url, body, session['access_token'])
    except AccountError as error:
        if str(error) != 'web_http_401':
            raise
    # Same exact refresh contract used by the official website. Never substitute
    # the Code OAuth refresh token or infer expiry from an unverified JWT.
    try:
        refreshed = _valid_web_tokens(_web_request(WEB_REFRESH_URL, {
            'refresh_token': session['refresh_token']}))
    except AccountError as error:
        if str(error) in ('web_http_401', 'web_http_403'):
            if persist:
                _web_save({})
            raise AccountError('web_reauth_required') from None
        raise
    session.update(refreshed)
    if persist:
        _web_save(session)
    try:
        return _web_request(url, body, session['access_token'])
    except AccountError as error:
        if str(error) == 'web_http_401':
            if persist:
                _web_save({})
            raise AccountError('web_reauth_required') from None
        raise


def _web_account_binding(session, persist=True):
    value = _web_call(session, WEB_USER_URL, {}, persist=persist)
    user = value.get('user')
    if not isinstance(user, dict):
        return {'state': 'unverified'}
    inference = _inference_identity()
    user = dict(user)
    if 'global_id' not in user and 'globalId' in user:
        user['global_id'] = user['globalId']
    comparisons = [user[name] == inference[target] for name, target in (
        ('id', 'userId'), ('global_id', 'globalId'))
        if isinstance(user.get(name), str) and user[name].strip()
        and isinstance(inference.get(target), str) and inference[target].strip()]
    if not comparisons:
        return {'state': 'unverified'}
    if not any(comparisons):
        return {'state': 'mismatch'}
    identifier = user.get('id') or user.get('global_id')
    session.update(account_hash=_hash(identifier),
                   inference_hash=common.credential_identity('kimi-for-coding'),
                   verified_at=time.time())
    return {'state': 'verified', 'method': 'official_identity_match'}


def import_web_session(payload, confirmed=False):
    """Explicitly authorized stdin/in-process import; secrets go only to Keychain.

    payload is {access_token, refresh_token}. The caller must capture it privately;
    never pass tokens in argv, URLs, config, reports or logs.
    """
    if confirmed is not True:
        raise ValueError('Explicit permission to save the web session is required')
    session = _valid_web_tokens(payload)
    # Verify before saving anything. A failed import cannot replace a good grant.
    binding = _web_account_binding(session, persist=False)
    if binding['state'] != 'verified':
        return {'state': 'account_mismatch' if binding['state'] == 'mismatch' else 'identity_unverified',
                'source': WEB_SOURCE, 'account_binding': binding}
    session['imported_at'] = time.time()
    _web_save(session)
    return {'state': 'connected', 'source': WEB_SOURCE, 'account_binding': binding}


def forget_web_session(confirmed=False):
    if confirmed is not True:
        raise ValueError('Explicit permission to remove the web session is required')
    _web_save({})
    return {'state': 'disconnected', 'source': WEB_SOURCE}


def web_status():
    try:
        session = _web_load()
    except AccountError as error:
        return {'state': 'unavailable', 'source': WEB_SOURCE, 'error': str(error)}
    if session is None:
        return {'state': 'not_connected', 'source': WEB_SOURCE}
    key = common.credential_identity('kimi-for-coding')
    state = 'connected' if key and session.get('inference_hash') == key else 'account_mismatch'
    return {'state': state, 'source': WEB_SOURCE,
            'imported_at': session.get('imported_at'), 'verified_at': session.get('verified_at')}


def normalize_web_total(value):
    balance = value.get('subscription_balance', value.get('subscriptionBalance')) if isinstance(value, dict) else None
    ratio = balance.get('amount_used_ratio', balance.get('amountUsedRatio')) if isinstance(balance, dict) else None
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise AccountError('web_total_unavailable')
    # kimi_code_used_ratio is consumption within the total pool, not an
    # independent monthly Code quota. Gifts/booster wallets are separate too.
    return {'name': 'monthTotal', 'kind': 'month_total', 'limit': 1,
            'remaining': 1 - ratio, 'remaining_percent': round((1 - ratio) * 100, 3),
            'duration_minutes': None, 'resets_at': None,
            'expires_at': _timestamp(balance.get('expire_time', balance.get('expireTime'))),
            'valid': True, 'source': WEB_SOURCE}


def fetch_web_total():
    base = {'source': WEB_SOURCE, 'sampled_at': time.time(), 'windows': [],
            'account_binding': {'state': 'unverified'}}
    try:
        session = _web_load()
    except AccountError as error:
        return dict(base, state='unavailable', error=str(error))
    if session is None:
        return dict(base, state='not_connected')
    try:
        binding = _web_account_binding(session)
        base['account_binding'] = binding
        if binding['state'] != 'verified':
            return dict(base, state='account_mismatch' if binding['state'] == 'mismatch' else 'identity_unverified')
        _web_save(session)
        grant = _hash(session['access_token'])
        stats = _web_call(session, WEB_STATS_URL, {'mute_notice': False})
        if grant != _hash(session['access_token']):
            binding = _web_account_binding(session)
            base['account_binding'] = binding
            if binding['state'] != 'verified':
                return dict(base, state='account_mismatch' if binding['state'] == 'mismatch' else 'identity_unverified')
        window = normalize_web_total(stats)
        return dict(base, state='ok', windows=[window], available=window['remaining'] > 0)
    except AccountError as error:
        return dict(base, state='reauth_required' if str(error) == 'web_reauth_required' else 'unavailable',
                    error=str(error))
