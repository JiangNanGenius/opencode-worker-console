"""Pure, fail-closed evidence for *retry authorization*, never inference health.

Callers capture identities immediately before an official query and re-read them
at completion. Re-evaluate under the billing lock against the current block
before a CAS write, preserving message_ids and the recovery watermark. No I/O,
credentials, imports of provider clients, clock reads or state mutations occur.

Ark's fetch() has no source marker: official_query=True is an explicit trust
assertion by the official GetAFPUsage call site, never by imported cache data.
Its account/control identity is deliberately distinct from inference identity.
AFP positive windows cannot prove cash balance, exact reset or inference health.
The integrating caller must bound attempts with a per-credential cooldown/backoff;
this predicate alone must never repeatedly reopen a failing circuit.
Kimi uses its independent verified account/month-total contract only.
"""
import math
from datetime import datetime

AFP_NAMES = frozenset(('AFPFiveHour', 'AFPDaily', 'AFPWeekly', 'AFPMonthly'))
KIMI_SOURCES = frozenset(('kimi_code_account', 'kimi_web_membership'))


def _result(allowed, kind='none', reason='invalid_evidence'):
    return {'allowed': allowed, 'evidence_kind': kind, 'reason': reason}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _epoch(value):
    numeric = _number(value)
    if numeric is not None:
        return numeric if numeric > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _identity_matches(block, captured, current):
    return (isinstance(captured, str) and bool(captured.strip()) and
            isinstance(current, str) and captured == current and
            block.get('credential') == captured)


def _query_reason(block, reading, captured, current, started, completed, now, max_age):
    if not isinstance(block, dict) or not block or block.get('cleared_at'):
        return 'inactive_block'
    if not _identity_matches(block, captured, current):
        return 'inference_identity_changed_or_missing'
    if not isinstance(reading, dict) or reading.get('state') != 'ok' or (reading.get('stale') is not None and reading.get('stale') is not False):
        return 'reading_not_current'
    # Error objects can survive stale-cache merges. No error-bearing read is proof.
    if any(reading.get(key) for key in ('error', 'live_error')):
        return 'reading_has_error'
    opened, start, end, clock, sample = map(_epoch, (
        block.get('opened_at'), started, completed, now, reading.get('sampled_at')))
    age = _number(max_age)
    if None in (opened, start, end, clock, sample, age) or age <= 0:
        return 'invalid_time'
    if not (opened < start <= sample <= end <= clock) or clock - start > age:
        return 'query_not_fresh_after_block'
    status = _number(reading.get('http_status'))
    if status is not None and status >= 400:
        return 'reading_has_http_error'
    if reading.get('available') is not True:
        return 'reading_not_positive'
    # Explicit expiry is distinct from reset. Neither may already have passed.
    for key in ('expires_at', 'ends_at'):
        if key in reading and (_epoch(reading[key]) is None or _epoch(reading[key]) <= clock):
            return 'reading_expired_or_unknown'
    plan = reading.get('plan')
    if isinstance(plan, dict):
        if plan.get('active') is False:
            return 'plan_inactive'
        if 'ends_at' in plan and (_epoch(plan['ends_at']) is None or _epoch(plan['ends_at']) <= clock):
            return 'plan_expired_or_unknown'
    return None


def _positive_window(window, now, require_reset=False):
    if not isinstance(window, dict) or window.get('valid') is not True:
        return False
    limit, remaining, percent = (_number(window.get(key)) for key in (
        'limit', 'remaining', 'remaining_percent'))
    if limit is None or remaining is None or limit <= 0 or not 0 < remaining <= limit:
        return False
    if percent is None or not 0 < percent <= 100:
        return False
    # Normalizers round percentages to 3 decimals; contradictory fields refuse.
    if abs(percent - remaining / limit * 100) > 0.0011:
        return False
    for key in ('expires_at', 'resets_at'):
        if key in window and window[key] is not None:
            expiry = _epoch(window[key])
            if expiry is None or expiry <= now:
                return False
    if require_reset and (_epoch(window.get('resets_at')) is None or _epoch(window['resets_at']) <= now):
        return False
    return True


