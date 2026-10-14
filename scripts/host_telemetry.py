"""Shared host telemetry collector for the authenticated console (stdlib only).

One collector, owned by the console process, serves the web Host Overview and
publishes the physical information-board payload. Acquisition cadence is fixed:
host metrics every 2 seconds, task activity every 3 seconds and provider quotas
on an independent slower cadence. Raw samples are retained for one hour and
one-minute aggregates for seven days in a bounded SQLite store. GET handlers
never acquire: they read this module's in-memory cache only, so a slow sensor,
a disconnected board consumer or an unreachable daemon can never delay an HTTP
response.

Sensor strategy prefers structured sources over fragile CLI scraping: fan
speeds, die temperatures and thermal pressure come from the local fan daemon's
JSON Unix socket; everything else uses short-bounded, read-only system
commands. Missing or unsupported sensors are reported as null, never as zero.

Disk discovery and task/quota normalisation are adapted from the MacStatusBoard
project (status_agent.py / dashboard_sources.py) with attribution; see
references/host-controls.md. Quotas for worker providers are read from the
delegate daemon's cache (quota.json) so this collector never issues provider
network requests or influences routing economics. The Codex quota adapter runs
only on the background quota thread, mirroring the board's own cadence.
"""
import contextlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from common import STATE, read_json

# --- cadence and retention -------------------------------------------------
HOST_INTERVAL_SECONDS = 2.0
TASK_INTERVAL_SECONDS = 3.0
QUOTA_INTERVAL_SECONDS = 300.0
RAW_TTL_SECONDS = 3600
MINUTES_TTL_SECONDS = 7 * 86400
RAW_CAP = 1900                     # ~2s samples for one hour
MINUTES_CAP = 7 * 24 * 60 + 10     # one aggregate per minute for seven days
STALE_AFTER_SECONDS = HOST_INTERVAL_SECONDS * 4
PRUNE_INTERVAL_SECONDS = 300

# --- board integration -----------------------------------------------------
BOARD_FILE = 'host-board.json'     # STATE/host-board.json, owner-only, atomic
BOARD_SCHEMA_VERSION = 2

# --- local adaptation sources (read-only reference project) -----------------
FAN_SOCKET_PATH = '/var/run/fancontrold.sock'
FAN_IPC_TIMEOUT = 1.5
CODEXBAR_CLI = '/Applications/CodexBar.app/Contents/Helpers/CodexBarCLI'

MAX_TASKS = 12
TASK_PRIORITY = {'attention': 0, 'running': 1, 'unknown': 2, 'completed': 3}
WORKER_PROVIDERS = ('kimi-for-coding', 'volcengine-agent-plan', 'deepseek')
PROVIDER_LABELS = {'kimi-for-coding': 'Kimi', 'volcengine-agent-plan': '火山方舟',
                   'deepseek': 'DeepSeek', 'codex': 'Codex'}
WINDOW_LABELS = {300: '5小时', 1440: '每日', 10080: '每周', 43200: '每月'}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounded(value, maximum):
    return ''.join(c for c in str(value or '') if c >= ' ' and c != '\x7f')[:maximum]


def epoch(value):
    """Parse epoch or ISO-8601 timestamps; 0 when unknown."""
    if _finite(value):
        return float(value)
    if isinstance(value, str):
        import datetime
        try:
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return 0


