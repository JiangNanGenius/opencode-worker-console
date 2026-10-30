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

import allowance_visibility
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
WORKER_PROVIDERS = ('kimi-for-coding', 'volcengine-agent-plan', 'deepseek', 'bailian-token-plan')
PROVIDER_LABELS = {'kimi-for-coding': 'Kimi', 'volcengine-agent-plan': '火山方舟',
                   'deepseek': 'DeepSeek', 'codex': 'Codex', 'bailian-token-plan': '阿里百炼'}
WINDOW_LABELS = {300: '5小时', 1440: '每日', 10080: '每周', 43200: '每月'}

# --- sequential Alibaba plan rows ------------------------------------------
# bailian_plan_edition=sequential keeps Team Credits and the Personal monthly
# allowance in one identity-bound namespace record (plan_quotas.team /
# plan_quotas.personal, top-level windows empty). The shared host-board payload
# carries one independent card per plan; both consumers accept these ids
# verbatim (firmware: arbitrary quota id up to 32 chars, at most 8 rows).
BAILIAN_PROVIDER = 'bailian-token-plan'
SEQUENTIAL_PLAN_ROWS = {'team': ('aliyun-team', '阿里团队'),
                        'personal': ('aliyun-personal', '阿里个人')}
PLAN_CACHE_FILE = 'bailian-plan-quota.json'   # identity-bound namespace record
PLAN_EDITION_CACHE_SECONDS = 60.0             # bounded config read, never secrets
# Rows the Alibaba provider may occupy: sequential replaces the legacy single
# row, and switching back to Team removes the split rows.
BAILIAN_ROW_IDS = (BAILIAN_PROVIDER, 'aliyun', 'aliyun-team', 'aliyun-personal')


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


def _align_skip(handle, budget):
    """Skip a partial boundary-straddling record without buffering it.

    Scans at most ``budget`` bytes for the newline that ends the record
    crossing the read boundary. If the record is larger than the budget the
    cursor simply stops mid-record; the discarded head and the remaining
    fragments never parse into dict events, so they are ignored downstream.
    """
    remaining = budget
    while remaining > 0:
        chunk = handle.read(min(65536, remaining))
        if not chunk:
            return
        index = chunk.find(b'\n')
        if index >= 0:
            # Rewind from the post-read cursor to the first byte after the
            # newline; the cursor already sits len(chunk) past the chunk start.
            handle.seek(index + 1 - len(chunk), 1)
            return
        remaining -= len(chunk)


