"""Bounded workload calibration from compact task metadata, never transcripts.

Worker-seconds are an empirical workload unit, not money or guaranteed throughput.
Mixed tasks need model segments; unknown legacy work is excluded from calibration.
"""
import math
import statistics


def positive(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None


def summarize(records, profiles, now):
    weights, durations, totals = {}, {}, {}
    active = []
    seen = set()
    earliest = now
    for task, usage in records:
        identity = task.get('id') or task.get('task_id')
        if identity in seen:
            continue
        seen.add(identity)
        start = positive(task.get('started_at')) or positive(task.get('created_at'))
        end = positive(task.get('finished_at'))
        if not start or (end or now) < now - 86400:
            continue
        earliest = min(earliest, max(start, now - 86400))
        segments = usage.get('by_model') if isinstance(usage, dict) else None
        model_weights = {}
        if isinstance(segments, list):
            for item in segments:
                if not isinstance(item, dict):
                    continue
                model, tokens = item.get('model'), positive(item.get('total'))
                if isinstance(model, str) and '/' in model and tokens:
                    provider = model.split('/')[0]
                    model_weights[provider] = model_weights.get(provider, 0) + tokens
        if not model_weights:
            actual = set(x for x in task.get('actual_models', []) if isinstance(x, str))
            if len(actual) > 1:
                continue
            model = next(iter(actual), None) or (profiles.get(task.get('profile')) or {}).get('model')
            if not isinstance(model, str) or '/' not in model:
                continue
            model_weights[model.split('/')[0]] = 1
        tier = task.get('tier') or 'normal'
        elapsed = min(86400, max(0, (end or now) - max(start, now - 86400)))
        whole = sum(model_weights.values())
        for provider, weight in model_weights.items():
            fraction = weight / whole
            weights[provider] = weights.get(provider, 0) + elapsed * fraction
            if end and task.get('status') == 'completed' and len(model_weights) == 1:
                durations.setdefault((provider, tier), []).append(elapsed)
                tokens = positive((usage or {}).get('total'))
                if tokens:
                    totals.setdefault((provider, tier), []).append(tokens)
        if not end and task.get('status') in ('starting', 'running'):
            model = (profiles.get(task.get('profile')) or {}).get('model', '')
            if '/' in model:
                active.append((model.split('/')[0], tier, elapsed, positive((usage or {}).get('total'))))
    denominator = sum(weights.values())
    out = {}
    for provider, seconds in weights.items():
        history = [d for (p, _), values in durations.items() if p == provider for d in values]
        out[provider] = {'observed_share': seconds / denominator if denominator else None,
                         'completed_samples': len(history), 'active_jobs': 0,
                         'expected_remaining_seconds': 0, 'prediction_samples': 0,
                         'confidence': 'calibrated' if len(history) >= 5 else 'collecting'}
    for provider, tier, elapsed, tokens in active:
        row = out.setdefault(provider, {'observed_share': None, 'completed_samples': 0,
                                       'active_jobs': 0, 'expected_remaining_seconds': 0,
                                       'prediction_samples': 0, 'confidence': 'collecting'})
        row['active_jobs'] += 1
        samples = durations.get((provider, tier), [])
        if len(samples) < 5:
            continue
        expected = statistics.median(samples)
        token_samples = totals.get((provider, tier), [])
        if tokens and len(token_samples) >= 5:
            # Bound the size adjustment: a large prompt is not a linear runtime promise.
            size = min(2, max(.5, tokens / statistics.median(token_samples)))
            expected *= size
        if elapsed >= expected:
            continue  # An overrun is uncertain, not evidence that completion is imminent.
        remaining = expected - elapsed
        row['expected_remaining_seconds'] += remaining
        row['prediction_samples'] += 1
    observed_hours = max(1, (now - earliest) / 3600)
    for provider, row in out.items():
        occupancy = weights.get(provider, 0) / (observed_hours * 3600)
        row['observed_parallelism'] = round(occupancy, 3)
        # A bounded near-term demand adjustment, never an unbounded per-job debit.
        # Unknown samples and idle time keep baseline demand. Work resumption can
        # tighten the same provider forecast by at most 25%; economic caps still apply.
        row['demand_multiplier'] = (min(1.25, max(1.0, row['active_jobs'] / occupancy))
                                    if row['confidence'] == 'calibrated' and occupancy > 0 else 1.0)
    return out


def snapshot(config, now):
    import common
    import task_activity
    import usage_ledger
    tasks = common.tasks()
    ids = {t['id'] for t in tasks}
    records = [(t, task_activity.usage_for_task(t)) for t in tasks]
    records.extend((r, r.get('usage', {})) for r in usage_ledger.entries(now=now)
                   if r.get('task_id') not in ids)
    return summarize(records, config.get('profiles', {}), now)
