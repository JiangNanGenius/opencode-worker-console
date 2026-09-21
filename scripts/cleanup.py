#!/usr/bin/env python3
"""Low-disk cleanup of disposable delegate runtime data.

The pool can live on a small machine, so the operator authorizes the agent to
remove regenerable data automatically when free disk is low. The module is
deliberately conservative:

* The default policy is disabled. Removal only runs when the configured
  ``cleanup.enabled`` is true or the operator passes an explicit ``--force``.
* Only data under ``common.STATE`` (our own runtime) and OpenCode sessions
  linked to our own terminal tasks are ever touched. An isolated worktree is
  released only after integration or when it is proven unchanged.
* Every local path is refused when it is a symlink, resolves outside STATE, or
  names a shared container directory.
* Sessions are deleted through ``management.update_session`` so its
  descendant/active safeguards still apply; a failed request is reported with a
  concise redacted error and never fabricated as success.

Nothing here reads or serializes task objectives, prompts or message bodies.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import time

import common
import management
import workspace

GiB = 1024 ** 3

DEFAULTS = {
    'enabled': False,
    'min_free_gb': 5.0,
    'target_free_gb': 10.0,
    'min_age_days': 30,
    'keep_recent': 20,
    'usage_retention_days': 365,
    'interval_seconds': 3600,
}

# Shared container directories that must never be removed wholesale.
_FORBIDDEN = {('tasks',), ('artifacts',), ('logs',), ('releases',), ('worktrees',)}
_DIR_KINDS = {'pycache', 'release_backup', 'artifact_before', 'artifact_after'}
_API_FAILURES = (common.HttpFailure, OSError, RuntimeError, ValueError)
_SECRET = re.compile(r'sk-[A-Za-z0-9_-]{12,}')


def policy():
    """Return the effective cleanup policy merged over conservative defaults."""
    try:
        cfg = common.config()
    except Exception:
        cfg = {}
    raw = cfg.get('cleanup') if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw.get('enabled'), bool):
        out['enabled'] = raw['enabled']
    for key in ('min_free_gb', 'target_free_gb'):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = float(value)
    for key in ('min_age_days', 'keep_recent', 'usage_retention_days'):
        value = raw.get(key)
        minimum = 1 if key == 'usage_retention_days' else 0
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
            out[key] = value
    value = raw.get('interval_seconds')
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        out['interval_seconds'] = value
    return out


def periodic():
    """Hourly maintenance: expire compact usage records, then apply low-disk policy."""
    import usage_ledger
    expired = usage_ledger.prune_expired()
    result = run(apply=True) if policy()['enabled'] else {'applied': False, 'skipped': 'disabled'}
    result['expired_usage_records'] = expired
    return result


def free_bytes():
    """Free bytes on the filesystem that backs the private state directory."""
    return shutil.disk_usage(str(common.STATE)).free


def _is_low(pol, free):
    return free < pol['min_free_gb'] * GiB


# -- eligibility ---------------------------------------------------------

def _task_age_days(t, now):
    stamp = max(t.get('created_at') or 0, t.get('finished_at') or 0)
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        stamp = t.get('finished_at') or 0
    return (now - float(stamp)) / 86400.0


def eligible_tasks(pol=None, now=None):
    """Return reclaimable terminal tasks.

    Needs-attention evidence becomes reclaimable after 24 idle hours, or as soon
    as the same Codex owner has produced a newer task. Its original status stays
    intact for the usage ledger. Other terminal work keeps the configured age and
    recent-task retention policy.
    """
    pol = policy() if pol is None else pol
    now = time.time() if now is None else now
    ordered = sorted((t for t in common.tasks() if isinstance(t, dict)),
                     key=lambda t: t.get('created_at') or 0, reverse=True)
    retained = {t.get('id') for t in ordered[:max(0, pol['keep_recent'])]}
    newest_by_owner = {}
    for item in ordered:
        owner = item.get('owner_thread_id') or item.get('group_id')
        created = item.get('created_at') or 0
        if owner and owner not in newest_by_owner:
            newest_by_owner[owner] = created
    eligible = {}
    for t in ordered:
        tid = t.get('id')
        if not tid:
            continue
        status = t.get('status')
        if status in common.ACTIVE or status == 'queued':
            continue
        if status not in common.TERMINAL:
            continue
        owner = t.get('owner_thread_id') or t.get('group_id')
        superseded_attention = status == 'needs_attention' and owner and \
            newest_by_owner.get(owner, 0) > (t.get('created_at') or 0)
        stale_attention = status == 'needs_attention' and _task_age_days(t, now) >= 1
        attention_ready = superseded_attention or stale_attention
        if tid in retained and not attention_ready:
            continue
        if not attention_ready and _task_age_days(t, now) < pol['min_age_days']:
            continue
        eligible[tid] = t
    return eligible


# -- size helpers --------------------------------------------------------

def _file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_size(path):
    total = 0
    for base, dirs, files in os.walk(str(path), followlinks=False):
        for name in files:
            p = Path(base) / name
            try:
                if not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    return total


def _old(path, now, age_seconds):
    try:
        return (now - path.stat().st_mtime) >= age_seconds
    except OSError:
        return False


# -- candidate discovery -------------------------------------------------

def _pycache_dirs():
    found = []
    root = common.STATE
    for base, dirs, files in os.walk(str(root), followlinks=False):
        kept = []
        if Path(base) == root:
            dirs[:] = [d for d in dirs if d not in ('worktrees', 'artifacts', 'tasks', 'runtime')]
        for name in dirs:
            candidate = Path(base) / name
            if candidate.is_symlink():
                continue
            if name == '__pycache__':
                found.append(candidate)
            else:
                kept.append(name)
        dirs[:] = kept
    return found


def _cache_items(pol, now):
    items = []
    age_seconds = pol['min_age_days'] * 86400
    for p in _pycache_dirs():
        if not _old(p, now, age_seconds):
            continue
        items.append({'kind': 'pycache', 'path': str(p), 'bytes': _dir_size(p)})
    logs = common.STATE / 'logs'
    if logs.is_dir():
        for p in sorted(logs.glob('*.previous.log')):
            if p.is_symlink():
                continue
            if _old(p, now, age_seconds):
                items.append({'kind': 'previous_log', 'path': str(p), 'bytes': _file_size(p)})
    releases = common.STATE / 'releases'
    if releases.is_dir():
        backups = sorted((p for p in releases.iterdir() if p.is_dir() and not p.is_symlink()),
                         key=lambda p: p.name, reverse=True)
        for p in backups[2:]:
            if _old(p, now, age_seconds):
                items.append({'kind': 'release_backup', 'path': str(p), 'bytes': _dir_size(p)})
    return items


def _artifact_items(task_id):
    items = []
    art = common.STATE / 'artifacts' / task_id
    for name in ('before', 'after'):
        p = art / name
        if p.is_dir() and not p.is_symlink():
            items.append({'kind': 'artifact_' + name, 'task_id': task_id, 'path': str(p), 'bytes': _dir_size(p)})
    messages = art / 'messages.json'
    if messages.is_file() and not messages.is_symlink():
        items.append({'kind': 'artifact_messages', 'task_id': task_id, 'path': str(messages),
                      'bytes': _file_size(messages)})
    result = art / 'result.json'
    if result.is_file() and not result.is_symlink():
        items.append({'kind': 'artifact_result', 'task_id': task_id, 'path': str(result),
                      'bytes': _file_size(result)})
    return items


def _worktree_item(task):
    status = workspace.isolated_release_status(task)
    if not status.get('eligible'):
        return None
    return {'kind': 'worktree', 'task_id': task['id'], 'path': status['path'],
            'bytes': status.get('bytes', 0)}


def _status_type(value):
    if isinstance(value, dict):
        kind = value.get('type')
        return kind if isinstance(kind, str) else 'idle'
    return value if isinstance(value, str) else 'idle'


def _session_statuses(raw):
    directories = sorted({s['directory'] for s in raw
                          if isinstance(s, dict) and isinstance(s.get('directory'), str) and s['directory']})
    merged = {}
    try:
        status = common.api('/session/status')
    except _API_FAILURES:
        status = None
    if isinstance(status, dict):
        merged.update(status)
    for directory in directories:
        try:
            extra = common.api('/session/status', directory=directory)
        except _API_FAILURES:
            continue
        if isinstance(extra, dict):
            merged.update(extra)
    return merged


def _session_items(eligible, pol, now):
    linked = {}
    for t in eligible.values():
        sid = t.get('session_id')
        if isinstance(sid, str) and sid:
            linked[sid] = t.get('id')
    if not linked:
        return []
    raw = common.api('/experimental/session?limit=' + str(management.SESSION_LIMIT) + '&archived=true')
    if isinstance(raw, dict):
        raw = raw.get('sessions') if isinstance(raw.get('sessions'), list) else raw.get('data')
    if not isinstance(raw, list):
        return []
    statuses = _session_statuses(raw)
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = item.get('id')
        if not isinstance(sid, str) or sid not in linked:
            continue
        time_value = item.get('time')
        archived = time_value.get('archived') if isinstance(time_value, dict) else None
        if isinstance(archived, bool) or not isinstance(archived, (int, float)) or archived <= 0:
            continue
        if now - (archived / 1000.0) < pol['min_age_days'] * 86400:
            continue
        if _status_type(statuses.get(sid)) != 'idle':
            continue
        items.append({'kind': 'session', 'session_id': sid, 'task_id': linked[sid], 'bytes': 0})
    return items


def _plan(pol, now):
    """Return (ordered candidates, discovery errors)."""
    items = list(_cache_items(pol, now))
    eligible = eligible_tasks(pol, now)
    for task_id in sorted(eligible):
        items.extend(_artifact_items(task_id))
        worktree = _worktree_item(eligible[task_id])
        if worktree:
            items.append(worktree)
    errors = []
    try:
        items.extend(_session_items(eligible, pol, now))
    except _API_FAILURES as e:
        errors.append({'kind': 'sessions', 'target': 'list', 'error': _brief(e)})
    return items, errors


# -- safety --------------------------------------------------------------

def _assert_under_state(path):
    root = common.STATE.resolve()
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise ValueError('refusing path outside state') from None
    parts = rel.parts
    if not parts:
        raise ValueError('refusing to remove state root')
    if parts in _FORBIDDEN or (parts[0] == 'artifacts' and len(parts) == 2):
        raise ValueError('refusing to remove shared state directory')
    return rel


def _has_symlink(path):
    for base, dirs, files in os.walk(str(path), followlinks=False):
        for name in dirs + files:
            if (Path(base) / name).is_symlink():
                return True
    return False


def _remove_local(item):
    path = Path(item['path'])
    if path.is_symlink():
        raise ValueError('refusing symlink target')
    rel = _assert_under_state(path)
    if item['kind'] in _DIR_KINDS:
        if not path.is_dir():
            raise FileNotFoundError(str(rel))
        if _has_symlink(path):
            raise ValueError('refusing tree containing symlinks')
        shutil.rmtree(str(path))
    else:
        if not path.is_file():
            raise FileNotFoundError(str(rel))
        path.unlink()


def _delete_session(item):
    """Delete one linked archive through management so its guards still apply."""
    sid = item['session_id']
    try:
        raw = common.api('/session/' + sid)
    except common.HttpFailure as e:
        if e.status == 404:
            return 'missing'
        raise
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
        raise ValueError('Unknown session')
    pol = policy()
    eligible = eligible_tasks(pol)
    linked = {t.get('session_id') for t in eligible.values()}
    pending, seen = [raw], set()
    while pending:
        current = pending.pop()
        cid = current.get('id')
        if cid in seen:
            continue
        seen.add(cid)
        archived = current.get('time', {}).get('archived', 0)
        if cid not in linked or not archived or time.time() - archived / 1000 < pol['min_age_days'] * 86400:
            raise ValueError('Session or descendant is outside cleanup retention policy')
        children = common.api('/session/' + cid + '/children', directory=current.get('directory'))
        if not isinstance(children, list) or len(seen) > 500:
            raise ValueError('Cannot verify archived descendants')
        pending.extend(children)
    title = raw.get('title')
    if not isinstance(title, str):
        raise ValueError('Missing session title')
    management.update_session(sid, {'action': 'delete', 'confirm_session_id': sid, 'confirm_title': title})
    return 'deleted'


# -- reporting -----------------------------------------------------------

def _brief(exc):
    text = common.redact((type(exc).__name__ + ': ' + str(exc))[:240])
    return _SECRET.sub('<redacted>', text)


def _rel_display(path):
    try:
        return str(Path(path).resolve().relative_to(common.STATE.resolve()))
    except ValueError:
        return str(path)


def _target(item):
    if 'session_id' in item:
        return item['session_id']
    return _rel_display(item['path'])


def _public_item(item):
    out = {'kind': item['kind'], 'bytes': item.get('bytes', 0)}
    if 'path' in item:
        out['path'] = _rel_display(item['path'])
    if 'task_id' in item:
        out['task_id'] = item['task_id']
    if 'session_id' in item:
        out['session_id'] = item['session_id']
    return out


def _write_report(report):
    try:
        common.write_json(common.STATE / 'cleanup-last.json', report)
    except OSError:
        pass


def preview():
    """Return the candidate plan with sizes without removing anything."""
    pol = policy()
    now = time.time()
    free = free_bytes()
    items, errors = _plan(pol, now)
    public = [_public_item(i) for i in items]
    return {
        'dry_run': True,
        'policy': pol,
        'free_bytes': free,
        'low_disk': _is_low(pol, free),
        'candidates': public,
        'candidate_count': len(public),
        'candidate_bytes': sum(i.get('bytes', 0) for i in public),
        'errors': errors,
    }


def _apply(items, pol, force):
    target = pol['target_free_gb'] * GiB
    removed, skipped, errors = [], [], []
    stopped = False
    for item in items:
        if not force and free_bytes() >= target:
            stopped = True
            break
        try:
            if item['kind'] == 'session':
                if _delete_session(item) == 'missing':
                    skipped.append({'kind': item['kind'], 'target': _target(item), 'reason': 'missing'})
                    continue
            elif item['kind'] == 'worktree':
                released = workspace.release_isolated(common.task(item['task_id']))
                if not released.get('released'):
                    skipped.append({'kind': item['kind'], 'target': _target(item),
                                    'reason': released.get('reason', 'not_releasable')})
                    continue
            else:
                _remove_local(item)
            removed.append({'kind': item['kind'], 'target': _target(item), 'bytes': item.get('bytes', 0)})
        except (ValueError, OSError, RuntimeError, common.HttpFailure) as e:
            errors.append({'kind': item['kind'], 'target': _target(item), 'error': _brief(e)})
    return removed, skipped, errors, stopped


def run(apply=False, force=False):
    """Preview by default; remove only when authorized and disk is low."""
    pol = policy()
    now = time.time()
    report = {
        'time': now,
        'dry_run': not apply,
        'applied': False,
        'skipped': None,
        'force': bool(force),
        'policy': pol,
        'free_bytes': free_bytes(),
        'low_disk': False,
        'candidates': [],
        'candidate_count': 0,
        'candidate_bytes': 0,
        'removed': [],
        'skipped_items': [],
        'errors': [],
        'freed_bytes': 0,
        'stopped_at_target': False,
    }
    if not apply:
        free = report['free_bytes']
        report['low_disk'] = _is_low(pol, free)
        items, errors = _plan(pol, now)
        public = [_public_item(i) for i in items]
        report['candidates'] = public
        report['candidate_count'] = len(public)
        report['candidate_bytes'] = sum(i.get('bytes', 0) for i in public)
        report['errors'] = errors
        _write_report(report)
        return report
    with common.locked('cleanup'):
        free = free_bytes()
        low = _is_low(pol, free)
        report['free_bytes'] = free
        report['low_disk'] = low
        items, errors = _plan(pol, now)
        public = [_public_item(i) for i in items]
        report['candidates'] = public
        report['candidate_count'] = len(public)
        report['candidate_bytes'] = sum(i.get('bytes', 0) for i in public)
        report['errors'] = list(errors)
        if not (pol['enabled'] or force):
            report['skipped'] = 'disabled'
        elif not (low or force):
            report['skipped'] = 'disk_ok'
        else:
            removed, skipped, apply_errors, stopped = _apply(items, pol, force)
            report['removed'] = removed
            report['skipped_items'] = skipped
            report['errors'].extend(apply_errors)
            report['freed_bytes'] = sum(r.get('bytes', 0) for r in removed)
            report['stopped_at_target'] = stopped
            report['applied'] = True
    report['free_bytes_after'] = free_bytes()
    report['freed_bytes_is_estimate'] = True
    _write_report(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='remove the listed candidates')
    parser.add_argument('--force', action='store_true',
                        help='explicit operator authorization: apply even when the policy is disabled or disk is OK')
    args = parser.parse_args(argv)
    result = run(apply=args.apply or args.force, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    main()