class CodexTasks:
    """Incremental read-only tail of Codex rollout events plus sqlite state."""

    def __init__(self, root=None):
        self.root = Path(root or Path.home() / '.codex')
        self.tails = {}

    def activity(self, path, now):
        empty = {'at': 0, 'event': None}
        if not isinstance(path, (str, Path)) or not path:
            return empty
        path = Path(path)
        if not any(path.resolve().is_relative_to((self.root / directory).resolve())
                   for directory in ('sessions', 'archived_sessions')):
            return empty
        key = str(path)
        try:
            stat = path.stat()
            cache = self.tails.get(key)
            if cache is None or cache['inode'] != stat.st_ino or stat.st_size < cache['offset']:
                cache = {'offset': 0, 'at': 0, 'carry': b'', 'inode': stat.st_ino,
                         'turns': {}, 'scan_before': None}
                self.tails[key] = cache
            def consume(raw):
                for line in raw.split(b'\n'):
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue  # scalars/lists (including cut mid-record) are not events
                    at = epoch(event.get('timestamp'))
                    cache['at'] = max(cache['at'], at)
                    payload = event.get('payload', {})
                    if not isinstance(payload, dict):
                        continue
                    kind = event.get('type')
                    lifecycle = payload.get('type') if kind == 'event_msg' else None
                    if kind != 'turn_context' and lifecycle not in ('task_started','task_complete','turn_aborted'):
                        continue
                    turn_id = payload.get('turn_id')
                    if not isinstance(turn_id, str) or not turn_id:
                        continue
                    turn = cache['turns'].setdefault(turn_id, {'turn_id': turn_id})
                    if kind == 'turn_context' and at >= turn.get('context_at', 0):
                        turn.update(context_at=at, model=payload.get('model'),
                                    effort=payload.get('effort') or payload.get('reasoning_effort'))
                    elif lifecycle == 'task_started':
                        turn['started_at'] = max(turn.get('started_at', 0), at)
                    if lifecycle and at >= turn.get('event_at', 0):
                        turn.update(event=lifecycle, event_at=at)
            with path.open('rb') as handle:
                # One bounded bootstrap, then incremental append reads. If a
                # long turn's boundary predates the tail, backfill one bounded
                # chunk per sample until its context is located.
                if cache['offset'] == 0:
                    start = max(0, stat.st_size - 2097152)
                    cache['scan_before'] = start
                else:
                    start = cache['offset']
                handle.seek(start)
                if start and start != cache['offset']:
                    _align_skip(handle, 2097152)
                raw = cache['carry'] + handle.read(2097152)
                cache['offset'] = handle.tell()
                lines = raw.split(b'\n')
                cache['carry'] = lines.pop()
                if len(cache['carry']) > 2097152:
                    cache['carry'] = b''  # oversized content is not activity metadata
                consume(b'\n'.join(lines))
                has_start = any(t.get('started_at') or t.get('context_at') for t in cache['turns'].values())
                before = cache.get('scan_before') or 0
                if not has_start and before:
                    # One bounded window per sample, always advancing.
                    start = max(0, before - 1048576)
                    cache['scan_before'] = start
                    handle.seek(start)
                    if start:
                        _align_skip(handle, 1048576)
                    # A single JSONL record longer than the window (image or
                    # tool dumps) can carry the alignment skip past the window
                    # end. The remaining span is then 0, never a negative read.
                    span = before - handle.tell()
                    if span > 0:
                        consume(handle.read(span))
            ranked = sorted(cache['turns'].values(), key=lambda t: max(t.get('started_at',0), t.get('context_at',0), t.get('event_at',0)), reverse=True)[:4]
            cache['turns'] = {t['turn_id']: t for t in ranked}
            started = [t for t in ranked if t.get('started_at') or t.get('context_at')]
            current = max(started, key=lambda t: max(t.get('started_at',0),t.get('context_at',0))) if started else (ranked[0] if ranked else {})
            return {**current, 'at': cache['at'], 'size': stat.st_size}
        except (OSError, ValueError):
            # Malformed records or a damaged cursor must never poison the
            # whole Codex source; fall back to the database projection.
            return empty

    def collect(self, now=None, owner_ids=()):
        now = now or time.time()
        items = []
        meta = self.root / 'state_5.sqlite'
        history = self.root / 'thread_history_1.sqlite'
        if not meta.is_file() or not history.is_file():
            return {'state': 'unavailable', 'sampled_at': now, 'total': 0, 'active': 0, 'items': []}
        with contextlib.closing(_ro_db(meta)) as meta_con, contextlib.closing(_ro_db(history)) as history_con:
            columns = {row[1] for row in meta_con.execute('PRAGMA table_info(threads)')}
            if not {'id', 'name', 'title', 'cwd', 'rollout_path', 'updated_at', 'archived'} <= columns:
                # Drifted or absent threads projection: report unavailable
                # instead of letting one failing query discard every board row.
                return {'state': 'unavailable', 'sampled_at': now, 'total': 0, 'active': 0, 'items': []}
            model_cols = ',model,reasoning_effort' if {'model','reasoning_effort'} <= columns else ',NULL AS model,NULL AS reasoning_effort'
            source_cols = ''.join(',' + key if key in columns else ',NULL AS ' + key for key in ('source','thread_source'))
            select = 'SELECT id,name,title,cwd,rollout_path,updated_at' + model_cols + source_cols + ' FROM threads '
            threads = list(meta_con.execute(
                select + 'WHERE archived=0 AND updated_at>? ORDER BY updated_at DESC LIMIT 100',
                (now - 86400,)).fetchall())
            seen = {row['id'] for row in threads}
            board_ids = set(seen)
            for owner in sorted(set(owner_ids))[:100]:
                if owner not in seen:
                    row = meta_con.execute(select + 'WHERE id=?', (owner,)).fetchone()
                    if row:
                        threads.append(row)
                        seen.add(owner)
            edges, relation_ids = {}, set(owner_ids)
            if seen and meta_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges'").fetchone():
                # Bounded relationship lookup, including finished children of a
                # visible owner; never scan turns/items or complete transcripts.
                placeholders = ','.join('?' for _ in seen)
                rows = meta_con.execute('SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges WHERE child_thread_id IN (' + placeholders + ') OR parent_thread_id IN (' + placeholders + ') LIMIT 200', tuple(seen) + tuple(seen)).fetchall()
                for edge in rows:
                    child = edge['child_thread_id']
                    edges[child] = edge['parent_thread_id']
                    relation_ids.add(child)
                    if child not in seen and len(threads) < 200:
                        row = meta_con.execute(select + 'WHERE id=?', (child,)).fetchone()
                        if row:
                            threads.append(row)
                            seen.add(child)
            turn_columns = {row[1] for row in history_con.execute('PRAGMA table_info(thread_turns)')}
            turn_extra = ''.join(',' + key if key in turn_columns else ',NULL AS ' + key for key in ('turn_id','rollout_end_byte_offset'))
            skipped = 0
            for thread in threads:
                # One malformed record (absent thread_turns table, damaged
                # projection row, oversized rollout) degrades only its own
                # thread; it must never discard every Codex task.
                try:
                    turn = history_con.execute(
                        'SELECT status,started_at,completed_at' + turn_extra + ' FROM thread_turns WHERE thread_id=? '
                        'ORDER BY rollout_ordinal DESC LIMIT 1', (thread['id'],)).fetchone() if turn_columns else None
                except Exception:
                    turn = None
                try:
                    activity = self.activity(thread['rollout_path'], now)
                except Exception:
                    activity = {'at': 0, 'event': None}
                try:
                    updated = max(thread['updated_at'] or 0, activity['at'])
                    # SQLite is an asynchronous projection. Reconcile only a
                    # matching turn or an explicitly newer start/context; never
                    # let an old completion override a different in-flight turn.
                    status = turn['status'] if turn else None
                    started_at = (turn['started_at'] or 0) if turn else 0
                    completed = (turn['completed_at'] or 0) if turn else 0
                    db_turn_id = turn['turn_id'] if turn else None
                    evidence_start = activity.get('started_at') or activity.get('context_at') or 0
                    same_turn = bool(db_turn_id and db_turn_id == activity.get('turn_id'))
                    newer_turn = bool(evidence_start and evidence_start > started_at and
                                      (not completed or evidence_start > completed) and not same_turn)
                    if newer_turn:
                        status, completed = 'inProgress', 0
                        started_at = evidence_start
                    if (same_turn or newer_turn) and activity.get('event_at',0) >= max(started_at, completed):
                        event_status = {'task_started':'inProgress','task_complete':'completed','turn_aborted':'interrupted'}
                        status = event_status.get(activity.get('event'),status)
                        if status in ('completed','interrupted'):
                            completed = activity['event_at']
                    if turn and turn['rollout_end_byte_offset'] and turn['rollout_end_byte_offset'] > activity.get('size',float('inf')) and not same_turn and not newer_turn:
                        status, completed = 'unknown', 0
                    state = _classify(status, updated, now, completed,
                                      now - (activity['at'] or started_at or 0))
                    if status == 'unknown':
                        state = 'unknown'
                    source = thread['source'] or ''
                    is_subagent = thread['id'] in edges or thread['thread_source'] == 'subagent' or 'subagent' in source
                    if is_subagent and thread['id'] not in edges:
                        try:
                            origin = json.loads(source)
                            parent = origin.get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                            if isinstance(parent,str):
                                edges[thread['id']] = parent
                        except (ValueError,AttributeError):
                            pass
                    board_visible = bool(state) and thread['id'] in board_ids
                    if state is None and thread['id'] in relation_ids and turn:
                        state = {'completed': 'completed', 'failed': 'attention', 'interrupted': 'interrupted'}.get(status)
                    if state or thread['id'] in relation_ids:
                        items.append({'id': thread['id'], 'title': _display_title(thread['name'] or thread['title']),
                                      'project': _bounded(Path(thread['cwd']).name, 36), 'status': state or 'unknown',
                                      'model': _bounded(activity.get('model') if newer_turn or (same_turn and activity.get('context_at')) else thread['model'],80) or None,
                                      'effort': _bounded(activity.get('effort') if newer_turn or (same_turn and activity.get('context_at')) else thread['reasoning_effort'],20) or None,
                                      'started_at': started_at, 'ended_at': completed,
                                      'updated_at': updated, 'parent_thread_id': edges.get(thread['id']),
                                      '_board_visible': board_visible,
                                      'turn_status': status, 'is_subagent': is_subagent})
                except Exception:
                    skipped += 1
        # A child whose parent is unavailable is not a main task. Keep only
        # children attached to a retained tree; guardians never become roots.
        for _ in range(8):
            ids = {item['id'] for item in items}
            retained = [item for item in items if not item['is_subagent'] or item.get('parent_thread_id') in ids]
            if len(retained) == len(items):
                break
            items = retained
        self.tails = {key: value for key, value in self.tails.items()
                      if key in {row['rollout_path'] for row in threads}}
        return {'state': 'ok', 'sampled_at': now, 'total': len(items),
                'active': sum(1 for t in items if t['status'] in ('running','attention','unknown')), 'items': sorted(items, key=lambda t: -t['updated_at'])[:200]}


