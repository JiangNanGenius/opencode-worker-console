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
import math


DEFAULTS = {
    'afp_cny_per_unit': 0.002,
    'kimi_plan_cny': 699.0,
    'ark_auto_afp_per_m': 50.0,
    'ark_evolving_afp_per_m': 250.0,
    'ark_k3_afp_per_m': 1000.0,
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
            out[key] = _number(source.get(key, default), key)
        except ValueError:
            out[key] = default
    return out


def validate(value):
    """Strict settings-API validation; every supported field is required."""
    if not isinstance(value, dict):
        raise ValueError('economics must be an object')
    missing = [key for key in DEFAULTS if key not in value]
    if missing:
        raise ValueError('economics is missing: ' + ', '.join(missing))
    unknown = [key for key in value if key not in DEFAULTS]
    if unknown:
        raise ValueError('economics contains unsupported fields: ' + ', '.join(unknown))
    return {key: _number(value[key], key) for key in DEFAULTS}


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
        'notes': {
            'kimi_equivalence_is_nominal': True,
            'model_coefficients_are_configurable_estimates': True,
            'ark_control_plane_is_authoritative': True,
        },
    }
