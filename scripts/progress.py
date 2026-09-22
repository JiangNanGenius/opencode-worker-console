"""Durable, bounded progress reports for long builds, tests, deploys and CI runs."""
import datetime
import json
import math
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import common

STATUSES = {'running', 'queued', 'completed', 'failed', 'cancelled'}


def _path(task_id):
    common.task(task_id)
    return common.STATE / 'progress' / (task_id + '.json')


def _github_target(url):
    """Return (repository, run id) for a canonical GitHub Actions run URL."""
    try:
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split('/') if part]
    except (TypeError, ValueError):
        return None
    if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or len(parts) != 5 or
            parts[2:4] != ['actions', 'runs'] or not parts[4].isdigit()):
        return None
    return parts[0] + '/' + parts[1], parts[4]


def _github_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def report(task_id, phase, message='', current=None, total=None, percent=None,
           eta_seconds=None, status='running', github_run=None):
    if status not in STATUSES: raise ValueError('Invalid progress status')
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 160:
        raise ValueError('Progress phase must contain 1-160 characters')
    if not isinstance(message, str) or len(message) > 1000:
        raise ValueError('Progress message must be at most 1000 characters')
    for name, value in (('current', current), ('total', total), ('percent', percent),
                        ('eta_seconds', eta_seconds)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError(name + ' must be a non-negative number')
    if total is not None and total <= 0: raise ValueError('total must be positive')
    if current is not None and total is not None:
        percent = min(100, current / total * 100)
    if percent is not None and percent > 100: raise ValueError('percent must not exceed 100')
    if github_run is not None and (not isinstance(github_run, str) or len(github_run) > 500 or
                                   _github_target(github_run) is None):
        raise ValueError('github_run must be a canonical GitHub Actions run URL')
    now = time.time()
    path = _path(task_id)
    saved = common.read_json(path, {}) or {}
    started = saved.get('started_at') or now
    if eta_seconds is None and percent and percent > 0 and status == 'running':
        eta_seconds = max(0, (now - started) * (100 - percent) / percent)
    event = {'time': now, 'phase': phase.strip(), 'message': message.strip(),
             'status': status, 'percent': round(percent, 2) if percent is not None else None,
             'eta_seconds': round(eta_seconds) if eta_seconds is not None else None}
    value = {'task_id': task_id, 'started_at': started, 'updated_at': now,
             **event, 'current': current, 'total': total,
             'github_run': github_run or saved.get('github_run'),
             'history': (saved.get('history') or [])[-49:] + [event]}
    common.write_json(path, value)
    return value


def _github(value):
    url = value.get('github_run')
    now = time.time()
    if not url or now - value.get('github_checked_at', 0) < 20:
        return value
    try:
        repository, run_id = _github_target(url)
        run = subprocess.run(['gh', 'run', 'view', run_id, '--repo', repository, '--json',
                              'name,status,conclusion,jobs,startedAt,updatedAt,url'],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=10)
        info = json.loads(run.stdout)
        jobs = info.get('jobs') or []
        steps = [step for job in jobs for step in (job.get('steps') or [])]
        done = sum(step.get('status') == 'completed' for step in steps)
        total = len(steps)
        terminal = info.get('status') == 'completed'
        conclusion = (info.get('conclusion') or '').lower()
        status = ('completed' if conclusion == 'success' else
                  'cancelled' if conclusion == 'cancelled' else
                  'failed') if terminal else 'running'
        percent = done / total * 100 if total else (100 if terminal else None)
        active_step = next((step.get('name') for step in steps
                            if step.get('status') not in ('completed', 'skipped') and step.get('name')), None)
        active_job = next((job.get('name') for job in jobs
                           if job.get('status') != 'completed' and job.get('name')), None)
        phase = active_step or active_job or info.get('name') or value.get('phase')
        message = ((str(done) + '/' + str(total) + ' steps') if total and not terminal else
                   conclusion or info.get('status') or value.get('message'))
        value.update(phase=phase, status=status,
                     current=done or None, total=total or None,
                     percent=round(percent, 2) if percent is not None else None,
                     message=message, github_checked_at=now,
                     github_state='live', github_error=None)
        started_at = _github_timestamp(info.get('startedAt')) or value['started_at']
        if status == 'running' and percent and percent < 100:
            value['eta_seconds'] = round(max(0, (now - started_at) *
                                                (100 - percent) / percent))
        else:
            value['eta_seconds'] = 0 if terminal else None
        common.write_json(_path(value['task_id']), value)
    except Exception:
        value.update(github_checked_at=now, github_state='unavailable',
                     github_error='GitHub Actions progress is temporarily unavailable')
        common.write_json(_path(value['task_id']), value)
    return value


def snapshot(task_id, refresh_github=True):
    try: value = common.read_json(_path(task_id), {}) or {}
    except ValueError: return None
    if not value: return None
    return _github(value) if refresh_github else value


def finish(task_id, status, message=''):
    saved = snapshot(task_id, False) or {}
    complete = status == 'completed'
    return report(task_id, saved.get('phase', 'Finished'), message or saved.get('message', ''),
                  current=saved.get('total') if complete else saved.get('current'),
                  total=saved.get('total'), percent=100 if complete else saved.get('percent'),
                  eta_seconds=0 if complete else None, status=status,
                  github_run=saved.get('github_run'))
