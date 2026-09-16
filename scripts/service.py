"""Start services from the calling app's authorized macOS context, without launchd TCC drift."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import ACTIVE, STATE, api, config, init, locked, read_json, tasks, update, write_json


def identity(pid):
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def alive(entry):
    return bool(entry and entry.get('identity') and identity(entry['pid']) == entry['identity'])


def start():
    init()
    with locked('service'):
        from bootstrap import ensure_opencode
        from common import CONFIG
        c = config()
        binary = ensure_opencode(c.get('opencode_binary'))
        if binary != c.get('opencode_binary'):
            c['opencode_binary'] = binary
            write_json(CONFIG, c)
        records = read_json(STATE / 'services.json', {})
        root = Path(__file__).resolve().parent
        for name, script, args in [('server', 'server.py', []), ('pool', 'delegate.py', ['daemon']),
                                   ('console', 'console.py', [])]:
            if name == 'console' and 'console_url' not in config():
                continue
            if alive(records.get(name)):
                continue
            logpath = STATE / 'logs' / (name + '.log')
            if logpath.exists() and logpath.stat().st_size > 5 * 1024 * 1024:
                logpath.replace(logpath.with_suffix('.previous.log'))
            with logpath.open('ab') as log:
                proc = subprocess.Popen([sys.executable, str(root / script), *args],
                                        cwd=str(STATE), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True, close_fds=True)
            records[name] = {'pid': proc.pid, 'identity': identity(proc.pid)}
            write_json(STATE / 'services.json', records)
            if name == 'server':
                for _ in range(40):
                    try:
                        if api('/global/health', timeout=1).get('healthy'):
                            break
                    except Exception:
                        time.sleep(0.25)
                else:
                    raise RuntimeError('OpenCode did not become healthy; inspect private service logs')
                import management
                management.sync_worker_permissions()
        return {'services': records, 'startup': 'on demand from calling app; durable task recovery'}


def stop():
    # Abort and confirm idle before stopping a process that owns writable scopes.
    with locked('service'):
        for t in tasks():
            if t['status'] in ACTIVE and t.get('session_id'):
                from worker import stop as stop_worker
                if not stop_worker(t):
                    raise RuntimeError('Worker abort not confirmed; service and ownership retained')
                update(t['id'], cancel_requested=True)
        records = read_json(STATE / 'services.json', {})
        for name in ('console', 'pool', 'server'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(60):
                    if not alive(entry):
                        break
                    time.sleep(0.25)
                if alive(entry):
                    raise RuntimeError(name + ' is still stopping; retry service status later')
        return {'services': 'stopped', 'artifacts': 'retained'}


def reload_runtime():
    """Restart idle execution processes while keeping the browser gateway alive."""
    with locked('service'):
        if any(t['status'] in ACTIVE | {'queued'} for t in tasks()):
            raise ValueError('Tasks are queued or running; apply settings when idle')
        import management
        if management._sessions_busy():
            raise ValueError('OpenCode sessions are running; apply settings when idle')
        records = read_json(STATE / 'services.json', {})
        for name in ('pool', 'server'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(60):
                    if not alive(entry):
                        break
                    time.sleep(0.25)
                if alive(entry):
                    raise RuntimeError(name + ' did not stop')
    result = start()
    c = config()
    c['restart_required'] = False
    from common import CONFIG
    write_json(CONFIG, c)
    return result


def stop_consumers():
    """Pause observers/UI for a compatible update; leave OpenCode execution intact."""
    with locked('service'):
        records = read_json(STATE / 'services.json', {})
        for name in ('pool', 'console'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(150):
                    if not alive(entry): break
                    time.sleep(0.1)
                if alive(entry): raise RuntimeError(name + ' did not stop for live update')
    return {'execution_preserved': True}
