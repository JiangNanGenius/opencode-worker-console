"""Pure, shared capacity forecasts. No I/O or routing side effects."""
import math
from datetime import datetime, timezone


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def epoch(value):
    n = number(value)
    if n is not None:
        return n / 1000 if n >= 1e12 else n
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            pass
    return None


def window(value, now):
    """Forecast at observed working pace; a reset schedule is the cold-start prior.

    ``runway`` stays the display endurance: remaining hours at the observed rate
    over time to reset. ``prior_runway`` is the same ratio at the window's own
    full-cycle pace (remaining fraction over remaining time fraction). Automatic
    routing takes the more pessimistic of the two; the UI keeps the observed
    fit. Without a real reset schedule there is no prior to expose.
    """
    if not isinstance(value, dict) or value.get('valid') is not True:
        return None
    p = number(value.get('remaining_percent'))
    if p is None:
        return None
    p = max(0.0, min(100.0, p))
    reset = epoch(value.get('resets_at'))
    start = epoch(value.get('subscribed_at'))
    duration = number(value.get('duration_minutes'))
    span = (reset - start) / 3600 if reset and start and reset > start else (duration or 0) / 60
    left = (reset - now) / 3600 if reset and reset > now else None
    estimate = value.get('consumption_estimate') or {}
    rates = [number(estimate.get(key)) for key in ('rate_percent_per_hour', 'active_rate_percent_per_hour')]
    rates = [v for v in rates if v is not None and v > .000001]
    reported_hours = number(estimate.get('hours'))
    if reported_hours and reported_hours > 0 and p > 0:
        rates.append(p / reported_hours)
    rate = max(rates) if rates else None
    # Schedule-aware pace: at the cycle's own average burn rate the allowance
    # lands exactly on zero at reset, so this is the pace a plan must not exceed.
    # It is exposed separately and never replaces the observed display runway.
    fraction = min(1.0, left / span) if left and span > 0 else None
    prior = max(0.0, min(4.0, p / 100.0 / fraction)) if fraction else None
    if rate:
        hours, full = p / rate, 100 / rate
        runway = hours / left if left else p / 100
        source = 'observed_burn'
    else:
        full = span or None
        hours = full * p / 100 if full is not None else None
        runway = prior if prior is not None else p / 100
        source = 'window_prior'
    return {'window': value.get('name'), 'remaining_percent': p, 'hours': hours,
            'capacity_hours': full, 'rate_percent_per_hour': rate, 'source': source,
            'runway': max(0.0, min(4.0, runway)), 'prior_runway': prior, 'reset_hours': left,
            'resets_at': value.get('resets_at'), 'reset_epoch': reset,
            'expired': bool(reset and reset <= now)}


def provider(record, now):
    record = record if isinstance(record, dict) else {}
    windows = [f for v in record.get('windows', []) if (f := window(v, now)) is not None]
    # Running-task count is useful scheduling telemetry, but it is not quota
    # consumption. Pre-charging the forecast when several tasks start together
    # makes the pool meter jump before a provider counter has moved. The window
    # estimator already observes real burn, so only that evidence drives runway.
    demand = 1.0
    for w in windows:
        w['demand_multiplier'] = demand

    fresh = not record.get('stale', True) and record.get('state') == 'ok' and not any(w['expired'] for w in windows)
    exhausted = record.get('available') is False or any(w['remaining_percent'] <= 0 for w in windows)
    bottleneck = min(windows, key=lambda w: w['runway']) if windows else None
    return {'fresh': fresh, 'exhausted': exhausted, 'windows': windows,
            'bottleneck': bottleneck['window'] if bottleneck else None,
            'runway': (0.0 if exhausted else bottleneck['runway']) if fresh and bottleneck else None,
            'hours': min((w['hours'] for w in windows if w['source'] == 'observed_burn' and w['hours'] is not None), default=None)}


def transition(raw_level, pressure, thresholds, previous, now, recovery=False):
    """Immediate protection; release only after observed quota replenishment.

    Read/preview callers never update state. Refresh owns persistence. Unknown data
    produces no new conservation action. Forecast refills are deliberately ignored:
    a lower conservation level is accepted only when fresh telemetry proves that at
    least one subscription window was actually replenished.
    """
    previous = previous if isinstance(previous, dict) else {}
    old = previous.get('level', raw_level)
    if old not in (0, 1, 2):
        old = raw_level
    if raw_level >= old:
        return {'level': raw_level, 'at': now, 'pending_since': None}
    if pressure is None:
        return {'level': old, 'at': now, 'pending_since': None}
    if recovery:
        return {'level': raw_level, 'at': now, 'pending_since': None}
    return {'level': old, 'at': now, 'pending_since': None}
