"""Public display visibility for terminal allowance exhaustion.

A current quota row is hidden only when one fresh, complete reading proves that
every usable pool is empty *and* the next recovery needs a new purchase or
renewal. Concretely:

* ``state`` must be ``ok``, the sample must be at most 15 minutes old, and no
  ``stale`` or non-empty ``live_error`` may be present. Unknown, failed or
  aging telemetry is retained so it can never be mistaken for a confirmed zero.
* ``available`` must be exactly ``False``. A missing amount/percentage is
  unknown, never zero.
* The plan must carry a paid-term end (``subscription.end_at`` or
  ``plan.ends_at``). Funding pools (Ark ``AFPMonthly``, Alibaba ``Credits(...)``
  and Personal ``Monthly``) must be valid, known-empty and reset at or after
  that end. Short-cycle guards (5-hour, daily, weekly) are constraints, not
  separate money: a positive 5-hour/weekly remainder beside a terminally empty
  monthly pool cannot refill it. Conversely, a zero 5-hour/weekly guard beside
  a positive monthly pool is recoverable and stays visible.
* Only when no funding pool is distinguishable does the rule require every
  reported window to be terminal; that fallback is conservative.
* A Personal Alibaba plan must also have a known-empty add-on summary. An
  unknown add-on can still contain Credits and therefore keeps the row visible.
* A cash-balance provider (DeepSeek) is terminal only when its fresh reading
  reports ``available: false`` and every known balance is exactly zero; that
  recovery requires a top-up. Missing or positive balances do not hide.

The same rule is consumed by ``quota.view`` (console cards and work-pool
sources), the shared host collector/board payload and the standalone
MacStatusBoard fallback, so all displays agree. Hiding is display-only: the raw
quota record, provider configuration, query path and history ledger are never
changed, and a newer positive sample makes the row reappear automatically.
"""
from datetime import datetime
import math
import time

FRESH_SECONDS = 900
# Exact subscription end and window reset are expected; allow only clock noise.
RESET_TOLERANCE_SECONDS = 1.0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _epoch(value):
    """Epoch seconds from a number or an aware ISO-8601 string; else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        if seconds >= 1e12:  # Provider millisecond timestamps.
            seconds /= 1000.0
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        seconds = parsed.timestamp()
    else:
        return None
    return seconds if 946_684_800 <= seconds <= 4_102_444_800 else None


def _fresh(value, now):
    if not isinstance(value, dict) or value.get('state') != 'ok':
        return False
    # Any supplied live_error (dict, string or other non-empty value) means
    # this reading is a retained/fallback view, not fresh proof of exhaustion.
    if value.get('stale') is True or value.get('live_error'):
        return False
    snapshot = value.get('snapshot')
    if isinstance(snapshot, dict):
        if snapshot.get('freshness') not in (None, 'fresh') or snapshot.get('expired') is True:
            return False
        sampled = _epoch(snapshot.get('captured_at')) or _epoch(value.get('sampled_at'))
    else:
        sampled = _epoch(value.get('sampled_at'))
    return sampled is not None and 0 <= now - sampled <= FRESH_SECONDS


def _entitlement_end(value):
    for container, field in (('subscription', 'end_at'), ('plan', 'ends_at')):
        block = value.get(container)
        if isinstance(block, dict):
            end = _epoch(block.get(field))
            if end is not None:
                return end
    return None


def _is_funding_window(window):
    """Whether a window is the allowance's spendable funding pool.

    Short-cycle guards (5-hour, daily, weekly) only constrain how fast a
    funding pool can be spent; they cannot refill it. Ark's AFPMonthly,
    Alibaba's Credits(...) pool and Monthly personal allowance are funding
    pools. A zero short-cycle guard beside a positive monthly pool is a
    temporary constraint, not terminal exhaustion.
    """
    if not isinstance(window, dict):
        return False
    name = window.get('name')
    name = name if isinstance(name, str) else ''
    kind = window.get('kind')
    kind = kind if isinstance(kind, str) else ''
    if name.startswith('Credits'):
        return True
    if name in ('AFPMonthly', 'Monthly', 'monthTotal', 'month_total'):
        return True
    return kind in ('month_total', 'monthly')


def _window_empty(window):
    """True only for a valid, complete, known-zero window.

    A percentage-only Personal window (``unit='percent'``, no amount) is
    complete with a zero percentage. A Credits window whose amount is missing
    is unknown even when a redundant percentage says zero.
    """
    if not isinstance(window, dict) or window.get('valid') is not True:
        return False
    if window.get('expired') is True or window.get('credits_service_degraded') is True:
        return False
    amount = _number(window.get('remaining'))
    percent = _number(window.get('remaining_percent'))
    if amount is None and percent is None:
        return False
    if amount is not None and amount != 0:
        return False
    if percent is not None and percent != 0:
        return False
    if amount is None and window.get('unit') != 'percent' and 'remaining' in window:
        return False
    if percent is None and window.get('unit') == 'percent':
        return False
    return True


def _addon_empty(value, personal):
    addon = value.get('addon')
    if addon is None:
        # Team has no add-on product; a Personal plan without a summary is
        # incomplete evidence and stays visible.
        return not personal
    if not isinstance(addon, dict):
        return False
    if addon.get('known') is not True or addon.get('inconsistent') is True:
        return False
    remaining = _number(addon.get('remaining'))
    if remaining is None or remaining != 0:
        return False
    percent = _number(addon.get('remaining_percent'))
    return percent is None or percent == 0


def _balance_empty(value):
    """Prepaid cash is terminal only when every known balance is exactly zero."""
    balances = value.get('balances')
    if not isinstance(balances, list) or not balances or value.get('windows'):
        return False
    for item in balances:
        if not isinstance(item, dict):
            return False
        remaining = _number(item.get('remaining'))
        if remaining is None or remaining != 0:
            return False
    return True


def terminal_exhausted(value, provider=None, now=None):
    """Whether this current allowance must disappear from every display.

    ``provider`` is the canonical provider id (or ``aliyun-team`` /
    ``aliyun-personal`` for a sequential sub-plan). The caller keeps the raw
    record; this function only answers the display question.
    """
    now = time.time() if now is None else now
    if not _fresh(value, now) or value.get('available') is not False:
        return False
    if provider == 'deepseek':
        return _balance_empty(value)
    personal = provider in ('aliyun-personal', 'bailian-personal') or \
        value.get('plan_edition') == 'personal'
    if isinstance(value.get('addon'), dict) or personal:
        if not _addon_empty(value, personal):
            return False
    end = _entitlement_end(value)
    if end is None:
        return False
    windows = value.get('windows')
    if not isinstance(windows, list) or not windows:
        return False

    def terminal_window(window):
        if not _window_empty(window):
            return False
        reset = _epoch(window.get('resets_at'))
        return reset is not None and reset >= end - RESET_TOLERANCE_SECONDS

    funding = [window for window in windows if _is_funding_window(window)]
    if funding:
        # A funding pool is the spendable balance. Short-cycle guards may still
        # show a positive remainder, but a 5-hour/weekly reset cannot recreate
        # an exhausted monthly pool before the paid term ends.
        return all(terminal_window(window) for window in funding)
    # No distinguishable funding pool: require every reported window to be
    # terminal, which is the conservative all-pools rule.
    return all(terminal_window(window) for window in windows)