def collect_worker_links(now=None):
    """Only exact owner IDs; never export job objectives or filesystem paths."""
    now = now or time.time()
    links = {}
    for path in sorted((STATE / 'tasks').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:500]:
        record = read_json(path, {}) or {}
        if record.get('status') not in ('starting','running','stopping','uncertain') and record.get('status') != 'queued' and now - epoch(record.get('updated_at')) > 86400:
            continue
        owner = record.get('owner_thread_id')
        if not isinstance(owner, str) or not owner:
            continue
        links.setdefault(owner, []).append({'id': _bounded(record.get('id'), 64),
            'title': _display_title(record.get('title')), 'status': _bounded(record.get('status'), 32),
            'model': _task_model(record), 'source': 'opencode'})
    return links


# --- quota view (delegate-daemon cache + CodexBar adapter, background only) --

def _window_label(minutes):
    if minutes in WINDOW_LABELS:
        return WINDOW_LABELS[minutes]
    if _finite(minutes) and minutes >= 43200:
        # Monthly windows drift with their subscription anniversary; a 30-day
        # Personal plan window must still read 每月 rather than 额度.
        return '每月'
    return '额度'


# Additive window metadata for the host overview: the Alibaba Token Plan row
# shows an actual Credits amount, Ark shows AFP, other providers stay percent.
# Sequential plan rows: Team is a Credits pool; Personal reports its own
# percentage window unit and gets no Credits fallback.
WINDOW_UNITS = {'bailian-token-plan': 'credits', 'aliyun-team': 'credits',
                'volcengine-agent-plan': 'afp'}
