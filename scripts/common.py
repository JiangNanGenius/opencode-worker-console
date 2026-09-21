"""Private durable state and authenticated OpenCode HTTP client (stdlib only)."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import time
import secrets
import urllib.error
import urllib.parse
import urllib.request

STATE = Path(os.environ.get('DELEGATE_STATE', '~/.local/state/delegate-opencode')).expanduser()
CONFIG = Path(os.environ.get('DELEGATE_CONFIG', '~/.config/opencode/delegate-pool.json')).expanduser()
ACTIVE = {'starting', 'running', 'stopping', 'uncertain'}
TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'needs_attention'}
CANCEL_REASONS = {'user_requested', 'superseded', 'wrong_scope', 'duplicate', 'side_effect_risk',
                  'service_stop'}


def init():
    for p in [STATE, STATE / 'tasks', STATE / 'artifacts', STATE / 'logs', STATE / 'worktrees',
              STATE / 'credentials', STATE / 'usage-ledger']:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.chmod(0o700)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(name='state'):
    init()
    with open(STATE / (name + '.lock'), 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def config():
    c = read_json(CONFIG)
    if not c:
        raise RuntimeError('Pool is not installed; run scripts/install.py')
    return c


def task_path(task_id):
    if not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in task_id):
        raise ValueError('Invalid task ID')
    return STATE / 'tasks' / (task_id + '.json')


def task(task_id):
    t = read_json(task_path(task_id))
    if not t:
        raise ValueError('Unknown task ID')
    return t


def update(task_id, **fields):
    with locked():
        t = task(task_id)
        t.update(fields, updated_at=time.time())
        write_json(task_path(task_id), t)
    return t


def request_cancel(task_id, reason, source='cli'):
    """Record an auditable cancellation request; elapsed time is not a reason.

    The public CLI deliberately exposes only concrete outcome reasons. Internal service
    shutdown uses ``service_stop`` after it has already confirmed the native worker abort.
    """
    if reason not in CANCEL_REASONS:
        raise ValueError('Cancellation reason must be one of: ' + ', '.join(sorted(CANCEL_REASONS)))
    if source not in ('cli', 'console', 'service'):
        raise ValueError('Invalid cancellation source')
    with locked():
        t = task(task_id)
        if t.get('status') in TERMINAL:
            return t
        now = time.time()
        t.update(cancel_requested=True,
                 cancellation={'reason': reason, 'source': source, 'requested_at': now},
                 updated_at=now)
        write_json(task_path(task_id), t)
    return t


def tasks():
    init()
    return sorted([read_json(p) for p in (STATE / 'tasks').glob('*.json')], key=lambda x: x['created_at'])


def artifact_dir(task_id):
    p = STATE / 'artifacts' / task_id
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def auth_key(provider):
    # Secrets are never returned to the caller or serialized in task state.
    p = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/auth.json'
    value = read_json(p, {}).get(provider, {})
    if value.get('type') != 'api' or not value.get('key'):
        raise ValueError('No API credential for ' + provider)
    return value['key']


def credential_identity(provider):
    try:
        return hashlib.sha256(auth_key(provider).encode()).hexdigest()
    except ValueError:
        return None


class HttpFailure(Exception):
    def __init__(self, status=None, message=None):
        self.status = status
        self.message = redact(str(message))[:4000] if message else None
        label = 'HTTP ' + str(status) if status else 'Transport failure'
        super().__init__(label + (': ' + self.message if self.message else ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, method='GET', data=None, headers=None, timeout=15, include_cursor=False):
    h = dict(headers or {})
    raw = None
    if data is not None:
        raw = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    try:
        req = urllib.request.Request(url, data=raw, headers=h, method=method)
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            body = r.read()
            value = json.loads(body) if body else None
            return (value, r.headers.get('X-Next-Cursor')) if include_cursor else value
    except urllib.error.HTTPError as e:
        # Extract only human-readable error fields, never headers or arbitrary body data.
        message = None
        try:
            value = json.loads(e.read(32768))
            if isinstance(value, dict):
                for item in (value.get('error'), value.get('data'), value):
                    if isinstance(item, dict) and isinstance(item.get('message'), str):
                        message = item['message']; break
                    if isinstance(item, str):
                        message = item; break
        except (ValueError, OSError):
            pass
        raise HttpFailure(e.code, message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HttpFailure(message=str(e)) from None


def api(path, directory=None, method='GET', data=None, timeout=15, include_cursor=False):
    c = config()
    base = c['server_url']
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
        raise ValueError('OpenCode server must use HTTP on 127.0.0.1')
    if directory:
        path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'directory': directory})
    pw = (STATE / 'server-password').read_text().strip()
    auth = base64.b64encode(('opencode:' + pw).encode()).decode()
    return request(base + path, method, data, {'Authorization': 'Basic ' + auth}, timeout, include_cursor)


def public_task(t):
    keys = ['id', 'title', 'status', 'profile', 'requested_profile', 'profile_reason', 'tier', 'mode', 'urgency', 'complexity',
            'group_id', 'group_title', 'parent_task_id', 'owner_thread_id',
            'workspace', 'source_dir', 'directory', 'scopes', 'targets', 'resources',
            'excluded_providers', 'session_id', 'session_deleted', 'session_directory', 'guidance',
            'route_history',
            'created_at', 'started_at', 'finished_at', 'reason', 'queue_reason', 'route_reason',
            'actual_models', 'fallback_used', 'routing_notice', 'review_required', 'cancellation', 'artifact_dir',
            'summary', 'elapsed_seconds', 'errors', 'recovery', 'auto_approve']
    return {k: t[k] for k in keys if k in t}


def secret_representations(value):
    """Literal and common reversible encodings of a secret for shared redaction.

    Covers the raw value, URL percent-encoding, JSON-escaped content, standard and
    URL-safe Base64 (padded and unpadded) and, when the value already has the
    ``user:password`` shape, its Base64 HTTP Basic credential form. Used for both
    known local OpenCode credentials and injected credential-reference values so
    the redaction logic is identical everywhere. This is exposure reduction, not a
    sandbox and not a guarantee that arbitrary text secrets are detected.
    """
    if not isinstance(value, str) or not value:
        return []
    reps = {value}
    try:
        reps.add(urllib.parse.quote(value, safe=''))
        reps.add(urllib.parse.quote_plus(value, safe=''))
    except Exception:
        pass
    for ensure_ascii in (False, True):
        try:
            reps.add(json.dumps(value, ensure_ascii=ensure_ascii)[1:-1])
        except Exception:
            pass
    try:
        data = value.encode('utf-8')
    except Exception:
        data = None
    if data:
        for encoded in (base64.b64encode(data), base64.urlsafe_b64encode(data)):
            text = encoded.decode('ascii')
            reps.add(text)
            reps.add(text.rstrip('='))
        if b':' in data:
            reps.add(base64.b64encode(data).decode('ascii'))
    return sorted((r for r in reps if r), key=len, reverse=True)


def redact(value):
    auth_path = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/auth.json'
    secrets = set()
    for auth in read_json(auth_path, {}).values():
        if isinstance(auth, dict):
            for k, v in auth.items():
                if k in ('key', 'access', 'refresh', 'access_token', 'refresh_token') and \
                        isinstance(v, str) and len(v) >= 8:
                    secrets.update(secret_representations(v))
    password = STATE / 'server-password'
    if password.is_file():
        # A known local protection must fail closed: if the owner-only password
        # cannot be read, surface the error instead of silently skipping redaction.
        server_password = password.read_text().strip()
        if server_password:
            secrets.update(secret_representations(server_password))
            # HTTP Basic has a user component the raw secret cannot express; the local
            # server always authenticates as `opencode`. Add that exact blob so a
            # captured Authorization header is redacted without exposing the password.
            try:
                secrets.add(base64.b64encode(('opencode:' + server_password).encode()).decode())
            except Exception:
                pass
    # Registered local references are resolved lazily so this module stays importable
    # without them and no circular import is created. Metadata-only, owner-only reads.
    try:
        import credentials as credential_refs
        for registered in credential_refs.redaction_values():
            secrets.update(secret_representations(registered))
    except Exception:
        pass
    secrets = sorted(secrets, key=len, reverse=True)
    sensitive = {'authorization', 'api_key', 'apikey', 'access_token', 'refresh_token',
                 'password', 'private_key', 'cookie', 'set-cookie'}
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for k, v in item.items():
                if isinstance(k, str):
                    key = clean(k)
                    if k.lower() in sensitive:
                        result[key] = '<redacted>'
                        continue
                else:
                    key = k
                result[key] = clean(v)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, '<redacted>')
            item = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})', '<redacted>', item)
            item = re.sub(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----', '<redacted private key>', item)
        return item
    return clean(value)


def message_id():
    # OpenCode ascending IDs use the low 48 bits of a millisecond timestamp << 12.
    stamp = (int(time.time() * 1000) * 4096 + 1) & ((1 << 48) - 1)
    return "msg_" + format(stamp, "012x") + secrets.token_hex(7)
