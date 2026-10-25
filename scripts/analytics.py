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
    result = {key: _number(value.get(key)) for key in
              ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total', 'cost')}
    if isinstance(value.get('by_model'), list):
        result['by_model'] = [{'model': item['model'], 'total': _number(item.get('total'))}
                              for item in value['by_model'] if isinstance(item, dict) and
                              isinstance(item.get('model'), str)]
    return result


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
    if len(set(actual)) > 1:
        return 'mixed / unattributed'
    if actual:
        return actual[0]
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


def _models(records):
    parts = []
    for record in records:
        segments = record['usage'].get('by_model')
        if not segments:
            parts.append(record)
            continue
        known = sum(item['total'] for item in segments if item['total'] is not None)
        total = record['usage'].get('total')
        if total is not None and known > total:
            parts.append(dict(record, model='mixed / unattributed'))
            continue
        for item in segments:
            parts.append(dict(record, model=item['model'], usage={'total': item['total']}))
        if total is not None and known < total:
            parts.append(dict(record, model='mixed / unattributed', usage={'total': total - known}))
    return _breakdown(parts, 'model')


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


def _timeline(records, now, seconds, buckets):
    """Bucket task completion activity without exposing prompt or file content."""
    step = seconds / buckets
    start = now - seconds
    rows = [{'start': start + index * step, 'end': start + (index + 1) * step,
             'tasks': 0, 'tokens': 0, 'known_tokens': 0}
            for index in range(buckets)]
    for record in records:
        stamp = record.get('finished_at') or record.get('created_at')
        if stamp is None or stamp < start or stamp > now:
            continue
        index = min(buckets - 1, max(0, int((stamp - start) / step)))
        rows[index]['tasks'] += 1
        total = record['usage'].get('total')
        if total is not None:
            rows[index]['tokens'] += total
            rows[index]['known_tokens'] += 1
    return rows


def usage_report(now=None, window_hours=(1, 24, 48)):
    """Per-model observed tokens and estimated Credits for rolling windows.

    Precise windows come from timestamped per-response usage events (assistant
    completion time, model, disjoint counters, deduplicated by message ID).
    Records without events fall back to coarse lifetime attribution and are
    reported separately, so they never contaminate the timestamped headline.
    Alibaba Token Plan Credits are estimated with the configured coefficients
    and always labeled as estimates; no billed total is invented.
    """
    import economics
    now = time.time() if now is None else now
    profiles = common.config().get('profiles', {})
    records = []
    for task in common.tasks():
        task_id = task.get('id')
        records.append({'task_id': task_id, 'status': task.get('status') or 'unknown',
                        'profile': task.get('profile'),
                        'created_at': _number(task.get('created_at')),
                        'finished_at': _number(task.get('finished_at')),
                        'fallback_used': task.get('fallback_used') is True,
                        'events': usage_ledger.events_for_task(task_id, now=now),
                        'usage': task_activity.usage_for_task(task)})
    current_ids = {row['task_id'] for row in records}
    for entry in usage_ledger.entries(now=now):
        if entry.get('task_id') in current_ids:
            continue
        records.append({'task_id': entry.get('task_id'), 'status': entry.get('status') or 'unknown',
                        'profile': entry.get('profile'),
                        'created_at': _number(entry.get('created_at')),
                        'finished_at': _number(entry.get('finished_at')),
                        'fallback_used': entry.get('fallback_used') is True,
                        'events': entry.get('usage_events') or [],
                        'usage': entry.get('usage') if isinstance(entry.get('usage'), dict) else {}})
    settings = (common.config() or {}).get('economics')
    windows = []
    for hours in window_hours:
        try:
            span = float(hours)
        except (TypeError, ValueError):
            continue
        if span <= 0 or span > 24 * 90:
            continue
        start = now - span * 3600
        models, statuses, fallbacks = {}, {}, 0
        model_tasks = {}
        event_tokens = coarse_tokens = event_credits = coarse_credits = 0.0
        unpriced_total = 0.0
        event_count = 0
        event_task_ids, coarse_task_ids = set(), set()
        credits_partial = False
        for row in records:
            events = [event for event in row['events'] if start <= event['at'] <= now]
            if row['events']:
                # Timestamped record: only in-window events count. A window with
                # no events contributes zero, never the task's lifetime total.
                if not events:
                    continue
                event_task_ids.add(row['task_id'])
            else:
                if not _coarse_in_window(row, start, now):
                    continue
                coarse_task_ids.add(row['task_id'])
            statuses[row['status']] = statuses.get(row['status'], 0) + 1
            if row['fallback_used']:
                fallbacks += 1
            profile = (profiles.get(row['profile']) or {}) if isinstance(row['profile'], str) else {}
            model = profile.get('model') if isinstance(profile.get('model'), str) else None
            segments = events if events else _report_segments(row['usage'], model or row['profile'] or 'unknown')
            for segment in segments:
                if all(not segment.get(key) for key in
                       ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total')):
                    continue
                name = segment.get('model') or 'unknown'
                item = models.get(name)
                if item is None:
                    item = models[name] = {'model': name, 'segments': 0,
                                           'input': 0, 'output': 0, 'reasoning': 0,
                                           'cache_read': 0, 'cache_write': 0, 'total': 0,
                                           'event_tokens': 0, 'coarse_tokens': 0,
                                           'event_credits': 0.0, 'coarse_credits': 0.0,
                                           'credits_estimable': False, 'credits_partial': False,
                                           'unpriced_tokens': 0.0, 'rate_provenance': None}
                item['segments'] += 1
                model_tasks.setdefault(name, set()).add(row['task_id'])
                for key in ('input', 'output', 'reasoning', 'cache_read', 'cache_write'):
                    if segment.get(key) is not None:
                        item[key] += segment[key]
                total = segment.get('total')
                if total is not None:
                    item['total'] += total
                    if events:
                        item['event_tokens'] += total
                        event_tokens += total
                    else:
                        item['coarse_tokens'] += total
                        coarse_tokens += total
                estimate = economics.alibaba_credits_cost(segment, name, settings)
                if estimate is None:
                    continue
                item['credits_estimable'] = True
                item['rate_provenance'] = estimate['rate_provenance']
                if events:
                    item['event_credits'] += estimate['credits']
                    event_credits += estimate['credits']
                else:
                    item['coarse_credits'] += estimate['credits']
                    coarse_credits += estimate['credits']
                if estimate.get('precision') != 'complete':
                    item['credits_partial'] = True
                    credits_partial = True
                    item['unpriced_tokens'] += estimate.get('unpriced_tokens') or 0
                    unpriced_total += estimate.get('unpriced_tokens') or 0
            if events:
                event_count += len(events)
        for name, item in models.items():
            item['tasks'] = len(model_tasks.get(name, ()))
            item['estimated_credits'] = round(item['event_credits'] + item['coarse_credits'], 6) \
                if item['credits_estimable'] else None
            item['event_credits'] = round(item['event_credits'], 6)
            item['coarse_credits'] = round(item['coarse_credits'], 6)
            item['event_tokens'] = round(item['event_tokens'], 3)
            item['coarse_tokens'] = round(item['coarse_tokens'], 3)
        task_ids = event_task_ids | coarse_task_ids
        windows.append({'hours': round(span, 3), 'since': start,
                        'tasks': len(task_ids),
                        'event_tasks': len(event_task_ids),
                        'coarse_tasks': len(coarse_task_ids),
                        'events': event_count,
                        'statuses': dict(sorted(statuses.items())), 'fallbacks': fallbacks,
                        'observed_tokens': event_tokens + coarse_tokens,
                        'event_tokens': event_tokens, 'coarse_tokens': coarse_tokens,
                        'precision': 'timestamped' if coarse_tokens == 0 else
                                     ('mixed' if event_tokens else 'coarse'),
                        'estimated_credits': round(event_credits + coarse_credits, 6),
                        'event_credits': round(event_credits, 6),
                        'coarse_credits': round(coarse_credits, 6),
                        'credits_precision': 'partial' if credits_partial else 'complete',
                        'unpriced_tokens': unpriced_total,
                        'models': sorted(models.values(),
                                         key=lambda item: (-(item['total'] or 0), item['model']))})
    return {'generated_at': now, 'windows': windows,
            'basis': 'timestamped per-response usage events; Credits are estimates, never a billed total',
            'precision_note': 'timestamped events are exact per-response attribution; coarse values '
                              'are lifetime counters for records without events and are listed separately'}


def _coarse_in_window(row, start, now):
    """Legacy lifetime-counter inclusion; approximate and reported separately."""
    created, finished = row['created_at'], row['finished_at']
    if finished is not None and finished < start:
        return False
    if created is not None and created > now:
        return False
    return created is not None or finished is not None


def _report_segments(usage, fallback_model):
    """Per-model token rows for one record; a single fallback row when split is absent."""
    fields = ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total')
    rows = usage.get('by_model') if isinstance(usage, dict) else None
    segments = []
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and isinstance(item.get('model'), str) and item['model']:
                segment = {key: _number(item.get(key)) for key in fields}
                segment['model'] = item['model']
                segments.append(segment)
    if segments:
        return segments
    segment = {key: _number(usage.get(key)) for key in fields} if isinstance(usage, dict) else {}
    segment['model'] = fallback_model
    return [segment]


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


def _period_records(records, now, days):
    """Use response timestamps when available; legacy task totals stay coarse."""
    if days is None:
        return records
    start = now - days * 86400
    result = []
    for record in records:
        events = usage_ledger.events_for_task(record.get('task_id'), now=now)
        stamp = record.get('finished_at') or record.get('created_at')
        matches = [event for event in events if _number(event.get('at')) is not None
                   and start <= event['at'] <= now]
        if events:
            if not matches and not (stamp is not None and start <= stamp <= now):
                continue
            usage = {}
            for key in ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total', 'cost'):
                known = [_number(event.get(key)) for event in matches]
                known = [value for value in known if value is not None]
                usage[key] = sum(known) if known else None
            models = defaultdict(float)
            for event in matches:
                if _number(event.get('total')) is not None:
                    models[event.get('model') or 'unknown'] += event['total']
            usage['by_model'] = [{'model': model, 'total': total} for model, total in models.items()]
            result.append(dict(record, usage=usage, period_precision='timestamped'))
        elif stamp is not None and start <= stamp <= now:
            result.append(dict(record, period_precision='coarse'))
    return result


def summary(now=None, days=30):
    now = time.time() if now is None else now
    if days in (None, 'all'):
        days = None
    elif days in (30, '30') and not isinstance(days, bool):
        days = 30
    else:
        raise ValueError('Statistics period must be 30 or all')
    current = [_task_record(task) for task in common.tasks()]
    current_ids = {record['task_id'] for record in current}
    retained = [_ledger_record(entry) for entry in usage_ledger.entries(now=now)
                if entry.get('task_id') not in current_ids]
    history_records = current + retained
    records = _period_records(history_records, now, days)
    profiles = common.config().get('profiles', {})
    for record in records:
        record['model'] = _model(record, profiles)
    known = [record['usage']['total'] for record in records if record['usage']['total'] is not None]
    costs = [record['usage']['cost'] for record in records if record['usage']['cost'] is not None]
    active = {'queued', 'starting', 'running', 'cancelling', 'uncertain'}
    return {
        'generated_at': now,
        'period': {'days': days, 'since': now - days * 86400 if days else None,
                   'until': now, 'basis': 'response timestamps when available; legacy task activity otherwise'},
        'retention_days': common.config().get('cleanup', {}).get('usage_retention_days', 365),
        'totals': {'tasks': len(records), 'active': sum(record['status'] in active for record in records
                                                     if record['source'] == 'current'),
                   'retained': sum(record['source'] == 'ledger' for record in records),
                   'tokens': sum(known) if known else None,
                   'known_tokens': len(known), 'provider_cost': sum(costs) if costs else None,
                   'known_cost': len(costs),
                   'fallbacks': sum(record['fallback_used'] for record in records)},
        'by_tier': _breakdown(records, 'tier'),
        'by_model': _models(records),
        'by_profile': _breakdown(records, 'profile'),
        'by_status': _breakdown(records, 'status'),
        'recent': {'hour': _timeline(history_records, now, 3600, 12),
                   'day': _timeline(history_records, now, 86400, 24)},
        'daily': _daily(history_records, now),
        'quota_history': _quota_history(),
    }
