"""Structured, allowlisted host controls for the authenticated console.

Every control here is a fixed action with validated parameters — there is no
terminal and no file browser. Writes are confined to narrow allowlists:

* fans: the local fan daemon's documented IPC, and only ``setControlMode``,
  ``setTarget`` and ``applyProfile`` with the built-in profile set. The daemon
  safety floor is never touched.
* power modes: only the modes ``pmset -g cap`` reports as supported, applied
  through the verified local privileged helper and confirmed by readback.
* keep-awake: one console-managed ``caffeinate`` process (off / timed /
  while-tasks). Existing sleep settings are never rewritten.
* processes: own-user, non-protected processes only, ended with TERM and then
  an explicit KILL; PID plus start time is validated so a recycled PID can
  never be signalled by mistake.
* services: the three managed services recorded by the console only.
* sleep/restart/shutdown: explicit confirmation, the exact list of affected
  running jobs, a fresh password check (never persisted) and a durable
  operation record — accepted is not completed, and status is reconciled
  against observed boot evidence.

Disruptive commands run through the narrow root helper
(scripts/host_helper.py); the console never runs as root and privileged
actions stay unavailable until the helper is installed and verified. Only
metadata reaches the audit log; credentials never enter scripts, logs or
configuration.
"""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time

from common import ACTIVE, locked, read_json, write_json
import host_telemetry

FAN_PROFILES = ('Silent', 'Balanced', 'Cool', 'Max')
FAN_WRITE_COMMANDS = {'setControlMode', 'setTarget', 'applyProfile'}
POWER_MODES = ('lowpowermode', 'highpowermode')
POWER_OPERATIONS = ('sleep', 'restart', 'shutdown')
MANAGED_SERVICES = ('server', 'pool', 'console')
CAFFEINATE_MODES = ('off', 'timed', 'while_tasks')
CAFFEINATE_MINUTES_MIN = 1
CAFFEINATE_MINUTES_MAX = 720
CAFFEINATE_IDLE_GRACE_SECONDS = 10
POWER_OP_DEDUPE_SECONDS = 60
HELPER_PATH = '/opt/opencode-host/bin/opencode-host-helper'
HELPER_TIMEOUT_SECONDS = 20
OPERATIONS_LIMIT = 50
AUDIT_LINE_LIMIT = 2000
AUDIT_MAX_BYTES = 1024 * 1024
_PID_MAX = 4 * 1024 * 1024

sent_commands = []  # bounded local record of IPC writes, for audit and tests


def _state_dir():
    return host_telemetry.STATE / 'host'


def _commands_record(command):
    sent_commands.append(command)
    del sent_commands[:-100]


def _run(args, timeout=5, env=None):
    full_env = dict(os.environ)
    full_env['LC_ALL'] = 'C'
    if env:
        full_env.update(env)
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              check=False, env=full_env)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(args, 1, '', str(error))


# --- audit (metadata only, bounded) ------------------------------------------

_AUDIT_EXCLUDED_KEYS = {'password', 'passwd', 'secret', 'token', 'credential', 'authorization'}