# Live cached provider readings are considered fresh for the same 15 minutes the
# quota view uses. Manual snapshots instead publish their own configured horizon
# (bounded by the plan reset) so a 24 h snapshot is not falsely aged out by a
# consumer's 15-minute default.
LIVE_QUOTA_VALID_SECONDS = 900


def billing_overlay(provider):
    """Safe exhaustion metadata for host/board views; never messages or IDs.

    The daemon cache holds raw account records; the durable billing circuit is
    what makes a provider genuinely uncallable (for example Kimi's confirmed
    monthly exhaustion) and must reach the host overview instead of a
    misleading positive window percentage.
    """
    try:
        import quota
        block = quota.billing_block(provider)
    except Exception:
        return {}
    if not block:
        return {}
    reason = block.get('reason') if isinstance(block.get('reason'), str) else 'billing_error'
    return {'available': False,
            'billing': {'reason': reason,
                        'monthly_exhausted': reason == 'monthly_usage_limit'}}


def _kimi_window_key(window):
    kind = window.get('kind')
    if kind in ('short', 'weekly', 'month_total', 'month_code'):
        return kind
    name = window.get('name')
    if name in ('limit5h', 'limit_5h') or window.get('duration_minutes') == 300:
        return 'short'
    if name in ('limit7d', 'limit_7d', 'overall') or window.get('duration_minutes') == 10080:
        return 'weekly'
    return 'name:' + str(name) if name else 'duration:' + str(window.get('duration_minutes'))


def _kimi_order_windows(windows):
    """Known account kinds first so the downstream four-window cap keeps them."""
    keyed = {_kimi_window_key(row): row for row in windows}
    ordered = [keyed.pop(kind) for kind in ('short', 'weekly', 'month_total', 'month_code')
               if kind in keyed]
    ordered.extend(keyed.values())
    return ordered


