"""Redacted operational analytics for the Worker Desk console.

The response contains task/routing/usage metadata only. Prompts, messages, file
contents, credential identities and raw provider payloads never enter this view.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
import time

import common
import task_activity
import usage_ledger


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def _usage(value):
    value = value if isinstance(value, dict) else {}
    return {key: _number(value.get(key)) for key in
            ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total', 'cost')}


def _task_record(task):
    usage = _usage(task_activity.usage_for_task(task))
    return {'task_id': task.get('id'), 'status': task.get('status') or 'unknown',
            'tier': task.get('tier') or 'unknown', 'profile': task.get('profile') or 'unknown',
            'actual_models': task.get('actual_models') if isinstance(task.get('actual_models'), list) else [],
            'fallback_used': task.get('fallback_used') is True,
            'created_at': _number(task.get('created_at')), 'finished_at': _number(task.get('finished_at')),
            'usage': usage, 'source': 'current'}


def _ledger_record(entry):
    return {'task_id': entry.get('task_id'), 'status': entry.get('status') or 'unknown',
            'tier': entry.get('tier') or 'unknown', 'profile': entry.get('profile') or 'unknown',
            'actual_models': entry.get('actual_models') if isinstance(entry.get('actual_models'), list) else [],
            'fallback_used': entry.get('fallback_used') is True,
            'created_at': _number(entry.get('created_at')), 'finished_at': _number(entry.get('finished_at')),
            'usage': _usage(entry.get('usage')), 'source': 'ledger'}


def _model(record, profiles):
    actual = [item for item in record.get('actual_models', []) if isinstance(item, str) and item]
    if actual:
        return actual[-1]
    profile = profiles.get(record.get('profile')) if isinstance(profiles, dict) else None
    return profile.get('model') if isinstance(profile, dict) and isinstance(profile.get('model'), str) \
        else record.get('profile') or 'unknown'


def _breakdown(records, key):
    groups = defaultdict(lambda: {'tasks': 0, 'tokens': 0, 'known_tokens': 0})
    for record in records:
        name = str(record.get(key) or 'unknown')
        row = groups[name]
        row['tasks'] += 1
        total = record['usage'].get('total')
        if total is not None:
            row['tokens'] += total
            row['known_tokens'] += 1
    return [dict(name=name, **values) for name, values in
            sorted(groups.items(), key=lambda item: (-item[1]['tokens'], -item[1]['tasks'], item[0]))]


def _daily(records, now):
    today = datetime.fromtimestamp(now, timezone.utc).date()
    days = {today - timedelta(days=offset): {'tasks': 0, 'tokens': 0, 'known_tokens': 0}
            for offset in range(29, -1, -1)}
    for record in records:
        stamp = record.get('finished_at') or record.get('created_at')
        if stamp is None:
            continue
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        if day not in days:
            continue
        days[day]['tasks'] += 1
        total = record['usage'].get('total')
        if total is not None:
            days[day]['tokens'] += total
            days[day]['known_tokens'] += 1
    return [dict(date=day.isoformat(), **values) for day, values in days.items()]


def _quota_history():
    raw = common.read_json(common.STATE / 'quota-history.json', {})
    result = {}
    if not isinstance(raw, dict):
        return result
    for provider, record in raw.items():
        if not isinstance(provider, str) or not isinstance(record, dict):
            continue
        clean = []
        for sample in record.get('samples', [])[-240:]:
            if not isinstance(sample, dict) or _number(sample.get('time')) is None:
                continue
            windows = {}
            for name, value in (sample.get('windows') or {}).items():
                percent = _number(value.get('remaining_percent')) if isinstance(value, dict) else None
                if isinstance(name, str) and percent is not None:
                    windows[name] = percent
            balances = {currency: amount for currency, amount in (sample.get('balances') or {}).items()
                        if isinstance(currency, str) and _number(amount) is not None}
            clean.append({'time': sample['time'], 'windows': windows, 'balances': balances})
        if clean:
            result[provider] = clean
    return result


def summary(now=None):
    now = time.time() if now is None else now
    current = [_task_record(task) for task in common.tasks()]
    current_ids = {record['task_id'] for record in current}
    retained = [_ledger_record(entry) for entry in usage_ledger.entries(now=now)
                if entry.get('task_id') not in current_ids]
    records = current + retained
    profiles = common.config().get('profiles', {})
    for record in records:
        record['model'] = _model(record, profiles)
    known = [record['usage']['total'] for record in records if record['usage']['total'] is not None]
    costs = [record['usage']['cost'] for record in records if record['usage']['cost'] is not None]
    active = {'queued', 'starting', 'running', 'cancelling', 'uncertain'}
    return {
        'generated_at': now,
        'retention_days': common.config().get('cleanup', {}).get('usage_retention_days', 365),
        'totals': {'tasks': len(records), 'active': sum(record['status'] in active for record in current),
                   'retained': len(retained), 'tokens': sum(known) if known else None,
                   'known_tokens': len(known), 'provider_cost': sum(costs) if costs else None,
                   'known_cost': len(costs),
                   'fallbacks': sum(record['fallback_used'] for record in records)},
        'by_tier': _breakdown(records, 'tier'),
        'by_model': _breakdown(records, 'model'),
        'by_profile': _breakdown(records, 'profile'),
        'by_status': _breakdown(records, 'status'),
        'daily': _daily(records, now),
        'quota_history': _quota_history(),
    }