def audit(event, **meta):
    record = {'ts': round(time.time(), 3), 'event': str(event)[:80]}
    for key, value in meta.items():
        if str(key).lower() in _AUDIT_EXCLUDED_KEYS:
            continue  # credentials never enter logs, even when a caller errs
        record[str(key)[:40]] = host_telemetry._bounded(value, 200)
    path = _state_dir() / 'audit.log'
    try:
        _state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        if path.stat().st_size > AUDIT_MAX_BYTES:
            lines = path.read_text(encoding='utf-8').splitlines()[-AUDIT_LINE_LIMIT // 2:]
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except OSError:
        pass
    return record


# --- fan daemon controls ------------------------------------------------------

def _fan_write(payload):
    command = payload.get('cmd')
    if command not in FAN_WRITE_COMMANDS:
        raise ValueError('Fan command is not allowed')
    _commands_record(command)
    return host_telemetry.daemon_request(payload)


def _fan_profiles_from_daemon():
    try:
        response = host_telemetry.daemon_request({'cmd': 'listProfiles'})
    except (OSError, RuntimeError, ValueError):
        return []
    profiles = response.get('profiles') if isinstance(response, dict) else None
    names = []
    for profile in profiles or []:
        name = profile.get('name') if isinstance(profile, dict) else None
        if isinstance(name, str):
            names.append(name)
    return names


def fan_status():
    try:
        response = host_telemetry.daemon_request({'cmd': 'status'})
        status = response.get('status') if isinstance(response, dict) else None
        if not status:
            return {'available': False}
    except (OSError, RuntimeError, ValueError):
        return {'available': False}
    fans = []
    rpm_min = rpm_max = None
    for fan in status.get('fans', []):
        info = fan.get('info', {}) if isinstance(fan, dict) else {}
        if isinstance(info.get('minRPM'), (int, float)):
            rpm_min = info['minRPM'] if rpm_min is None else max(rpm_min, info['minRPM'])
        if isinstance(info.get('maxRPM'), (int, float)):
            rpm_max = info['maxRPM'] if rpm_max is None else min(rpm_max, info['maxRPM'])
        fans.append({'name': host_telemetry._bounded(info.get('name') or ('Fan ' + str(info.get('index', '?'))), 32),
                     'rpm': info.get('actualRPM'), 'target': fan.get('commandedRPM'),
                     'min_rpm': info.get('minRPM'), 'max_rpm': info.get('maxRPM'),
                     'safety_engaged': bool(fan.get('safetyEngaged'))})
    return {'available': True, 'mode': status.get('controlMode'),
            'profile': status.get('activeProfileName'), 'fans': fans,
            'safety_engaged': bool(status.get('safetyEngaged')),
            'rpm_min': rpm_min, 'rpm_max': rpm_max,
            'profiles': _fan_profiles_from_daemon(),
            'thermal_pressure': status.get('thermalPressure')}


def set_fan_auto():
    _fan_write({'cmd': 'setControlMode', 'controlMode': 'appleAuto'})
    state = fan_status()
    audit('fan.auto', result='ok' if state.get('available') else 'unavailable')
    return state


def set_fan_profile(name):
    if not isinstance(name, str) or name not in FAN_PROFILES:
        raise ValueError('Fan profile must be one of: ' + ', '.join(FAN_PROFILES))
    available = _fan_profiles_from_daemon()
    if available and name not in available:
        raise ValueError('Fan profile is not provided by the daemon')
    _fan_write({'cmd': 'applyProfile', 'profileName': name})
    state = fan_status()
    audit('fan.profile', profile=name)
    return state


def set_fan_rpm(rpm):
    if isinstance(rpm, bool) or not isinstance(rpm, (int, float)) or not host_telemetry._finite(rpm):
        raise ValueError('RPM must be a number')
    state = fan_status()
    if not state.get('available'):
        raise RuntimeError('Fan daemon is unavailable')
    low, high = state.get('rpm_min'), state.get('rpm_max')
    if low is None or high is None:
        raise RuntimeError('Fan RPM bounds are unavailable')
    rpm = int(rpm)
    if not low <= rpm <= high:
        raise ValueError('RPM must be between {} and {}'.format(int(low), int(high)))
    current = state.get('mode')
    targets = [fan.get('target') for fan in state.get('fans', []) if fan.get('target') is not None]
    if current == 'fixed' and targets and all(abs(target - rpm) < 1 for target in targets):
        return state  # idempotent: identical fixed RPM is not re-sent
    _fan_write({'cmd': 'setControlMode', 'controlMode': 'fixed'})
    _fan_write({'cmd': 'setTarget', 'rpm': rpm})
    state = fan_status()
    audit('fan.rpm', rpm=rpm)
    return state


# --- privileged helper --------------------------------------------------------

_helper_cache = {'at': 0.0, 'status': None}


def helper_status(force=False):
    if not force and _helper_cache['status'] is not None and time.time() - _helper_cache['at'] < 60:
        return dict(_helper_cache['status'])
    path = Path(HELPER_PATH)
    installed = path.is_file() and os.access(path, os.X_OK)
    verified = False
    if installed:
        try:
            result = subprocess.run(['sudo', '-n', HELPER_PATH, '--verify'], capture_output=True,
                                    timeout=5, check=False)
            verified = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            verified = False
    status = {'installed': installed, 'verified': verified, 'path': HELPER_PATH}
    _helper_cache.update(at=time.time(), status=status)
    return dict(status)


def _helper_run(args):
    status = helper_status()
    if not status['verified']:
        raise RuntimeError('Privileged helper is not installed or verified; ' +
                           'see references/host-controls.md for installation')
    try:
        result = subprocess.run(['sudo', '-n', HELPER_PATH, *args], capture_output=True,
                                text=True, timeout=HELPER_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Privileged helper could not run: ' + str(error)[:200]) from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()[:200]
        raise RuntimeError('Privileged helper failed: ' + detail)
    try:
        return json.loads(result.stdout or '{}')
    except ValueError:
        return {'ok': True}


# --- power modes ---------------------------------------------------------------

def power_capabilities():
    supported, current = [], {}
    if sys.platform != 'darwin' or not shutil.which('pmset'):
        return {'supported': [], 'current': {}}
    cap_out = _run(['pmset', '-g', 'cap'], timeout=5).stdout
    for mode in POWER_MODES:
        if re.search(r'^\s*' + mode + r'\s*$', cap_out, re.M):
            supported.append(mode)
    custom = _run(['pmset', '-g', 'custom'], timeout=5).stdout
    for mode in supported:
        match = re.findall(r'^\s*' + mode + r'\s+(\d)\s*$', custom, re.M)
        current[mode] = int(match[0]) if match else None
    return {'supported': supported, 'current': current}


def set_power_mode(mode, enabled):
    if mode not in POWER_MODES:
        raise ValueError('Unsupported power mode')
    if not isinstance(enabled, bool):
        raise ValueError('enabled must be a boolean')
    capabilities = power_capabilities()
    if mode not in capabilities['supported']:
        raise ValueError('Power mode is not supported on this host')
    _helper_run(['power-mode', mode, '1' if enabled else '0'])
    after = power_capabilities()
    audit('power-mode.set', mode=mode, enabled=enabled)
    return after


# --- keep-awake (console-managed caffeinate) ------------------------------------

def _caffeinate_path():
    return _state_dir() / 'caffeinate.json'


def _caffeinate_read():
    return read_json(_caffeinate_path(), {}) or {}


def _caffeinate_write(state):
    write_json(_caffeinate_path(), state)


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
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


def _active_task_count(now=None):
    count = sum(1 for t in read_json_tasks() if t.get('status') in ACTIVE)
    collector = host_telemetry.current_collector()
    if collector is not None:
        try:
            count += collector.snapshot(now).get('tasks', {}).get('codex', {}).get('active', 0)
        except Exception:
            pass
    return count


def affected_jobs(now=None):
    """All work a disruptive power operation would interrupt: active or queued
    bridge tasks plus active Codex conversations."""
    jobs = sorted(t.get('id') for t in read_json_tasks()
                  if t.get('status') in ACTIVE | {'queued'} and t.get('id'))
    codex_active = []
    collector = host_telemetry.current_collector()
    if collector is not None:
        try:
            snapshot = collector.snapshot(now)
            codex = snapshot.get('tasks', {}).get('codex', {})
            if snapshot.get('sampled_at') and time.time() - snapshot['sampled_at'] < 120:
                codex_active = [item['id'] for item in codex.get('items', [])
                                if item.get('status') in ('running', 'unknown', 'attention') and item.get('id')]
        except Exception:
            pass
    else:
        try:
            collected = host_telemetry.CodexTasks().collect(now)
            codex_active = [item['id'] for item in collected.get('items', [])
                            if item.get('status') in ('running', 'unknown', 'attention') and item.get('id')]
        except Exception:
            codex_active = []
    return sorted(jobs + codex_active)


def read_json_tasks():
    root = host_telemetry.STATE / 'tasks'
    if not root.is_dir():
        return []
    tasks = []
    for path in root.glob('*.json'):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            tasks.append(value)
    return tasks


def caffeinate_status():
    available = shutil.which('caffeinate') is not None
    state = _caffeinate_read()
    pid = state.get('pid')
    active = bool(state.get('mode') and state.get('mode') != 'off' and _caffeinate_matches(state))
    if not active:
        state = {'mode': 'off'}
    return {'available': available, 'active': active, 'mode': state.get('mode'),
            'started_at': state.get('started_at'), 'until': state.get('until'),
            'pid': pid if active else None}


def _caffeinate_matches(state):
    from service import identity
    pid=state.get('pid')
    return isinstance(pid,int) and bool(state.get('identity')) and identity(pid)==state['identity'] and _pid_alive(pid)


def _caffeinate_stop(state, reason):
    pid = state.get('pid')
    if _caffeinate_matches(state):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _caffeinate_write({'mode': 'off'})
    audit('caffeinate.stop', reason=reason)


def set_caffeinate(mode, minutes=None):
    if mode not in CAFFEINATE_MODES:
        raise ValueError('Caffeinate mode must be one of: ' + ', '.join(CAFFEINATE_MODES))
    if not shutil.which('caffeinate'):
        raise RuntimeError('caffeinate is unavailable on this host')
    now = time.time()
    current = _caffeinate_read()
    if current.get('mode') and current.get('mode') != 'off':
        _caffeinate_stop(current, 'replaced')
    if mode == 'off':
        audit('caffeinate.set', mode='off')
        return caffeinate_status()
    state = {'mode': mode, 'started_at': now}
    if mode == 'timed':
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or \
                not CAFFEINATE_MINUTES_MIN <= minutes <= CAFFEINATE_MINUTES_MAX:
            raise ValueError('Minutes must be between {} and {}'.format(CAFFEINATE_MINUTES_MIN,
                                                                        CAFFEINATE_MINUTES_MAX))
        state['until'] = now + minutes * 60
    process = subprocess.Popen(['caffeinate', '-i'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    state['pid'] = process.pid
    from service import identity
    state['identity'] = identity(process.pid)
    _caffeinate_write(state)
    _ensure_supervisor()
    audit('caffeinate.set', mode=mode, minutes=minutes if mode == 'timed' else '')
    return caffeinate_status()


_supervisor = {'thread': None, 'stop': threading.Event()}


def _ensure_supervisor():
    if _supervisor['thread'] is not None and _supervisor['thread'].is_alive():
        return
    _supervisor['stop'].clear()
    thread = threading.Thread(target=_supervisor_loop, daemon=True, name='caffeinate-supervisor')
    thread.start()
    _supervisor['thread'] = thread


def _supervisor_loop():
    while not _supervisor['stop'].wait(5):
        try:
            _caffeinate_step()
        except Exception:
            pass


def _caffeinate_step(now=None):
    now = now or time.time()
    state = _caffeinate_read()
    mode = state.get('mode')
    if not mode or mode == 'off':
        return
    if not _caffeinate_matches(state):
        _caffeinate_write({'mode': 'off'})
        return
    if mode == 'timed':
        if state.get('until') and now >= state['until']:
            _caffeinate_stop(state, 'expired')
        return
    if mode == 'while_tasks':
        if _active_task_count(now) > 0:
            state.pop('idle_since', None)
            _caffeinate_write(state)
        else:
            idle_since = state.get('idle_since') or now
            state['idle_since'] = idle_since
            _caffeinate_write(state)
            if now - idle_since >= CAFFEINATE_IDLE_GRACE_SECONDS:
                _caffeinate_stop(state, 'idle')
        return


# --- processes -----------------------------------------------------------------

def _protected_pids():
    pids = {1, os.getpid()}
    try:
        records = read_json(host_telemetry.STATE / 'services.json', {}) or {}
        for entry in records.values():
            if isinstance(entry, dict) and isinstance(entry.get('pid'), int):
                pids.add(entry['pid'])
    except OSError:
        pass
    pid = os.getpid()
    for _ in range(32):  # walk ancestors; never signal our own lineage
        result = _run(['ps', '-o', 'ppid=', '-p', str(pid)], timeout=3)
        try:
            parent = int(result.stdout.strip())
        except ValueError:
            break
        if parent <= 1 or parent in pids:
            break
        pids.add(parent)
        pid = parent
    return pids


def _parse_ps_line(line, with_uid):
    parts = line.split(None, 6)
    if len(parts) < 7:
        return None
    try:
        pid = int(parts[0])
        started = int(time.mktime(time.strptime(' '.join(parts[1:6]), '%a %b %d %H:%M:%S %Y')))
    except (ValueError, OverflowError):
        return None
    rest = parts[6].split(None, 4)
    if len(rest) < 5:
        return None
    try:
        uid, cpu, mem, rss = int(rest[0]), float(rest[1]), float(rest[2]), int(rest[3]) * 1024
    except ValueError:
        return None
    name = host_telemetry._bounded(rest[4], 64)
    row = {'pid': pid, 'started_at': started, 'cpu_percent': round(cpu, 1),
           'mem_percent': round(mem, 1), 'rss_bytes': rss, 'name': name}
    if with_uid:
        row['uid'] = uid
    return row


def top_processes(limit=10, sort='cpu'):
    if sort not in ('cpu', 'mem'):
        raise ValueError('Unknown process sort')
    output = _run(['ps', '-A', '-o', 'pid=,lstart=,uid=,pcpu=,pmem=,rss=,comm='], timeout=5).stdout
    own = os.getuid()
    protected = _protected_pids()
    rows = []
    for line in output.splitlines():
        row = _parse_ps_line(line, with_uid=True)
        if not row or row['uid'] != own or row['pid'] in protected:
            continue
        row.pop('uid')
        rows.append(row)
    rows.sort(key=lambda r: r['cpu_percent' if sort == 'cpu' else 'mem_percent'], reverse=True)
    return rows[:max(1, min(int(limit or 10), 25))]


def _process_info(pid):
    result = _run(['ps', '-p', str(pid), '-o', 'pid=,lstart=,uid=,pcpu=,pmem=,rss=,comm='], timeout=3)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _parse_ps_line(result.stdout.strip(), with_uid=True)


def terminate_process(pid, started_at, signal_name):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise ValueError('Invalid PID') from None
    if not 1 < pid <= _PID_MAX:
        raise ValueError('Invalid PID')
    if signal_name not in ('term', 'kill'):
        raise ValueError('Signal must be term or kill')
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)) or \
            not host_telemetry._finite(started_at):
        raise ValueError('Process start time is required')
    info = _process_info(pid)
    if info is None or info.get('pid') != pid:
        raise ValueError('Process is not running')
    if info.get('uid') != os.getuid():
        raise ValueError('Only own-user processes can be ended')
    if abs(info['started_at'] - float(started_at)) > 2:
        raise ValueError('Process identity mismatch; it may have been restarted')
    if pid in _protected_pids():
        raise ValueError('This process is protected')
    sig = signal.SIGTERM if signal_name == 'term' else signal.SIGKILL
    os.kill(pid, sig)
    audit('process.' + signal_name, pid=pid, name=info.get('name'))
    return {'terminated': pid, 'signal': signal_name, 'name': info.get('name')}


# --- managed services -----------------------------------------------------------

def services_status():
    records = read_json(host_telemetry.STATE / 'services.json', {}) or {}
    out = []
    for name in MANAGED_SERVICES:
        entry = records.get(name) or {}
        pid = entry.get('pid')
        alive = False
        if isinstance(pid, int):
            identity = _run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], timeout=3)
            alive = identity.returncode == 0 and \
                identity.stdout.strip() == (entry.get('identity') or '').strip() and bool(identity.stdout.strip())
        out.append({'name': name, 'alive': alive, 'pid': pid if alive else None})
    return out