def _kimi_host_windows(value):
    """Merge only fresh, verified account windows; never acquire credentials.

    Known account kinds (5 h, weekly, monthly total, monthly code) are ordered
    ahead of legacy endpoint windows so a three- or five-window source plus
    four fresh account windows can never lose a real month card to insertion
    order and the four-window render cap. Expired or invalid account rows never
    replace the endpoint source, so an aging or unknown reading keeps its own
    provenance instead of being silently upgraded.
    """
    windows = [dict(row) for row in value.get('windows') or [] if isinstance(row, dict)]
    account = value.get('account')
    now = time.time()
    accepted = False
    if isinstance(account, dict):
        binding = account.get('account_binding')
        sampled = epoch(account.get('sampled_at'))
        if (account.get('state') == 'ok' and account.get('stale') is not True
                and isinstance(binding, dict) and binding.get('state') == 'verified'
                and sampled and 0 <= now - sampled <= LIVE_QUOTA_VALID_SECONDS):
            merged = {_kimi_window_key(row): row for row in windows}
            for row in account.get('windows') or []:
                if not isinstance(row, dict):
                    continue
                remaining = row.get('remaining_percent')
                reset = epoch(row.get('resets_at'))
                expires = epoch(row.get('expires_at'))
                if (row.get('valid') is not True or row.get('expired') is True
                        or not _finite(remaining) or not 0 <= remaining <= 100
                        or (reset and reset <= now) or (expires and expires <= now)):
                    continue
                merged[_kimi_window_key(row)] = dict(row)
                accepted = True
            windows = list(merged.values())
    return _kimi_order_windows(windows), accepted


