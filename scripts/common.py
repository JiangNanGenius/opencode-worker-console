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


def init():
    for p in [STATE, STATE / 'tasks', STATE / 'artifacts', STATE / 'logs', STATE / 'worktrees']:
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


def request(url, method='GET', data=None, headers=None, timeout=15):
    h = dict(headers or {})
    raw = None
    if data is not None:
        raw = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    try:
        req = urllib.request.Request(url, data=raw, headers=h, method=method)
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else None
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


def api(path, directory=None, method='GET', data=None, timeout=15):
    c = config()
    base = c['server_url']
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
        raise ValueError('OpenCode server must use HTTP on 127.0.0.1')
    if directory:
        path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'directory': directory})
    pw = (STATE / 'server-password').read_text().strip()
    auth = base64.b64encode(('opencode:' + pw).encode()).decode()
    return request(base + path, method, data, {'Authorization': 'Basic ' + auth}, timeout)


def public_task(t):
    keys = ['id', 'title', 'status', 'profile', 'requested_profile', 'mode', 'urgency', 'complexity',
            'group_id', 'group_title', 'parent_task_id', 'owner_thread_id',
            'workspace', 'source_dir', 'directory', 'scopes', 'resources', 'session_id', 'session_deleted', 'session_directory', 'guidance',
            'created_at', 'started_at', 'finished_at', 'reason', 'queue_reason', 'route_reason',
            'actual_models', 'review_required', 'artifact_dir', 'summary', 'elapsed_seconds', 'errors', 'auto_approve']
    return {k: t[k] for k in keys if k in t}


def redact(value):
    raw = json.dumps(value, ensure_ascii=False)
    auth_path = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'opencode/auth.json'
    for provider in read_json(auth_path, {}):
        try:
            raw = raw.replace(auth_key(provider), '<redacted>')
        except ValueError:
            pass
    raw = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '<redacted>', raw)
    return json.loads(raw)


def message_id():
    # OpenCode ascending IDs use the low 48 bits of a millisecond timestamp << 12.
    stamp = (int(time.time() * 1000) * 4096 + 1) & ((1 << 48) - 1)
    return "msg_" + format(stamp, "012x") + secrets.token_hex(7)
