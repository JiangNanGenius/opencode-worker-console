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


def work_pool(config, quota_view):
    """Normalized, additive view of the total available work pool.

    The normalization boundary lives here, next to the existing configurable
    CNY->AFP-equivalent ruler, so the UI never sums raw CNY against AFP units.

    * ``balance`` is every fresh CNY pay-as-you-go balance converted with the
      configured cost ruler (1/``afp_cny_per_unit``). Non-CNY balances are
      excluded: there is no defined FX normalization for them.
    * ``plan`` is the authoritative live Ark monthly AFP allowance (the
      control-plane AFPMonthly ``remaining`` value), never a nominal plan-price
      estimate.

    Each component carries its own status/unit/stale metadata so a missing,
    zero or stale source renders as one segment or zero without being mistaken
    for the other. Kimi is intentionally absent: its endpoint exposes no
    additive work-unit allowance (see module note).
    """
    values = normalize((config or {}).get('economics'))
    per_cny = 1.0 / values['afp_cny_per_unit']
    view = quota_view if isinstance(quota_view, dict) else {}

    balance_amount = 0.0
    balance_source = 0.0
    balance_currencies = []
    balance_state = 'missing'
    deepseek = view.get('deepseek') if isinstance(view.get('deepseek'), dict) else {}
    if deepseek:
        balances = deepseek.get('balances') or []
        cny = [b for b in balances if isinstance(b, dict) and b.get('currency') == 'CNY']
        balance_currencies = sorted({b.get('currency') for b in balances
                                     if isinstance(b, dict) and b.get('currency')})
        if cny:
            known = [a for a in (_non_negative(b.get('remaining')) for b in cny) if a is not None]
            if known:
                balance_source = sum(known)
                balance_amount = balance_source * per_cny
                if deepseek.get('available') is False:
                    balance_state = 'unavailable'
                elif deepseek.get('stale'):
                    balance_state = 'stale'
                else:
                    balance_state = 'ok'
            else:
                balance_state = 'unknown'
        else:
            balance_state = 'missing' if deepseek.get('state') in (None, 'ok') else 'unknown'

    monthly = _monthly_ark(view)
    ark = view.get('volcengine-agent-plan') if isinstance(view.get('volcengine-agent-plan'), dict) else {}
    plan_amount = _non_negative(monthly['remaining']) if monthly else None
    if plan_amount is None:
        plan_state = 'missing' if not ark else ('unknown' if ark.get('state') not in (None, 'ok') else 'missing')
    else:
        plan_state = 'stale' if ark.get('stale') else 'ok'

    balance = {'amount': 0.0 if balance_state == 'unavailable' else round(balance_amount, 3),
               'unit': 'AFP-equivalent',
               'source_amount': round(balance_source, 3), 'currency': 'CNY',
               'status': balance_state, 'stale': balance_state == 'stale',
               'excluded_currencies': [c for c in balance_currencies if c != 'CNY']}
    plan = {'amount': round(plan_amount, 3) if plan_amount is not None else None,
            'source_amount': round(plan_amount, 3) if plan_amount is not None else None,
            'unit': 'AFP', 'status': plan_state, 'stale': plan_state == 'stale',
            'resets_at': monthly.get('resets_at') if monthly else None}
    return {'unit': 'AFP-equivalent', 'total': round(balance['amount'] + (plan['amount'] or 0.0), 3),
            'components': {'balance': balance, 'plan': plan},
            'complete': balance_state in ('ok', 'stale') and plan_state in ('ok', 'stale'),
            'normalization': {'rule': 'cny_balance / settings.economics.afp_cny_per_unit',
                              'afp_eq_per_cny': per_cny,
                              'plan_source': 'live Ark AFPMonthly remaining',
                              'kimi_included': False,
                              'notes': {'balance_is_cost_ruler': True,
                                        'plan_is_authoritative_afp': True}}}


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