def normalize_worker(provider, value):
    value = value or {}
    result = {'id': provider, 'name': PROVIDER_LABELS.get(provider, provider),
              'state': value.get('state', 'unavailable'), 'sampled_at': epoch(value.get('sampled_at')),
              'windows': [], 'balances': []}
    windows = value.get('windows') or []
    kimi_account_current = False
    if provider == 'kimi-for-coding':
        windows, kimi_account_current = _kimi_host_windows(value)
    for window in windows[:4]:
        remaining = window.get('remaining_percent')
        if window.get('valid') and _finite(remaining) and 0 <= remaining <= 100:
            row = {'label': _window_label(window.get('duration_minutes')),
                   'remaining_percent': round(remaining, 3 if provider in (
                       'kimi-for-coding', 'aliyun-personal') else 1),
                   'resets_at': epoch(window.get('resets_at'))}
            if provider == 'kimi-for-coding':
                kind = _kimi_window_key(window)
                if kind in ('short', 'weekly', 'month_total', 'month_code'):
                    row['kind'] = kind
                    row['label'] = {'short': '5小时', 'weekly': '每周',
                                    'month_total': '总额度', 'month_code': '月度Code'}[kind]
                for key in ('name', 'source'):
                    if isinstance(window.get(key), str):
                        row[key] = _bounded(window[key], 64)
                if window.get('expires_at'):
                    row['expires_at'] = epoch(window['expires_at'])
            if window.get('name') == 'Addon' or window.get('addon') is True:
                # Supplemental usage bundles render as their own labelled window.
                row['addon'] = True
                row['label'] = '用量包'
                expiry = epoch(window.get('nearest_expires_at') or window.get('expires_at'))
                if expiry:
                    row['expires_at'] = expiry
            amount, limit = window.get('remaining'), window.get('limit')
            if _finite(amount):
                row['remaining'] = round(amount, 3)
            if _finite(limit):
                row['limit'] = round(limit, 3)
            # A plan window's own unit wins: a Personal percentage is never
            # relabelled as Team Credits. The provider fallback only applies to
            # readings whose window has no explicit unit.
            own_unit = window.get('unit')
            unit = own_unit if isinstance(own_unit, str) and own_unit else WINDOW_UNITS.get(provider)
            if unit:
                row['unit'] = _bounded(unit, 16)
            if window.get('expired') is True:
                row['expired'] = True
            result['windows'].append(row)
    addon = value.get('addon') if isinstance(value.get('addon'), dict) else None
    if addon is not None and (provider == 'aliyun-personal' or
                              value.get('plan_edition') == 'personal'):
        # Addon statistics are display-only metadata: the admission Addon window
        # stays exactly as the quota sampler produced it. An unknown or malformed
        # summary is never shown as zero, and a known-exhausted bundle disappears
        # from the current card while remaining inside the record for admission.
        known = addon.get('known') is True
        # Existing firmware marks an incomplete display row unknown while
        # preserving known monthly values; admission is independent of this row.
        if not known or addon.get('inconsistent') is True:
            result['state'] = 'unknown'
        remaining, limit = addon.get('remaining'), addon.get('limit')
        percent = addon.get('remaining_percent')
        visible = (known and _finite(remaining) and remaining > 0
                   and _finite(percent) and 0 <= percent <= 100)
        projected = any(row.get('addon') is True for row in result['windows'])
        if visible and not projected and len(result['windows']) < 4:
            row = {'label': '用量包', 'remaining_percent': round(percent, 3),
                   'resets_at': 0, 'addon': True, 'remaining': round(remaining, 3)}
            if _finite(limit) and limit > 0:
                row['limit'] = round(limit, 3)
                row['unit'] = 'credits'
            expiry = epoch(addon.get('nearest_expires_at'))
            if expiry:
                row['expires_at'] = expiry
            result['windows'].append(row)
        meta = {key: addon[key] for key in ('known', 'remaining', 'limit', 'used',
                                           'active_count', 'remaining_percent')
                if key in addon}
        expiry = epoch(addon.get('nearest_expires_at'))
        if expiry:
            meta['nearest_expires_at'] = expiry
        if addon.get('inconsistent') is True:
            meta['inconsistent'] = True
        result['addon'] = meta
    for balance in (value.get('balances') or [])[:2]:
        if _finite(balance.get('remaining')):
            result['balances'].append({'currency': _bounded(balance.get('currency'), 8),
                                       'remaining': balance['remaining']})
    if 'available' in value:
        # Unknown availability stays an explicit null instead of being dropped,
        # so a consumer can distinguish unknown from a missing field.
        result['available'] = value.get('available') if isinstance(value.get('available'), bool) else None
    if kimi_account_current and any(
            row.get('kind') in ('month_total', 'month_code') and row.get('remaining_percent') == 0
            for row in result['windows']):
        result['available'] = False
    billing = value.get('billing') if isinstance(value.get('billing'), dict) else None
    reason = billing.get('reason') if billing and isinstance(billing.get('reason'), str) else None
    if billing is not None or value.get('billing_reason') or value.get('monthly_plan_exhausted'):
        reason = reason or (value.get('billing_reason') if isinstance(value.get('billing_reason'), str)
                            else 'monthly_usage_limit' if value.get('monthly_plan_exhausted') else 'billing_error')
        result['billing'] = {'reason': reason, 'monthly_exhausted': reason == 'monthly_usage_limit'}
    snapshot = value.get('snapshot') if isinstance(value.get('snapshot'), dict) else None
    if snapshot is not None:
        # A manually imported reading is never a live balance. The snapshot's own
        # freshness (already reset-expiry aware) is authoritative; age is only a
        # fallback for legacy records, and expiration is propagated explicitly.
        captured = epoch(snapshot.get('captured_at'))
        fresh_for = snapshot.get('fresh_for_minutes')
        if not _finite(fresh_for) or fresh_for <= 0:
            fresh_for = 1440
        freshness = snapshot.get('freshness')
        if freshness not in ('fresh', 'stale'):
            age = max(0.0, time.time() - captured) if captured else None
            freshness = 'fresh' if age is not None and age <= fresh_for * 60 else 'stale'
        expired = snapshot.get('expired') is True or any(
            row.get('expired') is True for row in result['windows'])
        result['snapshot'] = {'captured_at': captured, 'freshness': freshness, 'expired': expired}
        if freshness != 'fresh' or expired:
            result['stale'] = True
        # Absolute freshness deadline: the configured snapshot horizon, never
        # beyond the plan reset. Consumers can honor it instead of assuming a
        # fixed default that would age a 24-hour snapshot out after 15 minutes.
        valid_until = captured + fresh_for * 60 if captured else None
        reset = epoch(snapshot.get('resets_at'))
        if reset is None:
            resets = [epoch(row.get('resets_at')) for row in result['windows']]
            resets = [item for item in resets if item]
            reset = min(resets) if resets else None
        if reset and (valid_until is None or reset < valid_until):
            valid_until = reset
        if valid_until:
            result['valid_until'] = valid_until
    elif _finite(value.get('sampled_at')):
        result['valid_until'] = epoch(value['sampled_at']) + LIVE_QUOTA_VALID_SECONDS
    if value.get('stale') is True:
        result['stale'] = True
    if value.get('source') in ('openapi_live', 'personal_console'):
        result['source'] = value['source']
    error = value.get('live_error')
    if isinstance(error, dict):
        result['stale'] = True
        result['live_error'] = {'state': _bounded(error.get('state') or 'unknown', 24),
                                'attempted_at': epoch(error.get('attempted_at'))}
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
        result['valid_until'] = result['sampled_at'] + LIVE_QUOTA_VALID_SECONDS
    return result


