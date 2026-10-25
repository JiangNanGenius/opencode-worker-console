#!/usr/bin/env python3
"""Bridge-owned OpenCode version checks, idle upgrades and rollback.

The upgrade is split into two phases so running work is never aborted:

* **Stage** — query the official registry at the configured daily interval (or
  on an explicit manual check) and install the verified stable binary into
  bridge-owned storage. Staging is safe while tasks run.
* **Cut over** — only when no task is executing and no native/uncached session
  is busy. When tasks are merely *queued* (waiting for capacity, locks or a
  route) the bridge reserves the existing maintenance barrier, which stops new
  admissions without cancelling anything, then performs the short serialized
  restart between jobs. A crash or an operator pause is recovered safely: a
  stale bridge-owned lease is reclaimed, an operator lease is never touched and
  the barrier is always cleared in ``finally``.

A pending target is reused from local state, so waiting for an idle gap never
re-queries the registry; only a failed network check is retried with backoff.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from common import ACTIVE, CONFIG, STATE, api, config, init, locked, read_json, redact, tasks, write_json
from bootstrap import binary_version, install_managed, latest_version, parse_version

STATUS = STATE / 'opencode-update.json'
DEFAULTS = {'enabled': True, 'check_interval_hours': 24}
# A pending (staged) update re-checks local idleness at this cadence; it never
# re-downloads and never re-queries the registry just because the machine is busy.
LOCAL_RETRY_SECONDS = 120
# Registry failures back off so a transient network problem is not hammered.
FAILED_BACKOFF_SECONDS = 6 * 3600
FAILED_BACKOFF_MAX_SECONDS = 24 * 3600
LAUNCH_GUARD_SECONDS = 60
# A bridge-owned cutover lease is short; a lease older than this with a dead or
# missing owner process is a crash leftover and is reclaimed.
MAINTENANCE_STALE_SECONDS = 45 * 60
CUTOVER_SETTLE_SECONDS = 10


def settings(value=None):
    raw = value if isinstance(value, dict) else {}
    enabled = raw.get('enabled', True)
    interval = raw.get('check_interval_hours', 24)
    if not isinstance(enabled, bool): enabled = True
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 168:
        interval = 24
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


def live_version():
    """Version the running OpenCode server reports, or None when unreachable.

    The configured binary's ``--version`` only proves what is on disk. Health
    verification after a cutover prefers this live value so a restart that
    silently kept the old process cannot look successful.
    """
    try:
        value = api('/global/health', timeout=5)
    except Exception:
        return None
    version = value.get('version') if isinstance(value, dict) else None
    if not isinstance(version, str):
        return None
    try:
        parse_version(version)
    except ValueError:
        return None
    return version


def _safe_status(**fields):
    value = {'time': time.time(), **fields}
    write_json(STATUS, value)
    return value


def _interval_seconds():
    return settings(config().get('opencode_updates'))['check_interval_hours'] * 3600


def status(check=False):
    c = config()
    saved = read_json(STATUS, {}) or {}
    policy = settings(c.get('opencode_updates'))
    result = {
        'enabled': policy['enabled'],
        'check_interval_hours': policy['check_interval_hours'],
        'current': current_version(c),
        'latest': saved.get('latest'),
        'state': saved.get('state', 'unknown'),
        'reason': saved.get('reason'),
        'checked_at': saved.get('checked_at'),
        'next_check_at': saved.get('next_check_at'),
        'updated_at': saved.get('updated_at'),
        'message': saved.get('message'),
        'staged': saved.get('staged') is True,
        'last': saved.get('time'),
    }
    return run(check_only=True) if check else result


def _pool_state():
    """(executing tasks, queued tasks); executing work blocks a cutover entirely."""
    all_tasks = tasks()
    active = [t for t in all_tasks if t.get('status') in ACTIVE]
    queued = [t for t in all_tasks if t.get('status') == 'queued']
    return active, queued


def sessions_busy():
    """True when a native OpenCode session is busy; unknown fails closed."""
    import management
    try:
        return bool(management._sessions_busy())
    except Exception:
        return True


def busy_reason():
    active, queued = _pool_state()
    if active or queued:
        return 'worker_tasks_active'
    if sessions_busy():
        return 'opencode_sessions_active'
    return None


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _recover_stale_maintenance(path, now):
    """True when no live maintenance lease is held; reclaims only our stale one."""
    record = read_json(path, {})
    if not isinstance(record, dict) or not record:
        return True
    if record.get('reason') != 'opencode_update':
        return False  # Operator pause or another subsystem owns the barrier.
    started = record.get('time') if isinstance(record.get('time'), (int, float)) else 0
    if now - started <= MAINTENANCE_STALE_SECONDS:
        return False
    if _pid_alive(record.get('pid')):
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _staged_binary(version):
    """Local verified binary for one exact version, or None; never downloads."""
    if not isinstance(version, str) or not version:
        return None
    try:
        parse_version(version)
    except ValueError:
        return None
    root = STATE / 'runtime' / 'opencode' / version
    for candidate in (root / 'npm/node_modules/.bin/opencode', root / 'opencode'):
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            try:
                if binary_version(candidate) == version:
                    return str(candidate)
            except Exception:
                continue
    return None


def _failure(saved, current, error, now):
    failures = saved.get('failures') if isinstance(saved.get('failures'), int) else 0
    failures = max(0, min(failures + 1, 16))
    backoff = min(FAILED_BACKOFF_MAX_SECONDS, FAILED_BACKOFF_SECONDS * failures)
    return _safe_status(state='check_failed', current=current, latest=saved.get('latest'),
                        checked_at=saved.get('checked_at'),
                        next_check_at=now + backoff, failures=failures,
                        message=redact(str(error))[:500])


def _network_due(saved, now):
    if saved.get('state') == 'check_failed':
        return now >= (saved.get('next_check_at') or 0)
    last = saved.get('checked_at') or 0
    return now - last >= _interval_seconds()


def run(check_only=False, force_check=False):
    """Check the stable channel and, unless check-only, stage and cut over.

    ``force_check`` is the manual path: it always queries the registry. The
    daemon path reuses a verified staged target and only queries when the daily
    interval (or failure backoff) is due.
    """
    init()
    with locked('opencode-update'):
        c = config()
        old_binary = c.get('opencode_binary')
        current = current_version(c)
        now = time.time()
        saved = read_json(STATUS, {}) or {}
        base = {'current': current, 'checked_at': saved.get('checked_at'), 'failures': 0}
        if check_only or force_check:
            try:
                latest = latest_version()
            except Exception as error:
                return _failure(saved, current, error, now)
            base.update(latest=latest, checked_at=now)
            if current and parse_version(current) >= parse_version(latest):
                return _safe_status(state='up_to_date', next_check_at=now + _interval_seconds(), **base)
            if check_only:
                return _safe_status(state='update_available', next_check_at=now + _interval_seconds(), **base)
        else:
            target = saved.get('latest') if isinstance(saved.get('latest'), str) else None
            staged = _staged_binary(target)
            # Only a genuinely newer already-staged target may skip the registry
            # before the daily interval. Once the interval is due the registry is
            # queried even with a pending local target, so an old runtime that
            # stayed busy across days still discovers an even newer release.
            pending_newer = bool(staged and target and
                                 (not current or parse_version(target) > parse_version(current)))
            due_now = _network_due(saved, now)
            if pending_newer and not due_now:
                latest = target
                base.update(latest=target)
            else:
                if not due_now:
                    return _safe_status(state=saved.get('state', 'unknown'),
                                        reason=saved.get('reason') or 'waiting_for_scheduled_check',
                                        latest=target, next_check_at=saved.get('next_check_at'),
                                        **base)
                try:
                    latest = latest_version()
                except Exception as error:
                    return _failure(saved, current, error, now)
                base.update(latest=latest, checked_at=now)
                if current and parse_version(current) >= parse_version(latest):
                    return _safe_status(state='up_to_date', next_check_at=now + _interval_seconds(), **base)
        # Stage the verified binary first: installation is safe while work runs.
        try:
            new_binary = install_managed(latest)
        except Exception as error:
            return _failure(saved, current, error, now)
        base['staged'] = True
        active, queued = _pool_state()
        if active:
            return _safe_status(state='waiting_for_idle', reason='worker_tasks_active',
                                local_retry_at=now + LOCAL_RETRY_SECONDS, **base)
        if sessions_busy():
            return _safe_status(state='waiting_for_idle', reason='opencode_sessions_active',
                                local_retry_at=now + LOCAL_RETRY_SECONDS, **base)
        maintenance = STATE / 'maintenance.json'
        if not _recover_stale_maintenance(maintenance, now):
            return _safe_status(state='waiting_for_idle', reason='maintenance_active',
                                local_retry_at=now + LOCAL_RETRY_SECONDS, **base)
        # Reserve the admission barrier: queued jobs stay queued and are not
        # cancelled, while no new task can be admitted during the short restart.
        write_json(maintenance, {'time': now, 'reason': 'opencode_update', 'pid': os.getpid(),
                                 'from': current, 'to': latest, 'queued_reserved': len(queued)})
        try:
            deadline = time.time() + CUTOVER_SETTLE_SECONDS
            while time.time() < deadline:
                active = [t for t in tasks() if t.get('status') in ACTIVE]
                if not active:
                    break
                time.sleep(1)
            if active or sessions_busy():
                return _safe_status(state='waiting_for_idle',
                                    reason='worker_tasks_active' if active else 'opencode_sessions_active',
                                    local_retry_at=time.time() + LOCAL_RETRY_SECONDS, **base)
            _safe_status(state='installing', **base)
            # Re-read under the shared configuration lock so profiles and other
            # settings written while this upgrade was staged are preserved and
            # the write cannot race a console settings save; only the binary
            # field changes.
            with locked():
                updated = config()
                updated['opencode_binary'] = new_binary
                write_json(CONFIG, updated)
            try:
                import service
                service.reload_runtime(allow_queued=True, maintenance_held=True)
                actual = live_version() or current_version()
                if actual != latest:
                    raise RuntimeError('OpenCode restarted with an unexpected version')
            except Exception:
                with locked():
                    rollback = config()
                    rollback['opencode_binary'] = old_binary
                    write_json(CONFIG, rollback)
                try:
                    import service
                    service.reload_runtime(allow_queued=True, maintenance_held=True)
                except Exception:
                    pass
                raise
            return _safe_status(state='updated', current=latest, latest=latest,
                                checked_at=base.get('checked_at'), updated_at=time.time(),
                                staged=True, failures=0, next_check_at=None)
        except Exception as error:
            return _safe_status(state='rollback_restored', current=current, latest=latest,
                                checked_at=base['checked_at'], failures=0,
                                message=redact(str(error))[:500])
        finally:
            maintenance.unlink(missing_ok=True)


def due():
    c = config()
    policy = settings(c.get('opencode_updates'))
    if not policy['enabled']:
        return False
    saved = read_json(STATUS, {}) or {}
    now = time.time()
    state = saved.get('state')
    if state == 'checking':
        # A child was launched moments ago; do not launch a second one.
        return now - (saved.get('time') or 0) >= LAUNCH_GUARD_SECONDS
    if state == 'waiting_for_idle':
        # Reuse the staged target and retry only the local idle check once the
        # scheduled local retry time arrives.
        retry_at = saved.get('local_retry_at')
        if isinstance(retry_at, (int, float)) and not isinstance(retry_at, bool):
            return now >= retry_at
        return now - (saved.get('time') or 0) >= LOCAL_RETRY_SECONDS
    if state == 'check_failed':
        return now >= (saved.get('next_check_at') or 0)
    last = saved.get('checked_at') or saved.get('time') or 0
    return now - last >= policy['check_interval_hours'] * 3600


def launch_if_due():
    if not due(): return False
    script = str(Path(__file__).resolve())
    log_path = STATE / 'logs/opencode-update.log'
    with log_path.open('ab') as log:
        subprocess.Popen([sys.executable, script, 'apply'], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, close_fds=True)
    # Guard against a second launch before the child writes its own state, but do
    # not advance checked_at: a crashed child must be retried on the next tick.
    saved = read_json(STATUS, {}) or {}
    saved.update(state='checking', time=time.time())
    write_json(STATUS, saved)
    return True


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'status': result = status()
    elif action == 'check': result = run(check_only=True, force_check=True)
    elif action == 'apply': result = run(check_only=False)
    else: raise SystemExit('usage: opencode_update.py [status|check|apply]')
    print(json.dumps(result, ensure_ascii=False, indent=2))