def restart_service(name, username='console'):
    if name not in MANAGED_SERVICES:
        raise ValueError('Unknown managed service')
    if name in ('server', 'pool') and any(t.get('status') in ACTIVE | {'queued'} for t in read_json_tasks()):
        raise ValueError('Tasks are queued or running; restart when idle')
    # A separate process survives the console it replaces. Use the existing
    # identity-checked service manager, never a bare PID from a stale record.
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--restart-service', name],
                     cwd=str(host_telemetry.STATE), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    audit('service.restart.accepted', service=name, user=username)
    return {'service': name, 'status': 'accepted'}


def _restart_service_worker(name):
    import service
    from common import locked
    time.sleep(1)
    try:
        with locked('service'):
            if name in ('server', 'pool') and any(t.get('status') in ACTIVE | {'queued'} for t in read_json_tasks()):
                raise RuntimeError('Work started before service restart')
            entry = read_json(host_telemetry.STATE / 'services.json', {}).get(name)
            if service.alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                deadline = time.monotonic() + 15
                while service.alive(entry) and time.monotonic() < deadline:
                    time.sleep(.1)
                if service.alive(entry):
                    raise RuntimeError('Service did not stop')
        service.start()
        audit('service.restart.completed', service=name)
    except Exception as error:
        audit('service.restart.failed', service=name, error=type(error).__name__)


