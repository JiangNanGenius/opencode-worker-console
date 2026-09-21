"""Read-only capacity replay from redacted quota history (no model/API calls)."""
import argparse
import bisect
import collections
import json
from pathlib import Path
import time
from unittest.mock import patch

import capacity
import quota


def replay(history, config, limit=240):
    series = {p: sorted((s for s in v.get('samples', []) if isinstance(s.get('time'), (int, float))),
                        key=lambda s: s['time']) for p, v in history.items() if isinstance(v, dict)}
    points = sorted({s['time'] for rows in series.values() for s in rows})[-limit:]
    timestamps = {p: [s['time'] for s in rows] for p, rows in series.items()}
    control = {}
    raw_counts, stable_counts = collections.Counter(), collections.Counter()
    raw_changes = stable_changes = 0
    old_raw = old_stable = None
    for now in points:
        views = {}
        for p, rows in series.items():
            index = bisect.bisect_right(timestamps[p], now) - 1
            if index < 0:
                continue
            sample = rows[index]
            windows = []
            for key, data in sample.get('windows', {}).items():
                name, _, duration = key.partition('|')
                try:
                    duration = float(duration)
                except ValueError:
                    duration = None
                w = dict(data, name=name, duration_minutes=duration, valid=True)
                # Use exactly the stored key for historical sampling identity.
                if duration is not None and duration.is_integer():
                    w['duration_minutes'] = int(duration)
                w['consumption_estimate'] = quota.consumption_estimate(w, rows[:index], now)
                windows.append(w)
            views[p] = {'state': 'ok', 'stale': now - sample['time'] > 900,
                        'sampled_at': sample['time'], 'windows': windows,
                        'available': not any(w.get('remaining_percent', 0) <= 0 for w in windows),
                        'balances': [{'currency': k, 'remaining': v} for k, v in sample.get('balances', {}).items()]}
        with patch.object(time, 'time', return_value=now):
            guidance = quota.tier_guidance(config, views, _raw=True)
        raw = guidance['conservation_level']
        control = capacity.transition(raw, guidance['capacity_pressure_percent'],
                                      [guidance['runway_threshold_percent'], guidance['level2_runway_percent']],
                                      control, now, guidance['conservation_refill_safe'])
        stable = control['level']
        raw_counts[raw] += 1
        stable_counts[stable] += 1
        raw_changes += int(old_raw is not None and old_raw != raw)
        stable_changes += int(old_stable is not None and old_stable != stable)
        old_raw, old_stable = raw, stable
    return {'samples': len(points), 'raw_level_samples': dict(raw_counts),
            'stabilized_level_samples': dict(stable_counts), 'raw_transitions': raw_changes,
            'stabilized_transitions': stable_changes,
            'limitations': 'Historical workload, not a counterfactual cost or task-quality benchmark; no requests or state changes.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=240)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error('limit must be 1..10000')
    print(json.dumps(replay(json.loads(args.history.read_text()), json.loads(args.config.read_text()), args.limit), indent=2))
