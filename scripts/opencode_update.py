#!/usr/bin/env python3
"""Bridge-owned OpenCode version checks, idle upgrades and rollback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from common import ACTIVE, CONFIG, STATE, config, init, locked, read_json, redact, tasks, write_json
from bootstrap import binary_version, install_managed, latest_version, parse_version

STATUS = STATE / 'opencode-update.json'
DEFAULTS = {'enabled': True, 'check_interval_hours': 6}


def settings(value=None):
    raw = value if isinstance(value, dict) else {}
    enabled = raw.get('enabled', True)
    interval = raw.get('check_interval_hours', 6)
    if not isinstance(enabled, bool): enabled = True
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 168:
        interval = 6
    return {'enabled': enabled, 'check_interval_hours': interval}


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('opencode_updates.enabled must be boolean')
    interval = value.get('check_interval_hours')
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 168:
        raise ValueError('opencode_updates.check_interval_hours must be an integer between 1 and 168')
    return {'enabled': value['enabled'], 'check_interval_hours': interval}


def current_version(c=None):
    c = c or config()
    binary = c.get('opencode_binary')
    try:
        return binary_version(binary) if binary else None
    except Exception:
        return None


def _safe_status(**fields):
    value = {'time': time.time(), **fields}
    write_json(STATUS, value)
    return value


def status(check=False):
    c = config()
    saved = read_json(STATUS, {}) or {}
    result = {
        'enabled': settings(c.get('opencode_updates'))['enabled'],
        'check_interval_hours': settings(c.get('opencode_updates'))['check_interval_hours'],
        'current': current_version(c),
        'latest': saved.get('latest'),
        'state': saved.get('state', 'unknown'),
        'checked_at': saved.get('checked_at'),
        'updated_at': saved.get('updated_at'),
        'message': saved.get('message'),
    }
    return run(check_only=True) if check else result


def busy_reason():
    if any(t.get('status') in ACTIVE | {'queued'} for t in tasks()):
        return 'worker_tasks_active'
    import management
    if management._sessions_busy():
        return 'opencode_sessions_active'
    return None


def run(check_only=False):
    """Check latest stable and, unless check-only, upgrade only while fully idle."""
    init()
    with locked('opencode-update'):
        c = config()
        old_binary = c.get('opencode_binary')
        current = current_version(c)
        try:
            latest = latest_version()
        except Exception as error:
            return _safe_status(state='check_failed', current=current,
                                message=redact(str(error))[:500], checked_at=time.time())
        base = {'current': current, 'latest': latest, 'checked_at': time.time()}
        if current and parse_version(current) >= parse_version(latest):
            return _safe_status(state='up_to_date', **base)
        if check_only:
            return _safe_status(state='update_available', **base)
        reason = busy_reason()
        if reason:
            return _safe_status(state='waiting_for_idle', reason=reason, **base)
        maintenance = STATE / 'maintenance.json'
        if maintenance.exists():
            return _safe_status(state='waiting_for_idle', reason='maintenance_active', **base)
        write_json(maintenance, {'time': time.time(), 'reason': 'opencode_update',
                                 'from': current, 'to': latest})
        try:
            reason = busy_reason()
            if reason:
                return _safe_status(state='waiting_for_idle', reason=reason, **base)
            _safe_status(state='installing', **base)
            new_binary = install_managed(latest)
            reason = busy_reason()
            if reason:
                return _safe_status(state='waiting_for_idle', reason=reason, **base)
            updated = config()
            updated['opencode_binary'] = new_binary
            write_json(CONFIG, updated)
            try:
                import service
                service.reload_runtime()
                actual = current_version()
                if actual != latest:
                    raise RuntimeError('OpenCode restarted with an unexpected version')
            except Exception:
                rollback = config()
                rollback['opencode_binary'] = old_binary
                write_json(CONFIG, rollback)
                try:
                    import service
                    service.reload_runtime()
                except Exception:
                    pass
                raise
            return _safe_status(state='updated', current=latest, latest=latest,
                                checked_at=base['checked_at'], updated_at=time.time())
        except Exception as error:
            return _safe_status(state='rollback_restored', current=current, latest=latest,
                                checked_at=base['checked_at'], message=redact(str(error))[:500])
        finally:
            maintenance.unlink(missing_ok=True)


def due():
    c = config()
    policy = settings(c.get('opencode_updates'))
    if not policy['enabled']:
        return False
    saved = read_json(STATUS, {}) or {}
    last = saved.get('checked_at') or saved.get('time') or 0
    delay = 300 if saved.get('state') == 'waiting_for_idle' else policy['check_interval_hours'] * 3600
    return time.time() - last >= delay


def launch_if_due():
    if not due(): return False
    script = str(Path(__file__).resolve())
    log_path = STATE / 'logs/opencode-update.log'
    with log_path.open('ab') as log:
        subprocess.Popen([sys.executable, script, 'apply'], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True)
    # Prevent another daemon loop from racing before the child writes its check time.
    saved = read_json(STATUS, {}) or {}
    saved.update(state='checking', time=time.time(), checked_at=time.time())
    write_json(STATUS, saved)
    return True


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'status': result = status()
    elif action == 'check': result = run(check_only=True)
    elif action == 'apply': result = run(check_only=False)
    else: raise SystemExit('usage: opencode_update.py [status|check|apply]')
    print(json.dumps(result, ensure_ascii=False, indent=2))