def ark_retry_evidence(block, reading, *, captured_inference_identity,
                       current_inference_identity, captured_control_identity,
                       current_control_identity, query_started_at,
                       query_completed_at, now, official_query=False,
                       prepaid=False, max_age_seconds=120):
    """Authorize one bounded Ark quota retry; no cash/auth recovery.

    Exactly all four normalized AFP windows are required, not a positive subset.
    Identity arguments are opaque hashes supplied by the caller, never returned.
    """
    if prepaid is not False or official_query is not True:
        return _result(False, reason='prepaid_or_unproven_official_query')
    if not isinstance(block, dict) or block.get('reason') not in ('billing_error', 'usage_window_limit', 'monthly_usage_limit'):
        return _result(False, reason='block_reason_not_eligible')
    if (not isinstance(captured_control_identity, str) or not captured_control_identity.strip() or
            captured_control_identity != current_control_identity):
        return _result(False, reason='control_identity_changed_or_missing')
    reason = _query_reason(block, reading, captured_inference_identity,
                           current_inference_identity, query_started_at,
                           query_completed_at, now, max_age_seconds)
    if reason:
        return _result(False, reason=reason)
    windows = reading.get('windows')
    if not isinstance(windows, list) or len(windows) != len(AFP_NAMES):
        return _result(False, reason='afp_windows_incomplete')
    if (any(not isinstance(w, dict) or not isinstance(w.get('name'), str) for w in windows) or
            {w['name'] for w in windows} != AFP_NAMES):
        return _result(False, reason='afp_windows_incomplete')
    if not all(_positive_window(w, _epoch(now), require_reset=True) for w in windows):
        return _result(False, reason='afp_window_not_positive_or_unexpired')
    return _result(True, 'ark_official_afp_retry_authorization', 'fresh_positive_all_afp_windows')


def kimi_monthly_retry_evidence(block, reading, *, captured_inference_identity,
                                current_inference_identity, query_started_at,
                                query_completed_at, now, prepaid=False,
                                max_age_seconds=120):
    """Authorize retry after verified Kimi monthly pool evidence, never key health.

    A genuine month_total is mandatory. Every present month_code also needs
    positive official evidence. Overall/week/5h and inferred billing resets do
    not qualify. Each monthly window needs a future expiry or reset; neither is inferred.
    """
    if prepaid is not False:
        return _result(False, reason='prepaid_not_eligible')
    if not isinstance(block, dict) or block.get('reason') != 'monthly_usage_limit':
        return _result(False, reason='block_reason_not_eligible')
    reason = _query_reason(block, reading, captured_inference_identity,
                           current_inference_identity, query_started_at,
                           query_completed_at, now, max_age_seconds)
    if reason:
        return _result(False, reason=reason)
    binding = reading.get('account_binding')
    if not isinstance(binding, dict) or binding.get('state') != 'verified':
        return _result(False, reason='account_binding_unverified')
    if reading.get('source') not in KIMI_SOURCES:
        return _result(False, reason='account_source_not_official')
    windows = reading.get('windows')
    if not isinstance(windows, list) or any(not isinstance(w, dict) for w in windows):
        return _result(False, reason='monthly_windows_missing')
    monthly = [w for w in windows if w.get('kind') in ('month_total', 'month_code')]
    if sum(w.get('kind') == 'month_total' for w in monthly) != 1:
        return _result(False, reason='monthly_total_missing_or_ambiguous')
    if sum(w.get('kind') == 'month_code' for w in monthly) > 1:
        return _result(False, reason='monthly_code_ambiguous')
    for window in monthly:
        expected_name = 'monthTotal' if window['kind'] == 'month_total' else 'monthCode'
        if (window.get('name') != expected_name or window.get('source') not in KIMI_SOURCES or
                not _positive_window(window, _epoch(now)) or
                not any((_epoch(window.get(key)) or 0) > _epoch(now)
                        for key in ('expires_at', 'resets_at'))):
            return _result(False, reason='monthly_window_not_positive_or_official')
    return _result(True, 'kimi_verified_monthly_pool_retry_authorization', 'fresh_positive_real_monthly_pool')
