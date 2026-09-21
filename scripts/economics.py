"""Nominal plan-cost comparison for Worker Desk.

This module deliberately keeps three different facts separate:

* Ark ``Used`` values are authoritative control-plane AFP telemetry.
* AFP-equivalent is a user-configurable money comparison unit, not provider
  quota.  The default is CNY 0.002 per AFP-equivalent.
* Kimi's plan price can be converted to a nominal AFP-equivalent purchase
  value, but never to an official Kimi token allowance.

Model coefficients are configuration inputs because promotions and provider
terms change.  They are estimates only; the live Ark control-plane delta is the
billing authority.
"""
from datetime import datetime, timedelta, timezone
import math
import time


DEEPSEEK_PRICE_SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
DEEPSEEK_PRICE_EFFECTIVE = '2026-09-10'


DEFAULTS = {
    'afp_cny_per_unit': 0.002,
    'kimi_plan_cny': 699.0,
    'ark_auto_afp_per_m': 50.0,
    'ark_evolving_afp_per_m': 250.0,
    'ark_k3_afp_per_m': 1000.0,
    # Official DeepSeek peak prices in CNY per million tokens. Off-peak is
    # derived with the documented multiplier so one setting cannot drift away
    # from the other two price bands.
    'deepseek_flash_cache_hit_cny_per_m': 0.04,
    'deepseek_flash_input_cny_per_m': 2.0,
    'deepseek_flash_output_cny_per_m': 8.0,
    'deepseek_pro_cache_hit_cny_per_m': 0.30,
    'deepseek_pro_input_cny_per_m': 9.0,
    'deepseek_pro_output_cny_per_m': 27.0,
    'deepseek_offpeak_multiplier': 0.5,
}


