"""Small, dependency-free host telemetry for the authenticated local console."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

_CACHE = {'at': 0.0, 'value': None}


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=1,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _cpu_percent():
    values = []
    for line in _run(['ps', '-A', '-o', '%cpu=']).splitlines():
        try:
            values.append(float(line.strip().replace(',', '.')))
        except ValueError:
            pass
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
        return {'total_bytes': None, 'available_bytes': None, 'used_percent': None}
    used = max(0, total - available)
    return {'total_bytes': total, 'available_bytes': available,
            'used_percent': max(0.0, min(100.0, used / total * 100))}


def snapshot(now=None):
    now = time.time() if now is None else now
    # The console's legacy task page consumes the same host sample as the home.
    try:
        import host_telemetry
        collector = host_telemetry.current_collector()
        sample = collector.snapshot(now)['system'] if collector else None
        if sample:
            disk = next((d for d in sample.get('disks', []) if d.get('internal')), None)
            total = sample.get('memory_total_gb')
            used = sample.get('memory_used_gb')
            return {'sampled_at': sample['sampled_at'], 'cpu_percent':sample.get('cpu_percent'),
                    'load_average':sample.get('load_average', []),
                    'memory':{'total_bytes':total * 1024**3 if total is not None else None,
                              'available_bytes':max(0,total-used)*1024**3 if total is not None and used is not None else None,
                              'used_percent':sample.get('memory_percent')},
                    'disk':{'total_bytes':disk['total_gb']*1024**3 if disk else None,
                            'free_bytes':disk['free_gb']*1024**3 if disk else None,
                            'used_percent':disk.get('used_percent') if disk else None}}
    except (ImportError, KeyError, TypeError):
        pass
    if _CACHE['value'] is not None and now - _CACHE['at'] < 5:
        return dict(_CACHE['value'])
    disk = shutil.disk_usage(Path.home())
    value = {'sampled_at': now, 'cpu_percent': _cpu_percent(), 'memory': _memory(),
             'disk': {'total_bytes': disk.total, 'free_bytes': disk.free,
                      'used_percent': disk.used / disk.total * 100 if disk.total else None},
             'load_average': list(os.getloadavg()) if hasattr(os, 'getloadavg') else []}
    _CACHE.update(at=now, value=value)
    return dict(value)