def _run(args, timeout=2):
    """Run a fixed read-only command with a bounded wait; '' on any failure."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


# --- fan daemon structured IPC ----------------------------------------------

def daemon_request(payload, timeout=FAN_IPC_TIMEOUT):
    """One line-delimited JSON request/response round trip with the daemon.

    Read-only and write commands share this client; callers decide which
    commands are allowed. Raises on transport or decoding failure.
    """
    raw = (json.dumps(payload) + '\n').encode()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(FAN_SOCKET_PATH)
        sock.sendall(raw)
        data = b''
        while b'\n' not in data and len(data) < 1 << 20:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    line = data.split(b'\n', 1)[0]
    if not line:
        raise RuntimeError('fan daemon returned no response')
    return json.loads(line.decode())


# --- metric acquisition (each failure isolates to a null field) --------------

def _cpu_percent():
    values = []
    for line in _run(['ps', '-A', '-o', '%cpu=']).splitlines():
        try:
            values.append(float(line.strip().replace(',', '.')))
        except ValueError:
            continue
    cores = os.cpu_count() or 1
    return max(0.0, min(100.0, sum(values) / cores)) if values else None


def _memory():
    if Path('/proc/meminfo').is_file():
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            match = re.match(r'([^:]+):\s+(\d+)\s+kB', line)
            if match:
                values[match.group(1)] = int(match.group(2)) * 1024
        total, available = values.get('MemTotal'), values.get('MemAvailable')
    else:
        try:
            total = int(_run(['sysctl', '-n', 'hw.memsize']).strip())
        except ValueError:
            total = None
        output = _run(['vm_stat'])
        page_match = re.search(r'page size of (\d+) bytes', output)
        pages = {name: int(value.replace('.', '')) for name, value in
                 re.findall(r'Pages (free|inactive|speculative):\s+(\d+)\.', output)}
        available = sum(pages.values()) * int(page_match.group(1)) if page_match and pages else None
    if not total or available is None:
        return {'total_bytes': None, 'used_bytes': None, 'used_percent': None}
    used = max(0, total - available)
    return {'total_bytes': total, 'used_bytes': used,
            'used_percent': max(0.0, min(100.0, used / total * 100))}


def _memory_pressure_percent():
    """True kernel memory-pressure signal, not derived from free bytes.

    Linux exposes PSI (/proc/pressure/memory some avg10). macOS uses
    a separate categorical pressure state; it does not invent a percentage.
    """
    if Path('/proc/pressure/memory').is_file():
        match = re.search(r'^some\s+avg10=([\d.]+)', Path('/proc/pressure/memory').read_text(), re.M)
        if match:
            return max(0.0, min(100.0, float(match.group(1))))
        return None
    return None


def _memory_pressure_state():
    raw = _run(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip()
    return {'1': 'normal', '2': 'warning', '4': 'critical'}.get(raw)


MACMON_BINARY = '/opt/homebrew/bin/macmon'
_macmon_present = None


def _gpu_percent():
    """GPU utilization from macmon's structured counters (bounded sampling).

    Returns None when macmon is absent or fails; Apple Silicon only in practice.
    """
    global _macmon_present
    if _macmon_present is False:
        return None
    if _macmon_present is None:
        _macmon_present = Path(MACMON_BINARY).is_file()
        if not _macmon_present:
            return None
    try:
        result = subprocess.run([MACMON_BINARY, 'pipe', '-s', '1', '-i', '250'],
                                capture_output=True, text=True, timeout=4, check=False)
        line = result.stdout.strip().splitlines()[-1]
        sensors = json.loads(line)
        usage = sensors.get('gpu_usage')
        if isinstance(usage, list) and len(usage) >= 2 and _finite(usage[1]):
            return round(max(0.0, min(100.0, usage[1] * 100.0)), 1)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return None


def _network_counters():
    """Cumulative (in_bytes, out_bytes) across non-loopback interfaces."""
    in_total = out_total = None
    if Path('/proc/net/dev').is_file():
        in_total = out_total = 0
        for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
            name, _, rest = line.partition(':')
            if name.strip() == 'lo':
                continue
            fields = rest.split()
            if len(fields) >= 9:
                in_total += int(fields[0])
                out_total += int(fields[8])
        return (in_total, out_total)
    for line in _run(['/usr/sbin/netstat', '-ibn']).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or not fields[2].startswith('<Link') or fields[0].startswith('lo'):
            continue
        # Address may be absent; the final seven columns are packet/error/byte counts.
        try:
            counts = fields[-7:]
            in_b, out_b = int(counts[2]), int(counts[5])
        except ValueError:
            continue
        in_total = (in_total or 0) + in_b
        out_total = (out_total or 0) + out_b
    return (in_total, out_total)


def _disk_counters():
    """Cumulative read/write counters in MB from Linux diskstats or macOS
    IOBlockStorageDriver. Missing counters stay unavailable.
    """
    counters = {}
    if Path('/proc/diskstats').is_file():
        for line in Path('/proc/diskstats').read_text().splitlines():
            fields = line.split()
            if len(fields) < 14 or fields[2].startswith(('loop', 'ram')):
                continue
            # sectors are 512 bytes.
            counters[fields[2]] = {'read_mb': int(fields[5]) * 512 / 1e6,
                                   'write_mb': int(fields[9]) * 512 / 1e6,
                                   'total_mb': (int(fields[5]) + int(fields[9])) * 512 / 1e6}
        return counters
    try:
        result = subprocess.run(['/usr/sbin/ioreg', '-a', '-r', '-c', 'IOBlockStorageDriver', '-d', '1'],
                                capture_output=True, timeout=2, check=True)
        for index, disk in enumerate(plistlib.loads(result.stdout)):
            stats = disk.get('Statistics', {})
            read, write = stats.get('Bytes (Read)'), stats.get('Bytes (Write)')
            if _finite(read) and _finite(write):
                counters[str(disk.get('IORegistryEntryID',index))] = {
                    'read_mb':read/1e6,'write_mb':write/1e6,'total_mb':(read+write)/1e6}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return counters


# Disk discovery adapted from MacStatusBoard status_agent.py collect_disks().
_disk_info_cache = {}


def _disk_candidates():
    partitions = []
    output = _run(['df', '-k'])
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 6 and fields[0].startswith('/dev/disk'):
            partitions.append((fields[0], fields[-1]))
    active = {mount: device for device, mount in partitions}
    for path in list(_disk_info_cache):
        if active.get(path) != _disk_info_cache[path][0]:
            del _disk_info_cache[path]
    candidates = [('/System/Volumes/Data', 'Mac SSD')]
    for device, path in partitions:
        if not path.startswith('/Volumes/'):
            continue
        if path not in _disk_info_cache:
            try:
                raw = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', path],
                                     capture_output=True, timeout=2, check=False).stdout
                _disk_info_cache[path] = (device, plistlib.loads(raw))
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
        info = _disk_info_cache[path][1]
        if info.get('Internal') is not False or info.get('BusProtocol') == 'Disk Image':
            continue
        candidates.append((path, info.get('VolumeName') or Path(path).name))
    return candidates


def _disks():
    disks = []
    for path, name in _disk_candidates():
        if any(d['mountpoint'] == path for d in disks):
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue  # A volume can disappear between discovery and sampling.
        disks.append({'name': _bounded(name, 48), 'mountpoint': path, 'internal': path == '/System/Volumes/Data',
                      'total_gb': round(usage.total / 1024 ** 3, 1),
                      'free_gb': round(usage.free / 1024 ** 3, 1),
                      'used_percent': round(usage.used / usage.total * 100, 1) if usage.total else None})
        if len(disks) >= 3:
            break
    return disks


def _uptime_seconds():
    if Path('/proc/uptime').is_file():
        try:
            return float(Path('/proc/uptime').read_text().split()[0])
        except (OSError, ValueError):
            return None
    match = re.search(r'sec\s*=\s*(\d+)', _run(['sysctl', '-n', 'kern.boottime']))
    return max(0.0, time.time() - float(match.group(1))) if match else None


def _fan_status():
    """Structured fan/thermal block from the daemon socket; None per field."""
    try:
        response = daemon_request({'cmd': 'status'})
        status = response.get('status') if isinstance(response, dict) else None
        if not status:
            return None
        groups = {g.get('group'): g for g in status.get('groups', []) if isinstance(g, dict)}
        def temp(name):
            group = groups.get(name)
            value = group.get('max') if group else None
            return round(value, 1) if _finite(value) and 1 < value < 150 else None
        fans = []
        for fan in status.get('fans', []):
            info = fan.get('info', {}) if isinstance(fan, dict) else {}
            rpm = info.get('actualRPM')
            if _finite(rpm) and rpm >= 0:
                fans.append({'name': _bounded(info.get('name') or ('Fan ' + str(info.get('index', '?'))), 32),
                             'rpm': round(rpm), 'target': fan.get('commandedRPM'),
                             'min_rpm': info.get('minRPM'), 'max_rpm': info.get('maxRPM'),
                             'safety_engaged': bool(fan.get('safetyEngaged'))})
        mode = status.get('controlMode')
        return {'fans': fans or None,
                'temp_cpu_c': temp('cpu'), 'temp_gpu_c': temp('gpu'), 'temp_hotspot_c': temp('hotspot'),
                'thermal_pressure': status.get('thermalPressure'),
                'control_mode': mode, 'active_profile': status.get('activeProfileName'),
                'safety_engaged': bool(status.get('safetyEngaged'))}
    except (OSError, RuntimeError, ValueError):
        return None


def acquire_sample(prev_net=None, prev_disk=None, now=None):
    """One host sample. Rates are computed from caller-held cumulative state."""
    now = time.time() if now is None else now
    with contextlib.suppress(Exception):
        cpu = _cpu_percent()
    memory = _memory()
    sample = {
        'sampled_at': now,
        'cpu_percent': locals().get('cpu'),
        'gpu_percent': _gpu_percent(),
        'load_average': list(os.getloadavg()) if hasattr(os, 'getloadavg') else [],
        'memory_percent': memory['used_percent'],
        'memory_used_gb': round(memory['used_bytes'] / 1024 ** 3, 2) if _finite(memory['used_bytes']) else None,
        'memory_total_gb': round(memory['total_bytes'] / 1024 ** 3, 2) if _finite(memory['total_bytes']) else None,
        'memory_pressure_percent': _memory_pressure_percent(),
        'memory_pressure_state': _memory_pressure_state(),
        'net_in_bps': None, 'net_out_bps': None,
        'disk_read_bps': None, 'disk_write_bps': None, 'disk_total_bps': None,
        'disks': [],
        'uptime_hours': round(uptime / 3600, 2) if (uptime := _uptime_seconds()) is not None else None,
    }
    try:
        counters = _network_counters()
    except Exception:
        counters = (None, None)
    if None not in counters:
        if prev_net and now > prev_net[0]:
            dt = now - prev_net[0]
            sample['net_in_bps'] = max(0.0, (counters[0] - prev_net[1]) / dt)
            sample['net_out_bps'] = max(0.0, (counters[1] - prev_net[2]) / dt)
        prev_net = (now, counters[0], counters[1])
    try:
        disk_counters = _disk_counters()
    except Exception:
        disk_counters = {}
    if disk_counters:
        read_mb = sum(v['read_mb'] for v in disk_counters.values() if v['read_mb'] is not None) or None
        write_mb = sum(v['write_mb'] for v in disk_counters.values() if v['write_mb'] is not None) or None
        total_mb = sum(v['total_mb'] for v in disk_counters.values() if v['total_mb'] is not None) or None
        if prev_disk and now > prev_disk[0]:
            dt = now - prev_disk[0]
            if read_mb is not None and prev_disk[1] is not None:
                sample['disk_read_bps'] = max(0.0, (read_mb - prev_disk[1]) * 1e6 / dt)
            if write_mb is not None and prev_disk[2] is not None:
                sample['disk_write_bps'] = max(0.0, (write_mb - prev_disk[2]) * 1e6 / dt)
            if total_mb is not None and prev_disk[3] is not None:
                sample['disk_total_bps'] = max(0.0, (total_mb - prev_disk[3]) * 1e6 / dt)
        prev_disk = (now, read_mb, write_mb, total_mb)
    try:
        sample['disks'] = _disks()
    except Exception:
        sample['disks'] = []
    fan = _fan_status()
    if fan:
        sample.update({
            'fans': fan.get('fans'), 'temp_cpu_c': fan.get('temp_cpu_c'),
            'temp_gpu_c': fan.get('temp_gpu_c'), 'temp_hotspot_c': fan.get('temp_hotspot_c'),
            'thermal_pressure': fan.get('thermal_pressure'),
            'fan_control_mode': fan.get('control_mode'),
            'fan_active_profile': fan.get('active_profile'),
            'fan_safety_engaged': fan.get('safety_engaged'),
        })
    return sample, prev_net, prev_disk


def _hostname():
    global _HOSTNAME
    if _HOSTNAME is None:
        import socket
        try:
            _HOSTNAME = socket.gethostname().split('.')[0]
        except OSError:
            _HOSTNAME = 'localhost'
    return _HOSTNAME


_HOSTNAME = None


# --- task activity (adapted from MacStatusBoard dashboard_sources.py) --------

def _display_title(value):
    text = _bounded(value, 96)
    if re.search(r"密码|password|api[_ -]?key|bearer |sk-[A-Za-z0-9]|https?://", text, re.I):
        return '未命名任务'
    return text or '未命名任务'


def _classify(status, updated, now, completed=0, active_age=None):
    if status in ('completed', 'done'):
        return 'completed' if completed and 0 <= now - completed <= 300 else None
    if status in ('cancelled', 'interrupted', 'timed_out'):
        return None
    if status in ('failed', 'needs_attention', 'queued', 'waiting', 'awaiting_input'):
        return 'attention' if 0 <= now - updated <= 86400 else None
    if status in ('inProgress', 'running', 'starting', 'stopping', 'retry'):
        if now - updated > 86400:
            return None
        return 'unknown' if active_age is not None and active_age > 120 else 'running'
    return None


def _sort_tasks(items):
    items.sort(key=lambda t: (TASK_PRIORITY.get(t['status'], 2), -t.get('updated_at', 0), t['id']))
    return items[:MAX_TASKS]


def _task_model(record):
    value = record.get('current_model')
    if not value:
        actual = record.get('actual_models')
        if isinstance(actual, list) and len(actual) == 1:
            value = actual[0]
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/+\-]{1,160}', value):
        return None
    return _bounded(value.rsplit('/', 1)[-1], 80) or None


def _progress_for(task_id):
    """Real progress only: a stored report with numbers and/or a run URL."""
    value = read_json(STATE / 'progress' / (task_id + '.json'), {}) or {}
    if not value:
        return None
    progress = {k: value.get(k) for k in ('status', 'phase', 'percent', 'message',
                                          'current', 'total', 'eta_seconds', 'github_run')
                if value.get(k) is not None}
    run = progress.get('github_run')
    if run is not None and not (isinstance(run, str) and re.fullmatch(r'https://github\.com/[^/]+/[^/]+/actions/runs/\d+', run)):
        progress.pop('github_run', None)
    return progress or None


def collect_opencode_tasks(now=None):
    now = now or time.time()
    items = []
    root = STATE / 'tasks'
    if not root.is_dir():
        return {'state': 'ok', 'sampled_at': now, 'total': 0, 'active': 0, 'items': []}
    for path in root.glob('*.json'):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        updated = epoch(record.get('updated_at'))
        completed = epoch(record.get('finished_at'))
        observed = epoch(record.get('last_observed_at')) or updated
        status = _classify(record.get('status'), updated, now, completed, now - observed)
        if status == 'running' and record.get('status') == 'running':
            item = {'id': _bounded(record.get('id'), 64), 'title': _display_title(record.get('title')),
                    'project': _bounded(record.get('group_title') or Path(record.get('source_dir', '')).name, 36),
                    'status': status, 'model': _task_model(record), 'effort': record.get('current_variant') or record.get('effort'),
                    'started_at': epoch(record.get('started_at')), 'ended_at': completed, 'updated_at': updated}
            progress = _progress_for(record.get('id') or path.stem)
            if progress:
                item['progress'] = progress
            items.append(item)
    return {'state': 'ok', 'sampled_at': now, 'total': len(items),
            'active': sum(1 for t in items if t['status'] != 'completed'), 'items': _sort_tasks(items)}


def _ro_db(path):
    con = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


class CodexTasks:
    """Incremental read-only tail of Codex rollout events plus sqlite state."""

    def __init__(self, root=None):
        self.root = Path(root or Path.home() / '.codex')
        self.tails = {}

    def activity(self, path, now):
        path = Path(path)
        cache = self.tails.setdefault(str(path), {'offset': 0, 'at': 0, 'event': None, 'carry': b''})
        try:
            with path.open('rb') as handle:
                size = handle.seek(0, 2)
                if size < cache['offset']:
                    cache.update(offset=0, at=0, event=None, carry=b'')
                start = max(cache['offset'], size - 262144)
                handle.seek(start)
                if start > cache['offset']:
                    handle.readline()
                    cache['carry'] = b''
                raw = cache['carry'] + handle.read(262144)
                cache['offset'] = handle.tell()
            lines = raw.split(b'\n')
            cache['carry'] = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if event.get('type') == 'event_msg':
                    at = epoch(event.get('timestamp'))
                    payload = event.get('payload', {})
                    if at >= cache['at']:
                        cache['at'] = at
                    if payload.get('type') in ('task_started', 'task_complete', 'turn_aborted'):
                        cache['event'] = payload['type']
            return cache
        except OSError:
            return {'at': 0, 'event': None}

    def collect(self, now=None):
        now = now or time.time()
        items = []
        meta = self.root / 'state_5.sqlite'
        history = self.root / 'thread_history_1.sqlite'
        if not meta.is_file() or not history.is_file():
            return {'state': 'unavailable', 'sampled_at': now, 'total': 0, 'active': 0, 'items': []}
        with contextlib.closing(_ro_db(meta)) as meta_con, contextlib.closing(_ro_db(history)) as history_con:
            columns = {row[1] for row in meta_con.execute('PRAGMA table_info(threads)')}
            model_cols = ',model,reasoning_effort' if {'model','reasoning_effort'} <= columns else ',NULL AS model,NULL AS reasoning_effort'
            threads = meta_con.execute(
                'SELECT id,name,title,cwd,rollout_path,updated_at' + model_cols + ' FROM threads '
                'WHERE archived=0 AND updated_at>? ORDER BY updated_at DESC LIMIT 100',
                (now - 86400,)).fetchall()
            for thread in threads:
                turn = history_con.execute(
                    'SELECT status,started_at,completed_at FROM thread_turns WHERE thread_id=? '
                    'ORDER BY rollout_ordinal DESC LIMIT 1', (thread['id'],)).fetchone()
                if not turn:
                    continue
                activity = self.activity(thread['rollout_path'], now)
                updated = max(thread['updated_at'], activity['at'])
                status = turn['status']
                completed = turn['completed_at'] or 0
                if status == 'inProgress' and activity['event'] == 'task_complete':
                    status, completed = 'completed', activity['at']
                if status == 'inProgress' and activity['event'] == 'turn_aborted':
                    status = 'interrupted'
                state = _classify(status, updated, now, completed,
                                  now - (activity['at'] or turn['started_at'] or 0))
                if state:
                    items.append({'id': thread['id'], 'title': _display_title(thread['name'] or thread['title']),
                                  'project': _bounded(Path(thread['cwd']).name, 36), 'status': state,
                                  'model': _bounded(thread['model'],80) or None, 'effort': _bounded(thread['reasoning_effort'],20) or None,
                                  'started_at': turn['started_at'] or 0, 'ended_at': completed,
                                  'updated_at': updated})
        return {'state': 'ok', 'sampled_at': now, 'total': len(items),
                'active': sum(1 for t in items if t['status'] != 'completed'), 'items': _sort_tasks(items)}


# --- quota view (delegate-daemon cache + CodexBar adapter, background only) --

def _window_label(minutes):
    return WINDOW_LABELS.get(minutes, '额度')


def normalize_worker(provider, value):
    value = value or {}
    result = {'id': provider, 'name': PROVIDER_LABELS.get(provider, provider),
              'state': value.get('state', 'unavailable'), 'sampled_at': epoch(value.get('sampled_at')),
              'windows': [], 'balances': []}
    for window in (value.get('windows') or [])[:4]:
        remaining = window.get('remaining_percent')
        if window.get('valid') and _finite(remaining) and 0 <= remaining <= 100:
            result['windows'].append({'label': _window_label(window.get('duration_minutes')),
                                      'remaining_percent': round(remaining, 1),
                                      'resets_at': epoch(window.get('resets_at'))})
    for balance in (value.get('balances') or [])[:2]:
        if _finite(balance.get('remaining')):
            result['balances'].append({'currency': _bounded(balance.get('currency'), 8),
                                       'remaining': balance['remaining']})
    return result


def normalize_codex(data, now=None):
    now = now or time.time()
    row = next((v for v in data if v.get('provider') == 'codex'), {}) if isinstance(data, list) else data
    usage = row.get('usage') or {}
    result = {'id': 'codex', 'name': 'Codex', 'state': 'unavailable', 'sampled_at': 0,
              'windows': [], 'balances': []}
    if row.get('error'):
        return result
    for key in ('primary', 'secondary', 'tertiary'):
        window = usage.get(key) or {}
        used = window.get('usedPercent')
        if _finite(used) and 0 <= used <= 100:
            minutes = window.get('windowMinutes') or window.get('windowDurationMins')
            result['windows'].append({'label': _window_label(minutes),
                                      'remaining_percent': round(100 - used, 1),
                                      'resets_at': epoch(window.get('resetsAt'))})
    if result['windows']:
        result.update(state='ok', sampled_at=epoch(usage.get('updatedAt')) or now)
    return result


def collect_quotas(now=None):
    """Quota snapshot; worker providers from the daemon cache, Codex via CLI."""
    import datetime
    now = now or time.time()
    out = []
    cached = read_json(STATE / 'quota.json', {}) or {}
    for provider in WORKER_PROVIDERS:
        out.append(normalize_worker(provider, cached.get(provider)))
    codex = {'id': 'codex', 'name': 'Codex', 'state': 'unavailable', 'sampled_at': 0, 'windows': [], 'balances': []}
    try:
        result = subprocess.run([CODEXBAR_CLI, 'usage', '--provider', 'codex', '--source', 'oauth',
                                 '--format', 'json', '--no-credits'], capture_output=True, text=True, timeout=35)
        codex = normalize_codex(json.loads(result.stdout), now)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    out.insert(0, codex)
    return out


# --- history series -----------------------------------------------------------

_SERIES_FIELDS = ('cpu_percent', 'gpu_percent', 'memory_percent', 'memory_pressure_percent', 'net_in_bps',
                  'net_out_bps', 'disk_read_bps', 'disk_write_bps', 'disk_total_bps', 'temp_cpu_c', 'temp_gpu_c')


def series_from_rows(rows):
    """Extract per-metric point lists; missing values are omitted, never zero."""
    series = {name: [] for name in _SERIES_FIELDS}
    for row in rows:
        ts, data = row
        for name in _SERIES_FIELDS:
            value = data.get(name)
            if _finite(value):
                series[name].append({'t': ts, 'v': round(value, 2)})
        fans = data.get('fans')
        if isinstance(fans, list) and fans and _finite(fans[0].get('rpm')):
            series.setdefault('fans_rpm', []).append({'t': ts, 'v': round(fans[0]['rpm'], 1)})
    return series


# --- bounded store ------------------------------------------------------------

class TelemetryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS raw(ts REAL PRIMARY KEY, data TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS minutes(ts INTEGER PRIMARY KEY, data TEXT NOT NULL)')

    @contextlib.contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def add_raw(self, sample):
        with self._connect() as con:
            con.execute('INSERT OR REPLACE INTO raw(ts, data) VALUES(?, ?)',
                        (sample['sampled_at'], json.dumps(sample, separators=(',', ':'))))

    def add_minute(self, minute, aggregate):
        with self._connect() as con:
            con.execute('INSERT OR REPLACE INTO minutes(ts, data) VALUES(?, ?)',
                        (minute, json.dumps(aggregate, separators=(',', ':'))))

    def prune(self, now):
        with self._connect() as con:
            con.execute('DELETE FROM raw WHERE ts < ?', (now - RAW_TTL_SECONDS,))
            con.execute('DELETE FROM minutes WHERE ts < ?', (int(now - MINUTES_TTL_SECONDS) // 60,))
            for table, cap in (('raw', RAW_CAP), ('minutes', MINUTES_CAP)):
                excess = con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] - cap
                if excess > 0:
                    con.execute('DELETE FROM ' + table + ' WHERE ts IN (SELECT ts FROM ' + table +
                                ' ORDER BY ts ASC LIMIT ?)', (excess,))

    def raw_rows(self, since, limit=5000):
        with self._connect() as con:
            return [(ts, json.loads(data)) for ts, data in con.execute(
                'SELECT ts, data FROM raw WHERE ts >= ? ORDER BY ts ASC LIMIT ?', (since, limit))]

    def minute_rows(self, since, limit=2000):
        with self._connect() as con:
            return [(ts * 60.0, json.loads(data)) for ts, data in con.execute(
                'SELECT ts, data FROM minutes WHERE ts >= ? ORDER BY ts ASC LIMIT ?',
                (int(since) // 60, limit))]


class _MinuteBuffer:
    """Incrementally aggregate one-minute windows; flush on minute rollover."""

    def __init__(self):
        self.minute = None
        self.sums = {}
        self.count = 0
        self.last = None

    def add(self, sample):
        minute = int(sample['sampled_at'] // 60)
        if self.minute is None:
            self.minute = minute
        if minute != self.minute:
            flushed = self.flush()
            self.minute = minute
            if flushed:
                return flushed
        self.count += 1
        self.last = sample
        for name in _SERIES_FIELDS:
            value = sample.get(name)
            if _finite(value):
                self.sums[name] = self.sums.get(name, 0.0) + value
        return None

    def flush(self):
        if self.minute is None or not self.count:
            return None
        aggregate = {'sampled_at': self.minute * 60.0 + 59, 'samples': self.count}
        for name, total in self.sums.items():
            aggregate[name] = round(total / self.count, 2)
        for key in ('disks', 'fans', 'thermal_pressure', 'fan_control_mode',
                    'fan_active_profile', 'fan_safety_engaged'):
            if self.last:
                aggregate[key] = self.last.get(key)
        self.sums = {}
        self.count = 0
        return self.minute, aggregate


# --- collector ----------------------------------------------------------------

class Collector:
    """Background threads: host 2s, tasks 3s, quotas 300s; board file publisher."""

    def __init__(self, state_dir=None, host_interval=HOST_INTERVAL_SECONDS,
                 task_interval=TASK_INTERVAL_SECONDS, quota_interval=QUOTA_INTERVAL_SECONDS,
                 codex_root=None):
        self.state = Path(state_dir) if state_dir else STATE
        self.host_interval = host_interval
        self.task_interval = task_interval
        self.quota_interval = quota_interval
        self.codex_root = codex_root
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.threads = []
        self.codex = CodexTasks(root=codex_root) if codex_root else CodexTasks()
        self.store = None
        self._prev_net = None
        self._prev_disk = None
        self._minute = _MinuteBuffer()
        self._last_prune = 0.0
        self.latest_system = None
        self.latest_tasks = {'codex': {'state': 'unavailable', 'sampled_at': 0, 'total': 0, 'active': 0, 'items': []},
                             'opencode': {'state': 'unavailable', 'sampled_at': 0, 'total': 0, 'active': 0, 'items': []}}
        self.latest_quotas = []

    @property
    def store_path(self):
        return self.state / 'host' / 'telemetry.sqlite'

    @property
    def board_path(self):
        return self.state / BOARD_FILE

    # -- lifecycle
    def start(self):
        if self.threads:
            return self
        try:
            self.store = TelemetryStore(self.store_path)
        except OSError:
            self.store = None  # history unavailable; snapshot path still works
        for target in (self._host_loop, self._task_loop, self._quota_loop):
            thread = threading.Thread(target=target, daemon=True, name='host-telemetry-' + target.__name__)
            thread.start()
            self.threads.append(thread)
        return self

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)
        self.threads = []

    @property
    def running(self):
        return any(thread.is_alive() for thread in self.threads)

    # -- loops (each iteration fully fault-isolated)
    def _host_loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.collect_host_once()
            except Exception:
                pass  # keep the collector alive; stale data must never look fresh
            self.stop_event.wait(max(0.1, self.host_interval - (time.monotonic() - started)))

    def _task_loop(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.collect_tasks_once()
            except Exception:
                pass
            self.stop_event.wait(max(0.1, self.task_interval - (time.monotonic() - started)))

    def _quota_loop(self):
        while not self.stop_event.is_set():
            try:
                self.collect_quotas_once()
            except Exception:
                pass
            self.stop_event.wait(self.quota_interval)

    # -- single-shot collection (also used by tests)
    def collect_host_once(self, now=None):
        sample, self._prev_net, self._prev_disk = acquire_sample(
            prev_net=self._prev_net, prev_disk=self._prev_disk, now=now)
        minute_row = self._minute.add(sample)
        if self.store:
            self.store.add_raw(sample)
            if minute_row:
                self.store.add_minute(*minute_row)
            if time.time() - self._last_prune > PRUNE_INTERVAL_SECONDS:
                self.store.prune(time.time())
                self._last_prune = time.time()
        with self.lock:
            self.latest_system = sample
        self.publish_board()
        return sample

    def collect_tasks_once(self, now=None):
        now = now or time.time()
        tasks = {'codex': self.codex.collect(now),
                 'opencode': collect_opencode_tasks(now)}
        with self.lock:
            self.latest_tasks = tasks
        self.publish_board()
        return tasks

    def collect_quotas_once(self, now=None):
        quotas = collect_quotas(now)
        # A failed provider keeps its last good windows (stale, not empty).
        previous = {q['id']: q for q in self.latest_quotas}
        merged = []
        for quota in quotas:
            if quota['state'] != 'ok' and quota['id'] in previous:
                old = previous[quota['id']]
                quota = {**quota, 'windows': old['windows'], 'balances': old['balances'],
                         'sampled_at': old['sampled_at']}
            merged.append(quota)
        with self.lock:
            self.latest_quotas = merged
        self.publish_board()
        return merged

    # -- board v2 payload (owner-only atomic file; consumers read at will)
    def board_payload(self):
        with self.lock:
            latest = json.loads(json.dumps(self.latest_system)) if self.latest_system else {}
            tasks = json.loads(json.dumps(self.latest_tasks))
            quotas = json.loads(json.dumps(self.latest_quotas))
        # v2 firmware matches these exact compact IDs. Keep full provider IDs
        # in the web snapshot/cache; adapt only this independent board copy.
        board_ids = {'kimi-for-coding': 'kimi', 'volcengine-agent-plan': 'ark'}
        for quota in quotas:
            quota['id'] = board_ids.get(quota.get('id'), quota.get('id'))
        unix_time = int(time.time())
        try:
            time.tzset()
            offset = time.localtime(unix_time).tm_gmtoff
        except (AttributeError, OSError):
            offset = 0
        # Explicit backward-compatible aliases: the board firmware expects the
        # original status_agent field names; new collectors use richer names.
        # Both are published so old and new consumers keep working.
        system = dict(latest)
        system['host'] = _hostname()
        system['cpu'] = latest.get('cpu_percent')
        system['cpu_temp_c'] = latest.get('temp_cpu_c')
        system['gpu_temp_c'] = latest.get('temp_gpu_c')
        return {'schema_version': BOARD_SCHEMA_VERSION, 'unix_time': unix_time,
                'utc_offset_seconds': offset, 'system': system, 'tasks': tasks, 'quotas': quotas}

    def publish_board(self):
        payload = self.board_payload()
        target = self.board_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp = __import__('tempfile').mkstemp(prefix='.host-board', dir=str(target.parent))
            with os.fdopen(fd, 'w') as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        except OSError:
            try:
                if 'temp' in dir() and os.path.exists(temp):
                    os.unlink(temp)
            except OSError:
                pass

    # -- read paths for the console (never acquire)
    def snapshot(self, now=None):
        now = now or time.time()
        with self.lock:
            system = json.loads(json.dumps(self.latest_system)) if self.latest_system else None
            tasks = json.loads(json.dumps(self.latest_tasks))
            quotas = json.loads(json.dumps(self.latest_quotas))
        sampled_at = system.get('sampled_at') if system else None
        return {'sampled_at': sampled_at,
                'stale': (sampled_at is None or now - sampled_at > STALE_AFTER_SECONDS),
                'system': system, 'tasks': tasks, 'quotas': quotas}

    def history(self, range_name, now=None, downsample=None):
        now = now or time.time()
        if range_name == '15m':
            since, rows = now - 900, self.store.raw_rows(now - 900) if self.store else []
            interval = HOST_INTERVAL_SECONDS
        elif range_name == '1h':
            since = now - 3600
            rows = self.store.raw_rows(since) if self.store else []
            interval = 15.0
        elif range_name == '24h':
            since = now - 86400
            rows = self.store.minute_rows(since) if self.store else []
            interval = 60.0
        else:
            raise ValueError('Unknown history range')
        if downsample is None:
            downsample = {'15m': 1, '1h': 8, '24h': 1}[range_name]
        if downsample > 1 and len(rows) > 1:
            rows = rows[::downsample]
        return {'range': range_name, 'generated_at': now, 'interval_seconds': interval,
                'series': series_from_rows(rows)}


_COLLECTOR = None
_COLLECTOR_LOCK = threading.Lock()


def ensure_collector():
    """The console-owned shared collector; safe to call from any thread."""
    global _COLLECTOR
    with _COLLECTOR_LOCK:
        if _COLLECTOR is None or not _COLLECTOR.running:
            _COLLECTOR = Collector().start()
        return _COLLECTOR


def current_collector():
    return _COLLECTOR
