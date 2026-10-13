"""Lifecycle and configuration for the local OpenViking memory server.

The server is the published prebuilt `openviking` wheel running from a dedicated
internal-disk virtual environment. Persistent model credentials are never written
into configuration: `ov.conf` keeps only `${OPENVIKING_*_API_KEY}` references and
this module injects the real values through the child environment at start time.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import common

URL = 'http://127.0.0.1:1933'
OV_HOME = Path.home() / '.openviking'


def runtime():
    return common.STATE / 'runtime/openviking'


def venv_bin(name):
    return runtime() / 'venv/bin' / name


def workspace():
    return common.STATE / 'openviking/workspace'


def state_dir():
    return common.STATE / 'openviking'


def logs_dir():
    return state_dir() / 'logs'


def conf_path():
    return OV_HOME / 'ov.conf'


def cli_conf_path():
    return OV_HOME / 'ovcli.conf'


def pid_path():
    return state_dir() / 'openviking.pid'


def _settings():
    cfg = common.config().get('memory') if isinstance(common.config().get('memory'), dict) else {}
    ov = cfg.get('openviking') if isinstance(cfg.get('openviking'), dict) else {}
    out = {'url': URL, 'workspace': str(workspace())}
    out['url'] = ov.get('url') or out['url']
    out['workspace'] = ov.get('workspace') or out['workspace']
    return out


def embedding_settings():
    cfg = common.config().get('memory') if isinstance(common.config().get('memory'), dict) else {}
    return {
        'api_url': (cfg.get('embedding_api_url') or 'https://ark.cn-beijing.volces.com/api/plan/v3').rstrip('/'),
        'model': cfg.get('embedding_model') or 'doubao-embedding-vision',
        'dimensions': int(cfg.get('embedding_dimensions') or 2048),
    }


def vlm_settings():
    # Settled decision: the VLM is DeepSeek Flash from the existing direct profile.
    cfg = common.config().get('memory') if isinstance(common.config().get('memory'), dict) else {}
    ov = cfg.get('openviking') if isinstance(cfg.get('openviking'), dict) else {}
    return {
        'provider': 'openai',
        'api_base': ov.get('vlm_api_base') or 'https://api.deepseek.com',
        'model': ov.get('vlm_model') or 'deepseek-flash',
    }


def service_env():
    """Child environment with model credentials resolved from owner-only local auth."""
    env = dict(os.environ)
    env['OPENVIKING_ARK_API_KEY'] = common.auth_key('volcengine-agent-plan')
    env['OPENVIKING_DEEPSEEK_API_KEY'] = common.auth_key('deepseek')
    env['OPENVIKING_ROOT_API_KEY'] = root_api_key(create=True)
    user_key = api_key()
    if user_key:
        # Trusted child processes (plugin, MCP proxy, CLI) share the data key
        # through the environment; it never enters configuration files.
        env['OPENVIKING_API_KEY'] = user_key
    return env


def root_api_key(create=False):
    """Owner-only root credential for the local server; never logged or returned."""
    path = common.STATE / 'credentials/openviking-root-key'
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        import secrets
        path.write_text(secrets.token_urlsafe(32))
    if not path.exists():
        return None
    key = path.read_text().strip()
    path.chmod(0o600)
    return key or None


def api_key_path():
    return common.STATE / 'credentials/openviking-api-key'


def api_key():
    """Owner-only data key for authenticated memory CRUD and search."""
    path = api_key_path()
    if not path.exists():
        return None
    key = path.read_text().strip()
    path.chmod(0o600)
    return key or None


def _save_api_key(key):
    if not isinstance(key, str) or not key.strip():
        raise ValueError('OpenViking did not return a user key')
    path = api_key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(key.strip())
    path.chmod(0o600)


def _admin_request(path, data=None, timeout=30):
    key = root_api_key(create=True)
    if not key:
        raise ValueError('OpenViking root credential is unavailable')
    url = _settings()['url'] + path
    try:
        payload = common.request(url, 'POST' if data is not None else 'GET', data,
                                 headers={'X-API-Key': key}, timeout=timeout)
    except common.HttpFailure as error:
        raise ValueError(str(error)) from None
    if not isinstance(payload, dict) or payload.get('error'):
        raise ValueError('OpenViking admin request failed')
    return payload.get('result', payload)


def bootstrap_key():
    """Create the default account/user key once, then reuse the stored owner-only file.

    Fresh installs auto-create the default account, so both the account and user
    creation calls tolerate ALREADY_EXISTS; the key endpoint below is the source
    of truth. The returned key is written owner-only and never logged.
    """
    existing = api_key()
    if existing:
        return {'created': False}
    account, user = 'default', 'default'
    for path, payload in (
        ('/api/v1/admin/accounts', {'account_id': account, 'admin_user_id': user}),
        ('/api/v1/admin/accounts/{}/users'.format(account), {'user_id': user, 'role': 'admin'}),
    ):
        try:
            _admin_request(path, payload)
        except ValueError:
            pass  # Already exists, or the key endpoint below reports the real error.
    result = _admin_request('/api/v1/admin/accounts/{}/users/{}/key'.format(account, user), {})
    user_key = (result or {}).get('user_key') if isinstance(result, dict) else None
    _save_api_key(user_key)
    return {'created': True, 'account': account, 'user': user}


def _redact(text):
    """Remove resolved credential values and common encodings from captured output."""
    if not text:
        return text
    values = [api_key(), root_api_key()]
    for provider in ('volcengine-agent-plan', 'deepseek'):
        try:
            value = common.auth_key(provider)
        except ValueError:
            continue
        values.append(value)
    for value in values:
        if not value:
            continue
        for variant in (value, value.encode().hex()):
            if variant:
                text = text.replace(variant, '<redacted>')
    return common.redact(text)


def _console_origins():
    """Explicit loopback origins for browser CORS; never a wildcard."""
    origins = ['http://127.0.0.1:1933', 'http://localhost:1933']
    try:
        console = str(common.config().get('console_url') or '')
    except RuntimeError:
        console = ''
    from urllib.parse import urlsplit
    parsed = urlsplit(console)
    if parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost'):
        origins.append(console.rstrip('/'))
    return origins


def runtime_config():
    embedding = embedding_settings()
    vlm = vlm_settings()
    return {
        'embedding': {
            'max_concurrent': 4,
            'max_retries': 3,
            'text_source': 'content_only',
            'max_input_tokens': 4096,
            'dense': {
                'provider': 'openai',
                'api_base': embedding['api_url'],
                'api_key': '${OPENVIKING_ARK_API_KEY}',
                'model': embedding['model'],
                'dimension': embedding['dimensions'],
                'input': 'text',
                'encoding_format': 'float',
            },
        },
        'vlm': {
            'provider': vlm['provider'],
            'api_base': vlm['api_base'],
            'api_key': '${OPENVIKING_DEEPSEEK_API_KEY}',
            'model': vlm['model'],
            'temperature': 0,
            'max_retries': 3,
            'timeout': 600,
        },
        'storage': {
            'workspace': _settings()['workspace'],
            'agfs': {'backend': 'local'},
            'vectordb': {'backend': 'local'},
        },
        'server': {'host': '127.0.0.1', 'port': _port(), 'workers': 1,
                   'auth_mode': 'api_key',
                   'root_api_key': '${OPENVIKING_ROOT_API_KEY}',
                   'cors_origins': _console_origins()},
        'memory': {'extraction_enabled': True},
        'log': {'level': 'info'},
    }


def _port():
    from urllib.parse import urlsplit
    return urlsplit(_settings()['url']).port or 1933


def write_runtime_config():
    """Materialize ov.conf/ovcli.conf with env references only, preserving extra fields."""
    OV_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    (state_dir() / 'logs').mkdir(parents=True, exist_ok=True, mode=0o700)

    existing = common.read_json(conf_path(), {}) or {}
    if conf_path().exists():
        backup = conf_path().with_name('ov.conf.bak-' + time.strftime('%Y%m%d-%H%M%S'))
        if not backup.exists():
            common.write_json(backup, existing)
            backup.chmod(0o600)
        # Keep a short, bounded history; the standard backup convention is a
        # handful of readable previous revisions, not one file per service start.
        backups = sorted(OV_HOME.glob('ov.conf.bak-*'), reverse=True)
        for stale in backups[5:]:
            try:
                stale.unlink()
            except OSError:
                pass
    def merge(current, managed):
        result = dict(current) if isinstance(current, dict) else {}
        for key, value in managed.items():
            result[key] = merge(result.get(key), value) if isinstance(value, dict) else value
        return result

    merged = merge(existing, runtime_config())
    common.write_json(conf_path(), merged)
    conf_path().chmod(0o600)

    cli_existing = common.read_json(cli_conf_path(), {}) or {}
    cli_merged = dict(cli_existing)
    cli_merged.update({'url': _settings()['url'],
                       # The loader expands environment references, so the CLI
                       # and Studio never persist the data key itself.
                       'api_key': '${OPENVIKING_API_KEY}'})
    common.write_json(cli_conf_path(), cli_merged)
    cli_conf_path().chmod(0o600)
    return {'config': str(conf_path()), 'cli_config': str(cli_conf_path()),
            'workspace': _settings()['workspace']}


def probe(url=None, timeout=3):
    base = (url or _settings()['url']).rstrip('/')
    result = {}
    for name in ('health', 'ready'):
        try:
            result[name] = common.request(base + '/' + name, timeout=timeout)
        except Exception as error:
            result[name] = {'unavailable': common.redact(str(error))}
    result['healthy'] = bool(isinstance(result.get('health'), dict) and
                             result['health'].get('healthy') is True)
    result['ready'] = bool(isinstance(result.get('ready'), dict) and
                           result['ready'].get('status') == 'ready')
    return result


def _identity(pid):
    out = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return out.stdout.decode().strip() if out.returncode == 0 else None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _verified_pid(pid):
    """True only for a live process that still runs the OpenViking server binary."""
    if not isinstance(pid, int) or not _alive(pid):
        return False
    out = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'command='],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    import shlex
    try:
        uid, command = out.stdout.decode().strip().split(None, 1)
        argv = shlex.split(command)
        binary = str(venv_bin('openviking-server'))
        config_index = argv.index('--config') + 1
        return (out.returncode == 0 and int(uid) == os.getuid() and binary in argv[:2]
                and Path(argv[config_index]).resolve() == conf_path().resolve())
    except (ValueError, IndexError):
        return False


def _listener_pid():
    """Pid of the process listening on our loopback port, when it is our server."""
    import shutil
    lsof = shutil.which('lsof') or '/usr/sbin/lsof'
    out = subprocess.run([lsof, '-nP', '-iTCP:' + str(_port()), '-sTCP:LISTEN', '-t'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        pid = int(out.stdout.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return pid if _verified_pid(pid) else None


def _pid_file_record():
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if not _verified_pid(pid):
        return None
    return {'pid': pid, 'identity': _identity(pid), 'source': 'pid-file'}


def running_record():
    record = common.read_json(state_dir() / 'service.json', {}) or {}
    pid = record.get('pid')
    if isinstance(pid, int) and _verified_pid(pid) and _identity(pid) == record.get('identity'):
        return record
    pid_file = _pid_file_record()
    if pid_file:
        return pid_file
    listener = _listener_pid()
    if listener:
        return {'pid': listener, 'identity': _identity(listener), 'source': 'listener'}
    return None


def start(timeout=180):
    """Start the loopback server if it is not already answering."""
    write_runtime_config()
    record = running_record()
    if record and probe()['ready']:
        verify_auth()
        return status()
    if record and not _alive(record['pid']):
        record = None
    if record is None:
        # Adopt a live listener that predates this module's service record (an
        # earlier launcher revision), so start/stop stay idempotent instead of
        # racing a second process against the data-directory lock.
        listener = _listener_pid()
        if listener:
            common.write_json(state_dir() / 'service.json',
                              {'pid': listener, 'identity': _identity(listener), 'adopted': True})
            if probe()['ready']:
                verify_auth()
                return status()
            raise RuntimeError('OpenViking is listening but not ready; stop it before restarting')
    binary = venv_bin('openviking-server')
    if not binary.exists():
        raise ValueError('OpenViking runtime is not installed; run the documented install first')
    logpath = logs_dir() / 'openviking.log'
    log = logpath.open('ab')
    proc = subprocess.Popen(
        [str(binary), '--config', str(conf_path())],
        cwd=str(state_dir()), env=service_env(), stdin=subprocess.DEVNULL,
        stdout=log, stderr=log, start_new_session=True, close_fds=True)
    common.write_json(state_dir() / 'service.json',
                      {'pid': proc.pid, 'identity': _identity(proc.pid), 'started_at': time.time()})
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe(timeout=2)['ready']:
            bootstrap_key()
            verify_auth()
            return status()
        if not _alive(proc.pid):
            raise RuntimeError('OpenViking server exited during startup; inspect ' + str(logpath))
        time.sleep(0.5)
    raise RuntimeError('OpenViking server did not become ready; inspect ' + str(logpath))


def verify_auth():
    """Readiness alone must never adopt an unauthenticated development server."""
    health = probe().get('health') or {}
    if health.get('auth_mode') != 'api_key' or not api_key():
        raise ValueError('OpenViking API-key authentication is not configured')
    _admin_request('/api/v1/admin/accounts')
    # Read-only user settings validate the data key, without reading memory.
    request('/api/v1/admin/accounts/default/users/default/settings')
    return True


def stop(timeout=30):
    record = running_record()
    if not record:
        pid_path().unlink(missing_ok=True)
        return {'running': False}
    pid = record['pid']
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.25)
    if _alive(pid):
        raise RuntimeError('OpenViking server is still stopping; retry later')
    pid_path().unlink(missing_ok=True)
    # The recorded identity belongs to the stopped process; clear it so status
    # and the next start() cannot mistake a dead pid for a live server.
    service_path = state_dir() / 'service.json'
    if (common.read_json(service_path, {}) or {}).get('pid') == pid:
        service_path.unlink(missing_ok=True)
    return {'running': False, 'stopped_pid': pid}


def status():
    record = running_record()
    result = {'running': bool(record), 'url': _settings()['url'],
              'config': str(conf_path()), 'workspace': _settings()['workspace'],
              'venv': str(venv_bin('openviking-server')),
              'auth_mode': (common.read_json(conf_path(), {}) or {}).get('server', {}).get('auth_mode', 'dev'),
              'data_key': bool(api_key())}
    if record:
        result['pid'] = record['pid']
    probe_result = probe()
    result.update({'healthy': probe_result['healthy'], 'ready': probe_result['ready'],
                   'health': probe_result.get('health'), 'readiness': probe_result.get('ready')})
    return result


def doctor(timeout=300):
    """Run the official doctor; returns pass flags and redacted tail output."""
    write_runtime_config()
    binary = venv_bin('openviking-server')
    if not binary.exists():
        raise ValueError('OpenViking runtime is not installed')
    out = subprocess.run([str(binary), 'doctor'], env=service_env(), cwd=str(state_dir()),
                         capture_output=True, text=True, timeout=timeout)
    text = _redact(out.stdout + out.stderr)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = {}
    for line in lines:
        for key in ('Config', 'Authentication', 'Embedding', 'VLM', 'Disk'):
            if line.startswith(key + ':') and key not in summary:
                summary[key] = line.split(':', 1)[1].strip()
    summary['passed'] = bool(out.returncode == 0 and 'All checks passed' in text)
    summary['tail'] = lines[-25:]
    return summary

def drain(timeout=120, interval=1.0):
    """Wait for in-flight OpenViking background work to settle after OpenCode disposal.

    The OpenCode plugin commits sessions and captures turns while it shuts down,
    so the memory service must stay reachable until those commits finish. This
    polls the task API for pending/running/cancelling work and returns the last
    observed counts; a timeout returns the unsettled state instead of raising so
    the caller can decide whether to keep the service alive.
    """
    deadline = time.time() + max(0, timeout)
    last = {'pending': None, 'running': None, 'settled': False}
    while True:
        counts = {'pending': 0, 'running': 0, 'cancelling': 0}
        try:
            for status in tuple(counts):
                result = request('/api/v1/tasks?' + '&'.join(
                    ['status=' + status, 'limit=200']))
                tasks = result if isinstance(result, list) else (result or {}).get('tasks', [])
                counts[status] = len(tasks or [])
        except ValueError:
            counts = None
        last = {'pending': counts['pending'] if counts else None,
                'running': counts['running'] if counts else None,
                'settled': counts is not None and sum(counts.values()) == 0}
        if last['settled'] or time.time() >= deadline:
            return last
        time.sleep(interval)


def request(path, method='GET', data=None, timeout=30):
    """Small authenticated helper for service-local probes (key stays in-process)."""
    key = api_key()
    headers = {'X-API-Key': key} if key else None
    try:
        payload = common.request(_settings()['url'] + path, method, data,
                                 headers=headers, timeout=timeout)
    except common.HttpFailure as error:
        raise ValueError(str(error)) from None
    if not isinstance(payload, dict):
        raise ValueError('OpenViking returned an unexpected response')
    if payload.get('error'):
        raise ValueError('OpenViking request failed')
    return payload.get('result', payload)