# --- power operations (sleep / restart / shutdown) ------------------------------

def _operations_path():
    return _state_dir() / 'operations.json'


def read_operations():
    values = read_json(_operations_path(), []) or []
    return values[:OPERATIONS_LIMIT]


def _write_operations(values):
    write_json(_operations_path(), values[:OPERATIONS_LIMIT])


def _current_uptime_seconds():
    try:
        return host_telemetry._uptime_seconds()
    except Exception:
        return None


def request_power_operation(op, confirm, jobs, password, username, peer=None, now=None):
    """Explicit-confirm power operation with throttled reauthentication.

    The password check uses the console account verifier behind the standard
    per-peer login throttle, and the dedupe/persist cycle is serialized with a
    file lock so concurrent HTTP requests cannot both pass the duplicate
    check or interleave operation records.
    """
    if op not in POWER_OPERATIONS:
        raise ValueError('Unknown power operation')
    if confirm is not True:
        raise ValueError('Power operation requires explicit confirmation')
    now = now or time.time()
    import console_auth
    throttle_peer = str(peer or 'console')[:120]
    retry = console_auth.admit_login_attempt(throttle_peer, host_telemetry.STATE)
    if retry:
        raise ValueError('Too many attempts; retry in {} seconds'.format(retry))
    if not console_auth.verify_credentials(username, password, host_telemetry.STATE):
        raise ValueError('Recent authentication failed')
    try:
        console_auth.record_login_success(throttle_peer, host_telemetry.STATE)
    except Exception:
        pass
    requested = sorted(str(j) for j in (jobs or []))
    with locked('host-operations'):
        active = affected_jobs(now)
        if requested != active:
            raise ValueError('Affected job list does not match the running jobs')
        operations = read_operations()
        recent = [o for o in operations if o.get('op') == op and o.get('status') in ('accepted', 'dispatched')
                  and now - o.get('requested_at', 0) < POWER_OP_DEDUPE_SECONDS]
        if recent:
            raise ValueError('This power operation was already requested moments ago')
        record = {'id': 'op-' + secrets.token_hex(4), 'op': op, 'status': 'accepted',
                  'user': str(username)[:64], 'requested_at': now, 'affected_jobs': requested,
                  'uptime_seconds': _current_uptime_seconds()}
        operations.insert(0, record)
        _write_operations(operations)
    audit('power.request', op=op, jobs=len(requested), user=username)
    try:
        _helper_run(['power', op])
        record['status'] = 'dispatched'
        record['dispatched_at'] = time.time()
    except RuntimeError as error:
        record['status'] = 'failed'
        record['error'] = str(error)[:200]
    with locked('host-operations'):
        operations = read_operations()
        for index, existing in enumerate(operations):
            if existing.get('id') == record['id']:
                operations[index] = record
                break
        _write_operations(operations)
    return record