def _number(value, key, low=0.000001, high=10000000.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(key + ' must be a number')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(key + ' is outside the supported range')
    return value


def normalize(value):
    """Fail-safe stored configuration with complete defaults."""
    source = value if isinstance(value, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        try:
            out[key] = _number(source.get(key, default), key, high=1.0) \
                if key == 'deepseek_offpeak_multiplier' else _number(source.get(key, default), key)
        except ValueError:
            out[key] = default
    return out


def validate(value):
    """Strict settings-API validation with compatible pricing defaults."""
    if not isinstance(value, dict):
        raise ValueError('economics must be an object')
    legacy_required = ('afp_cny_per_unit', 'kimi_plan_cny', 'ark_auto_afp_per_m',
                       'ark_evolving_afp_per_m', 'ark_k3_afp_per_m')
    missing = [key for key in legacy_required if key not in value]
    if missing:
        raise ValueError('economics is missing: ' + ', '.join(missing))
    unknown = [key for key in value if key not in DEFAULTS]
    if unknown:
        raise ValueError('economics contains unsupported fields: ' + ', '.join(unknown))
    result = {}
    for key, default in DEFAULTS.items():
        raw = value.get(key, default)
        result[key] = _number(raw, key, high=1.0) if key == 'deepseek_offpeak_multiplier' \
            else _number(raw, key)
    return result


def deepseek_peak(timestamp):
    """Return whether one timestamp falls in DeepSeek's Beijing peak band."""
    local = datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(hours=8)
    return local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)


def _usage_number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else 0.0


def deepseek_usage_cost(usage, model, timestamp, settings=None):
    """Estimate one DeepSeek usage snapshot from the official token classes.

    Worker Desk keeps input, cache-read, cache-write, output and reasoning
    counters disjoint. Cache reads use the hit price; ordinary input and cache
    writes use the cache-miss price; output and reasoning use the output price.
    """
    if not isinstance(usage, dict) or not isinstance(model, str):
        return None
    values = normalize(settings)
    model_name = model.split('/', 1)[-1].lower()
    family = 'pro' if 'pro' in model_name else 'flash'
    hit = values['deepseek_' + family + '_cache_hit_cny_per_m']
    input_rate = values['deepseek_' + family + '_input_cny_per_m']
    output_rate = values['deepseek_' + family + '_output_cny_per_m']
    multiplier = 1.0 if deepseek_peak(timestamp) else values['deepseek_offpeak_multiplier']
    cache_hit = _usage_number(usage.get('cache_read'))
    uncached = _usage_number(usage.get('input')) + _usage_number(usage.get('cache_write'))
    generated = _usage_number(usage.get('output')) + _usage_number(usage.get('reasoning'))
    tokens = cache_hit + uncached + generated
    if tokens <= 0:
        return None
    cost = (cache_hit * hit + uncached * input_rate + generated * output_rate) * multiplier / 1_000_000
    return {'cost_cny': cost, 'tokens': tokens, 'peak': multiplier == 1.0, 'family': family,
            'cache_hit_tokens': cache_hit, 'input_tokens': uncached, 'output_tokens': generated}


def recent_deepseek_spend(config, now=None, lookback_hours=24):
    """Price recent Worker Desk DeepSeek usage without reading prompts/messages."""
    import common
    import task_activity
    import usage_ledger

    now = time.time() if now is None else now
    cutoff = now - lookback_hours * 3600
    profiles = (config or {}).get('profiles') if isinstance(config, dict) else {}
    profiles = profiles if isinstance(profiles, dict) else {}
    settings = (config or {}).get('economics') if isinstance(config, dict) else None
    records = []
    current_ids = set()
    for task in common.tasks():
        current_ids.add(task.get('id'))
        records.append((task, task_activity.usage_for_task(task)))
    for entry in usage_ledger.entries(now=now):
        if entry.get('task_id') not in current_ids:
            records.append((entry, entry.get('usage')))
    total_cost = total_tokens = 0.0
    task_count = 0
    earliest = now
    peak_tasks = 0
    for record, usage in records:
        stamp = record.get('finished_at') or record.get('updated_at') or record.get('created_at')
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp < cutoff or stamp > now:
            continue
        actual = [item for item in (record.get('actual_models') or [])
                  if isinstance(item, str) and item.startswith('deepseek/')]
        profile = profiles.get(record.get('profile')) if isinstance(profiles.get(record.get('profile')), dict) else {}
        model = actual[-1] if actual else profile.get('model')
        if not isinstance(model, str) or not model.startswith('deepseek/'):
            continue
        priced = deepseek_usage_cost(usage, model, stamp, settings)
        if priced is None:
            continue
        total_cost += priced['cost_cny']
        total_tokens += priced['tokens']
        peak_tasks += int(priced['peak'])
        task_count += 1
        created = record.get('created_at')
        earliest = min(earliest, created if isinstance(created, (int, float)) and not isinstance(created, bool) else stamp)
    if task_count == 0 or total_cost <= 0:
        return None
    # One short task should not be extrapolated as if it ran continuously every
    # minute. Amortize over at least one hour, while retaining at most one day
    # so the estimate follows the current workload rather than lifetime usage.
    span_hours = max(1.0, min(float(lookback_hours), (now - max(cutoff, earliest)) / 3600))
    return {'rate_balance_per_hour': total_cost / span_hours, 'estimated_spend_cny': total_cost,
            'tokens': total_tokens, 'task_count': task_count, 'sample_span_hours': span_hours,
            'peak_tasks': peak_tasks, 'source': 'official_token_pricing',
            'pricing_effective': DEEPSEEK_PRICE_EFFECTIVE, 'pricing_url': DEEPSEEK_PRICE_SOURCE}


def _monthly_ark(quota_view):
    ark = quota_view.get('volcengine-agent-plan') if isinstance(quota_view, dict) else None
    if not isinstance(ark, dict):
        return None
    for window in ark.get('windows') or []:
        if not isinstance(window, dict) or window.get('name') != 'AFPMonthly' or window.get('valid') is not True:
            continue
        limit = window.get('limit')
        remaining = window.get('remaining')
        if isinstance(limit, (int, float)) and isinstance(remaining, (int, float)):
            return {'limit': limit, 'used': max(0.0, limit - remaining),
                    'remaining': remaining, 'resets_at': window.get('resets_at')}
    return None


def _non_negative(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _percent(amount, capacity):
    if amount is None or capacity is None or capacity <= 0:
        return None
    return round(max(0.0, min(100.0, amount / capacity * 100)), 3)


def _selected_window(provider, record):
    windows = [item for item in record.get('windows') or [] if isinstance(item, dict)
               and item.get('valid') is not False and _non_negative(item.get('remaining_percent')) is not None]
    if not windows:
        return None
    preferred = 'overall' if provider == 'kimi-for-coding' else 'AFPWeekly'
    return next((item for item in windows if item.get('name') == preferred), None) or \
        max(windows, key=lambda item: _non_negative(item.get('duration_minutes')) or 0.0)


def _window_fit(provider, record):
    window = _selected_window(provider, record)
    if window is None:
        return {'amount': None, 'capacity': None, 'remaining_percent': None,
                'source_amount': None, 'unit': 'fitted-hours',
                'status': 'missing' if not record else 'unknown', 'stale': bool(record.get('stale'))}
    remaining = _non_negative(window.get('remaining_percent'))
    estimate = window.get('consumption_estimate') if isinstance(window.get('consumption_estimate'), dict) else {}
    rate = _non_negative(estimate.get('rate_percent_per_hour'))
    duration = _non_negative(window.get('duration_minutes'))
    # The live burn rate is authoritative. A full-window duration is only the
    # cold-start prior, so a newly reset plan immediately re-enters the pool
    # while its next-cycle fit is still collecting.
    fitted_capacity = 100.0 / rate if rate and rate > .000001 else None
    window_capacity = duration / 60.0 if duration else None
    # A subscription window cannot contribute more useful runtime than the
    # time until its next refill. This keeps long idle periods from inflating a
    # weekly plan into several weeks of apparent capacity.
    capacity = min(fitted_capacity, window_capacity) if fitted_capacity is not None and window_capacity is not None \
        else fitted_capacity if fitted_capacity is not None else window_capacity
    amount = capacity * remaining / 100.0 if capacity is not None else None
    if amount is not None and record.get('available') is False:
        amount = 0.0
    if remaining <= 0 or record.get('available') is False:
        state = 'unavailable'
    else:
        state = 'stale' if record.get('stale') else 'ok'
    return {'amount': round(amount, 3) if amount is not None else None,
            'capacity': round(capacity, 3) if capacity is not None else None,
            'remaining_percent': _percent(amount, capacity),
            'source_amount': round(remaining, 3), 'unit': 'fitted-hours',
            'status': state, 'stale': state == 'stale', 'resets_at': window.get('resets_at'),
            'window': window.get('name'),
            'fit_source': 'observed_burn' if rate and rate > .000001 else 'window_prior'}


def _balance_fit(record):
    balances = record.get('balances') or []
    cny = [item for item in balances if isinstance(item, dict) and item.get('currency') == 'CNY']
    currencies = sorted({item.get('currency') for item in balances
                         if isinstance(item, dict) and item.get('currency')})
    remaining_value = remaining_hours = capacity_hours = 0.0
    fitted = 0
    for item in cny:
        remaining = _non_negative(item.get('remaining'))
        estimate = item.get('consumption_estimate') if isinstance(item.get('consumption_estimate'), dict) else {}
        rate = _non_negative(estimate.get('rate_balance_per_hour'))
        observed = _non_negative(estimate.get('observed_capacity'))
        if remaining is None:
            continue
        remaining_value += remaining
        if rate and rate > .000001:
            remaining_hours += remaining / rate
            capacity_hours += max(remaining, observed or remaining) / rate
            fitted += 1
    if not cny:
        state = 'missing' if not record or record.get('state') in (None, 'ok') else 'unknown'
    elif record.get('available') is False or remaining_value <= 0:
        state = 'unavailable'
    else:
        state = 'stale' if record.get('stale') else 'ok'
    amount = remaining_hours if fitted else None
    capacity = capacity_hours if fitted else None
    if amount is not None and record.get('available') is False:
        amount = 0.0
    return {'amount': round(amount, 3) if amount is not None else None,
            'capacity': round(capacity, 3) if capacity is not None else None,
            'remaining_percent': _percent(amount, capacity), 'unit': 'fitted-hours',
            'source_amount': round(remaining_value, 3), 'currency': 'CNY',
            'status': state, 'stale': state == 'stale',
            'excluded_currencies': [currency for currency in currencies if currency != 'CNY'],
            'fit_source': 'observed_burn' if fitted else 'collecting'}


def _reset_seconds(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value / 1000.0 if value >= 1e12 else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _refill_forecast(components, capacity, now):
    if capacity <= 0:
        return []
    current = {key: (item.get('amount') or 0.0) for key, item in components.items()
               if item.get('capacity') is not None}
    events = []
    for key in ('kimi', 'plan'):
        item = components[key]
        stamp = _reset_seconds(item.get('resets_at'))
        if stamp is not None and stamp > now and item.get('capacity') is not None:
            events.append((stamp, key))
    forecast = []
    previous = now
    for stamp, key in sorted(events):
        elapsed = max(0.0, (stamp - previous) / 3600.0)
        current = {name: max(0.0, amount - elapsed) for name, amount in current.items()}
        current[key] = components[key]['capacity']
        total = sum(current.values())
        forecast.append({'provider': key, 'resets_at': components[key].get('resets_at'),
                         'hours_until': round(max(0.0, (stamp - now) / 3600.0), 2),
                         'projected_remaining_percent': _percent(total, capacity)})
        previous = stamp
    return forecast


def work_pool(config, quota_view, now=None, include_payg_balance=True):
    """Fit unlike provider quotas onto one actual-workload runtime axis.

    Each slot's full width is its estimated runtime at its own observed burn
    rate; the filled part is the fitted runtime still available. No AFP, token
    price, or plan purchase price is used to weight this meter. Window duration
    is a temporary cold-start prior only, allowing a reset source to reappear
    before enough new-cycle samples exist.
    """
    view = quota_view if isinstance(quota_view, dict) else {}
    deepseek = view.get('deepseek') if isinstance(view.get('deepseek'), dict) else {}
    kimi = view.get('kimi-for-coding') if isinstance(view.get('kimi-for-coding'), dict) else {}
    ark = view.get('volcengine-agent-plan') if isinstance(view.get('volcengine-agent-plan'), dict) else {}
    components = {'balance': _balance_fit(deepseek),
                  'kimi': _window_fit('kimi-for-coding', kimi),
                  'plan': _window_fit('volcengine-agent-plan', ark)}
    included = components if include_payg_balance else {
        key: components[key] for key in ('kimi', 'plan')}
    known = [item for item in included.values() if item.get('capacity') is not None]
    total = sum(item.get('amount') or 0.0 for item in known)
    capacity = sum(item['capacity'] for item in known)
    refills = _refill_forecast(included, capacity, time.time() if now is None else now)
    return {'unit': 'fitted-hours', 'total': round(total, 3),
            'capacity': round(capacity, 3), 'remaining_percent': _percent(total, capacity),
            'components': components, 'refills': refills,
            'complete': bool(known) and all(item.get('status') in ('ok', 'stale', 'unavailable')
                                            for item in known),
            'normalization': {'rule': 'remaining_runtime / fitted_full_runtime',
                              'payg_balance_included': bool(include_payg_balance),
                              'kimi_included': components['kimi'].get('capacity') is not None,
                              'notes': {'weights_use_observed_burn': True,
                                        'afp_not_used_as_weight': True,
                                        'prices_not_used_as_weight': True}}}


def summary(config, quota_view):
    values = normalize((config or {}).get('economics'))
    unit = values['afp_cny_per_unit']
    kimi_eq = values['kimi_plan_cny'] / unit
    deep_ark_share = 1.0 / 3.0
    normal_ark_share = 1.0 / 2.0
    coefficients = {
        'deep': values['ark_k3_afp_per_m'] * deep_ark_share,
        'normal': values['ark_evolving_afp_per_m'] * normal_ark_share,
        'small': values['ark_auto_afp_per_m'],
    }
    monthly = _monthly_ark(quota_view)
    if monthly:
        monthly = dict(monthly,
                       used_cny=monthly['used'] * unit,
                       remaining_cny=monthly['remaining'] * unit)
    return {
        'settings': values,
        'afp_eq_per_cny': 1.0 / unit,
        'kimi_plan_afp_eq': kimi_eq,
        'baseline_ark_afp_per_m': coefficients,
        'baseline_formula': 'Ark AFP = %.2fD + %.2fN + %.2fS' % (
            coefficients['deep'], coefficients['normal'], coefficients['small']),
        'ark_monthly': monthly,
        'work_pool': work_pool(config, quota_view),
        'notes': {
            'kimi_equivalence_is_nominal': True,
            'model_coefficients_are_configurable_estimates': True,
            'ark_control_plane_is_authoritative': True,
        },
    }