def _cached_plan_edition():
    """Bounded display-only plan edition read; config JSON only, never secrets."""
    now = time.time()
    try:
        import common
        info = common.CONFIG.stat()
        config_stat = (str(common.CONFIG), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError:
        config_stat = None
    if (now - _PLAN_EDITION_CACHE['checked_at'] >= PLAN_EDITION_CACHE_SECONDS or
            _PLAN_EDITION_CACHE.get('config_stat') != config_stat):
        edition = 'team'
        try:
            import bailian_quota
            edition = bailian_quota.plan_edition()
        except Exception:
            edition = 'team'  # fail closed to the legacy single row
        _PLAN_EDITION_CACHE['edition'] = edition if edition in (
            'team', 'personal', 'sequential') else 'team'
        _PLAN_EDITION_CACHE['checked_at'] = now
        _PLAN_EDITION_CACHE['config_stat'] = config_stat
    return _PLAN_EDITION_CACHE['edition']


_PLAN_EDITION_CACHE = {'edition': 'team', 'checked_at': 0.0}


def _is_sequential_record(record):
    """Require current sampler-stamped display provenance, not self-binding."""
    if not isinstance(record, dict) or record.get('plan_edition') != 'sequential':
        return False
    bindings = [record[key] for key in ('_plan_identity', '_credential')
                if isinstance(record.get(key), str) and record.get(key)]
    if not bindings or any(value != bindings[0] for value in bindings):
        return False
    import bailian_quota
    identity = bailian_quota.display_identity()
    if not identity or record.get('_display_identity') != identity:
        return False
    plans = record.get('plan_quotas')
    return isinstance(plans, dict) and all(isinstance(plans.get(edition), dict)
                                           for edition in ('team', 'personal'))


def _sequential_display_record(stored):
    """Resolve the current sequential reading without quota.view or keychain.

    A long-lived
    Team-edition writer can stamp the shared quota.json seconds *after* the
    bound namespace refresh, so candidates are filtered by sequential shape
    and the sampler's current metadata display binding before sample time is
    compared. An old Team record therefore never wins by being newer.
    """
    namespace = read_json(STATE / PLAN_CACHE_FILE, None)
    candidates = [record for record in (stored, namespace) if _is_sequential_record(record)]
    if not candidates:
        return None
    return max(candidates, key=lambda record: (epoch(record.get('sampled_at')) or 0,
                                               epoch(record.get('checked_at')) or 0))


def _sequential_plan_rows(display):
    """One independent row per surviving plan; each keeps its state/freshness.

    A plan whose fresh, complete reading is terminally exhausted (the next
    recovery needs a new purchase) is omitted so the host overview, shared
    board payload and physical display hide it together. The other plan stays
    visible; a null/unknown plan is always retained.
    """
    plans = display.get('plan_quotas') or {}
    active = display.get('active_plan') if isinstance(display.get('active_plan'), str) else None
    rows = []
    for edition in ('team', 'personal'):
        plan = plans.get(edition) or {}
        if allowance_visibility.terminal_exhausted(plan, provider='aliyun-' + edition):
            continue
        row_id, name = SEQUENTIAL_PLAN_ROWS[edition]
        row = normalize_worker(row_id, plan)
        row['name'] = name
        row['plan_edition'] = 'sequential'
        if active in ('team', 'personal'):
            row['plan_slot'] = 'active' if active == edition else 'standby'
        rows.append(row)
    return rows


def _sequential_unknown_rows():
    """No bound reading yet: two honest unknown cards, never the legacy row."""
    rows = []
    for edition in ('team', 'personal'):
        row_id, name = SEQUENTIAL_PLAN_ROWS[edition]
        row = normalize_worker(row_id, {'state': 'unknown', 'available': None})
        row['name'] = name
        row['plan_edition'] = 'sequential'
        rows.append(row)
    return rows


def worker_rows(provider, record):
    """Rows one provider contributes to the snapshot and board quota list.

    normalize_worker keeps its one-row contract for every other caller. Under
    bailian_plan_edition=sequential the Alibaba provider instead expands into
    its Team Credits and Personal allowance rows, taken only from the
    identity-bound namespace record; the legacy provider row is replaced so a
    stale Team-edition writer cannot keep showing 0 Credits as the only plan.
    Current rows whose fresh, complete exhaustion requires a new purchase are
    filtered here, so every host/board consumer sees the same source list.
    """
    if provider == BAILIAN_PROVIDER and _cached_plan_edition() == 'sequential':
        display = _sequential_display_record(record if isinstance(record, dict) else None)
        return _sequential_plan_rows(display) if display is not None else _sequential_unknown_rows()
    if allowance_visibility.terminal_exhausted(record, provider=provider):
        return []
    return [normalize_worker(provider, record)]


def cached_worker_quotas(previous):
    """Read the shared cache cheaply; preserve the separately sampled Codex row.

    This never calls a provider or the Codex CLI. Web snapshots and board
    publication see a worker balance update immediately, even while the slower
    Codex sampler keeps its independent five-minute cadence. Sequential Alibaba
    plans replace whatever row the provider occupied before, so a mode switch
    leaves no stale split or legacy card behind.
    """
    cached = read_json(STATE / 'quota.json', {})
    cached = cached if isinstance(cached, dict) else {}
    rows = {row.get('id'): row for row in previous}
    for provider in WORKER_PROVIDERS:
        if provider not in cached and not (provider == BAILIAN_PROVIDER and
                                           _cached_plan_edition() == 'sequential'):
            continue
        value = cached.get(provider) if isinstance(cached.get(provider), dict) else {}
        new_rows = worker_rows(provider, dict(value, **billing_overlay(provider)))
        for retired in (BAILIAN_ROW_IDS if provider == BAILIAN_PROVIDER else (provider,)):
            rows.pop(retired, None)
        for row in new_rows:
            if row.get('valid_until') and row['valid_until'] <= time.time():
                row['stale'] = True
            rows[row['id']] = row
    return list(rows.values())


def collect_quotas(now=None):
    """Quota snapshot; worker providers from the daemon cache, Codex via CLI."""
    import datetime
    now = now or time.time()
    out = []
    cached = read_json(STATE / 'quota.json', {}) or {}
    for provider in WORKER_PROVIDERS:
        record = cached.get(provider)
        if isinstance(record, dict):
            record = dict(record)
            record.update(billing_overlay(provider))
        out.extend(worker_rows(provider, record))
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
        links = collect_worker_links(now)
        with self.lock:
            previous = json.loads(json.dumps(self.latest_tasks))
        tasks = {}
        for source, acquire in (('codex', lambda: self.codex.collect(now, owner_ids=links)),
                                ('opencode', lambda: collect_opencode_tasks(now))):
            try:
                tasks[source] = acquire()
            except Exception:
                tasks[source] = dict(previous.get(source, {}), state='unavailable', error='collection_failed')
        codex = tasks.get('codex', {})
        for item in codex.get('items', []):
            item['workers'] = links.pop(item['id'], [])
        codex['missing_owners'] = [{'id': owner, 'title': None, 'status': 'unknown', 'workers': jobs}
                                   for owner, jobs in links.items()]
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
        board_task_keys = {'id','title','project','status','model','effort','started_at','ended_at','updated_at','progress'}
        for group in tasks.values():
            group.pop('missing_owners', None)
            visible = [item for item in group.get('items', []) if item.get('_board_visible', True)]
            group['total'] = len(visible)
            group['active'] = sum(item.get('status') != 'completed' for item in visible)
            group['items'] = _sort_tasks([{k: v for k, v in item.items() if k in board_task_keys}
                                         for item in visible])
        quotas = cached_worker_quotas(quotas)
        # v2 firmware matches these exact compact IDs. Keep full provider IDs
        # in the web snapshot/cache; adapt only this independent board copy.
        # The Alibaba Token Plan row is additive metadata (windows, state and
        # bounded snapshot freshness); consumers that do not know the alias
        # simply ignore the extra row.
        board_ids = {'kimi-for-coding': 'kimi', 'volcengine-agent-plan': 'ark',
                     'bailian-token-plan': 'aliyun'}
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
        for group in tasks.values():
            for item in group.get('items', []):
                item.pop('_board_visible', None)
            group['stale'] = (group.get('state') != 'ok' or
                              now - group.get('sampled_at', 0) > TASK_INTERVAL_SECONDS * 4)
        quotas = cached_worker_quotas(quotas)
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