def reconcile_operations(now=None):
    """Observed-state reconciliation: a reboot after dispatch completes the op."""
    now = now or time.time()
    uptime_now = _current_uptime_seconds()
    changed = False
    operations = read_operations()
    for record in operations:
        if record.get('status') != 'dispatched' or record.get('op') not in ('restart', 'shutdown'):
            continue
        dispatched_at = record.get('dispatched_at') or record.get('requested_at') or 0
        stored = record.get('uptime_seconds')
        if uptime_now is None or stored is None:
            continue
        boot_happened = uptime_now < stored - 5 or (now - dispatched_at > 10 and uptime_now < (now - dispatched_at) - 5)
        if boot_happened:
            record['status'] = 'completed'
            record['completed_at'] = now
            changed = True
    if changed:
        _write_operations(operations)
    return operations


# --- capabilities ---------------------------------------------------------------

def capabilities():
    fan = fan_status()
    power = power_capabilities()
    helper = helper_status()
    return {'fans': {'available': fan.get('available', False), 'writable': fan.get('available', False),
                     'profiles': list(FAN_PROFILES), 'safety_preserved': True},
            'power_modes': {'supported': power['supported'], 'current': power['current'],
                            'writable': helper['verified']},
            'caffeinate': {'available': shutil.which('caffeinate') is not None,
                           'writable': shutil.which('caffeinate') is not None},
            'processes': {'available': True, 'can_terminate': True},
            'services': list(MANAGED_SERVICES),
            'power': {'operations': list(POWER_OPERATIONS), 'available': helper['verified']},
            'helper': helper}


# --- action dispatch --------------------------------------------------------------

def dispatch(action, params, username, peer=None):
    params = params or {}
    if action == 'fan.auto':
        return set_fan_auto()
    if action == 'fan.profile':
        return set_fan_profile(params.get('name'))
    if action == 'fan.rpm':
        return set_fan_rpm(params.get('rpm'))
    if action == 'power-mode.set':
        return set_power_mode(params.get('mode'), params.get('enabled'))
    if action == 'caffeinate.set':
        return set_caffeinate(params.get('mode'), params.get('minutes'))
    if action == 'process.terminate':
        return terminate_process(params.get('pid'), params.get('started_at'), params.get('signal'))
    if action == 'service.restart':
        return restart_service(params.get('name'), username)
    if action == 'power.request':
        return request_power_operation(params.get('op'), params.get('confirm'),
                                       params.get('affected_jobs'), params.get('password'),
                                       username, peer=peer)
    raise ValueError('Unknown host action')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--restart-service' and sys.argv[2] in MANAGED_SERVICES:
        _restart_service_worker(sys.argv[2])
    else:
        raise SystemExit('Unsupported operation')
