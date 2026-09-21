"""Compact, expiring accounting records for task rows removed from the console.

The ledger excludes objectives, prompts, messages, file contents and credentials. It keeps
only routing/timing/status metadata plus the final provider-reported usage snapshot so
debugging and plan-cost analysis remain continuous after a task row is cleared.
"""
import math
import time

import common


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _path(task_id):
    common.task_path(task_id)  # validate the identifier
    return common.STATE / 'usage-ledger' / (task_id + '.json')


def record(task, retention_days, now=None):
    """Persist one redacted, compact accounting record before a task row is removed."""
    if not isinstance(task, dict) or not isinstance(task.get('id'), str):
        raise ValueError('Invalid task record')
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError('Invalid usage retention period')
    import task_activity
    now = time.time() if now is None else now
    usage = task_activity.snapshot(task).get('usage')
    if not isinstance(usage, dict):
        usage = task_activity.empty_usage('none')
    keep = ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total', 'cost',
            'source', 'complete')
    entry = {
        'task_id': task['id'], 'title': task.get('title'), 'status': task.get('status'),
        'tier': task.get('tier'), 'profile': task.get('profile'),
        'actual_models': task.get('actual_models') if isinstance(task.get('actual_models'), list) else [],
        'route_history': task.get('route_history') if isinstance(task.get('route_history'), list) else [],
        'fallback_used': task.get('fallback_used') is True,
        'group_id': task.get('group_id'), 'group_title': task.get('group_title'),
        'parent_task_id': task.get('parent_task_id'), 'owner_thread_id': task.get('owner_thread_id'),
        'created_at': _number(task.get('created_at')), 'started_at': _number(task.get('started_at')),
        'finished_at': _number(task.get('finished_at')),
        'elapsed_seconds': _number(task.get('elapsed_seconds')),
        'cancellation': task.get('cancellation') if isinstance(task.get('cancellation'), dict) else None,
        'usage': {key: usage.get(key) for key in keep},
        'deleted_at': now, 'expires_at': now + retention_days * 86400,
    }
    common.write_json(_path(task['id']), entry)
    return entry


def entries(now=None, include_expired=False):
    """Read valid ledger entries; malformed or symlinked files are ignored."""
    common.init()
    now = time.time() if now is None else now
    found = []
    for path in (common.STATE / 'usage-ledger').glob('job-*.json'):
        if path.is_symlink() or not path.is_file():
            continue
        value = common.read_json(path)
        if not isinstance(value, dict) or value.get('task_id') != path.stem:
            continue
        expires = _number(value.get('expires_at'))
        if expires is None or (not include_expired and expires <= now):
            continue
        found.append(value)
    return sorted(found, key=lambda item: item.get('deleted_at') or 0, reverse=True)


def prune_expired(now=None):
    """Remove only ledger files whose own recorded expiry time has passed."""
    common.init()
    now = time.time() if now is None else now
    removed = []
    for entry in entries(now=now, include_expired=True):
        expires = _number(entry.get('expires_at'))
        if expires is None or expires > now:
            continue
        path = _path(entry['task_id'])
        if path.is_symlink() or not path.is_file():
            continue
        path.unlink()
        removed.append(entry['task_id'])
    return removed


def summary(now=None, include_entries=False, limit=500):
    """Return honest aggregates, with bounded detail only when explicitly requested.

    The main console state stays small even after months of task cleanup. Debug or
    accounting callers can request recent records without loading an unbounded ledger.
    """
    current = entries(now=now)
    total = 0
    cost = 0.0
    known_tokens = known_cost = 0
    for entry in current:
        usage = entry.get('usage') if isinstance(entry.get('usage'), dict) else {}
        value = _number(usage.get('total'))
        if value is not None:
            total += value; known_tokens += 1
        value = _number(usage.get('cost'))
        if value is not None:
            cost += value; known_cost += 1
    result = {'count': len(current),
              'total_tokens': total if known_tokens else None, 'known_token_count': known_tokens,
              'provider_cost': cost if known_cost else None, 'known_cost_count': known_cost}
    if include_entries:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError('Invalid usage ledger limit')
        result.update(entries=current[:limit], truncated=len(current) > limit)
    return result
