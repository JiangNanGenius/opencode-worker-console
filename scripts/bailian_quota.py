"""Alibaba Token Plan live quota adapter (ModelStudio OpenAPI GetSubscriptionStats).

Read-only control-plane query, response shape and authentication were verified
against the official API on 2026-10-17:

* ``GET https://modelstudio.cn-beijing.aliyuncs.com/tokenplan/subscription/stats``
  with ``x-acs-action: GetSubscriptionStats`` and ``x-acs-version: 2026-02-10``
  (ACS3-HMAC-SHA256; the transport and signer live in ``aliyun_openapi``).
* Success envelope: ``{"Success": true, "Code": "200", "Message": "successful",
  "Data": {"SubscriptionStartTime": ..., "SubscriptionEndTime": ...,
  "Items": [{"SeatType": "pro", "SeatRefreshTime": ..., "TotalSeats": 1,
  "AssignedSeats": 1, "SeatCredits": 100000.0, "SeatRemainingCredits": ...}]}}``.
* Epoch fields arrive in **milliseconds** in the live response even though some
  official examples show seconds; both units are normalized defensively.
* ``SeatCredits``/``SeatRemainingCredits`` are per seat-type group values. With one
  seat they are the pool directly; with multiple seats they are the group aggregate
  as documented, so ``TotalSeats`` is never used as a multiplier.
  ``SeatRemainingCredits`` can be null when the CreditsService is degraded: that
  window is reported unknown, never zero.

Seat ledger: independent seat groups that share the same ``SeatRefreshTime`` are
one entitlement for that cycle. Their Credits add up, so the ledger exposes a
single combined ``Credits(pool)`` window (sum of limits and remainings, the common
reset) plus the untouched per-group rows in ``seat_groups`` outside ``windows``.
The minimum of independent groups is not the subscription remaining, and a group
exhausted early must not hide another group's usable Credits. Groups with a
different or missing refresh are different cycles and are never guessed into the
sum; an unknown group remaining makes the summed remaining unknown, never a
partial number. A single reported group keeps the legacy ``Credits(<seat>)``
window unchanged.

Credential boundary: the Alibaba account AccessKey pair is separate from the
Token Plan inference API key (data plane) and from Volcengine control credentials.
Values are resolved in-process by ``aliyun_openapi`` (registered references
``aliyun-control-ak``/``aliyun-control-sk``, then the standard SDK environment
pair) and never appear in returned metadata or errors.

This adapter is the live source for the Alibaba row of the shared quota cache.
A failure never fabricates data and never advances a sample time: the caller
(quota.refresh) preserves the last genuine reading or falls back to the manual
snapshot anchor, each with an explicit freshness/source label.
"""
import math
import re
from datetime import datetime, timezone
import time

import common
import providers

HOST = 'modelstudio.cn-beijing.aliyuncs.com'
ACTION = 'GetSubscriptionStats'
VERSION = '2026-02-10'
SOURCE_LIVE = 'openapi_live'
# Gateway statuses that mean the credential/signature itself was rejected for
# this fixed read-only request: the AK does not exist, the signature mismatched,
# the date header is missing, or RAM permission is absent.
_AUTH_STATUSES = frozenset({400, 401, 403, 404})
_WINDOW_CAP = 8
_MAX_DETAIL = 160
_SEAT_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
POOL_WINDOW = 'Credits(pool)'


def resolve_credentials():
    """(ak, sk, source) from the transport module; values never leave it."""
    import aliyun_openapi
    return aliyun_openapi.resolve_credentials()


def credential_identity():
    """Private identity bound to both credential parts, or None when absent."""
    import aliyun_openapi
    return aliyun_openapi.credential_identity()


def credential_source():
    """Safe availability label only: 'credential_reference', 'environment' or None."""
    import aliyun_openapi
    return aliyun_openapi.credential_source()


def _call(access_key, secret_key):
    import aliyun_openapi
    return aliyun_openapi.call(access_key, secret_key)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _epoch(value):
    """Epoch seconds from a live field that may use seconds or milliseconds.

    The official docs showed seconds in examples while the verified live response
    used milliseconds, so values above 1e12 are treated as milliseconds. Anything
    outside a sane window (2001..2100) is unknown, not guessed.
    """
    raw = _number(value)
    if raw is None or raw <= 0:
        return None
    seconds = raw / 1000.0 if raw >= 1e12 else raw
    return seconds if 946_684_800 <= seconds <= 4_102_444_800 else None


def _iso(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _iso_seconds(value):
    """Aware-ISO epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def positive_live_proof(value, now=None):
    """True only for a complete fresh live reading usable as recovery evidence.

    Every reported seat-type window must be a valid, strictly positive Credits
    window whose reset lies strictly in the future. A degraded (null-credits),
    expired, zero or malformed window is never proof that the account recovered,
    and a display fallback carrying live_error is never proof at all.
    """
    now = time.time() if now is None else now
    if not isinstance(value, dict) or value.get('state') != 'ok' or \
            value.get('source') != SOURCE_LIVE or value.get('available') is not True or \
            isinstance(value.get('live_error'), dict) or isinstance(value.get('snapshot'), dict):
        return False
    windows = value.get('windows')
    if not isinstance(windows, list) or not windows:
        return False
    for window in windows:
        if not isinstance(window, dict) or window.get('valid') is not True or \
                window.get('expired') is True or window.get('credits_service_degraded') is True:
            return False
        remaining = _number(window.get('remaining'))
        if remaining is None or remaining <= 0:
            return False
        reset = _iso_seconds(window.get('resets_at'))
        if reset is None or reset <= now:
            return False
    return True


def _seat_window(row, now, cycle_start=None):
    """One Credits window per seat-type group; unknown stays unknown, never 0.

    duration_minutes is the full cycle length (SubscriptionStartTime to this
    seat group's SeatRefreshTime), never the remaining time: consumers compute
    elapsed burn as duration minus remaining runway, so a now-relative value
    would reset the elapsed clock on every sample. With no authoritative start
    the duration stays unknown instead of being invented.
    """
    if not isinstance(row, dict):
        return None
    seat = row.get('SeatType')
    name = seat if isinstance(seat, str) and _SEAT_NAME.match(seat) else 'unknown'
    limit = _number(row.get('SeatCredits'))
    remaining = _number(row.get('SeatRemainingCredits'))
    refresh = _epoch(row.get('SeatRefreshTime'))
    seats = {}
    for key, field in (('total', 'TotalSeats'), ('assigned', 'AssignedSeats')):
        count = _number(row.get(field))
        if count is not None and 0 <= count <= 1_000_000:
            seats[key] = int(count)
    expired = bool(refresh is not None and refresh <= now)
    valid = (limit is not None and limit > 0 and remaining is not None
             and 0 <= remaining <= limit and not expired)
    duration = None
    if refresh is not None and cycle_start is not None and refresh > cycle_start:
        duration = max(1.0, min(525_600.0, (refresh - cycle_start) / 60))
    window = {'name': 'Credits(' + name + ')', 'seat_type': name,
              'limit': limit, 'remaining': remaining,
              'remaining_percent': round(remaining / limit * 100, 3) if valid else None,
              'duration_minutes': duration, 'resets_at': _iso(refresh), 'valid': valid}
    if expired:
        window['expired'] = True
    if seats:
        window['seats'] = seats
    if limit is not None and remaining is None:
        # CreditsService degraded for this seat group: the pool exists but its
        # remaining value is genuinely unknown right now.
        window['credits_service_degraded'] = True
    return window


def _seat_count(window, key):
    """Integer seat metadata from a group window, or None when unknown."""
    seats = window.get('seats')
    value = seats.get(key) if isinstance(seats, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _combine_seat_groups(groups):
    """One additive Credits window for independent groups on a shared cycle.

    Returns ``(pool_window, member_indexes, detail_rows)`` or ``None`` when no two
    groups share a refresh time. Only groups with the same non-null
    ``SeatRefreshTime`` are summed: the API reports each group's already-aggregate
    Credits, so the subscription entitlement is their sum, never a multiplication
    by ``TotalSeats`` and never the minimum of the groups. A null remaining in any
    member keeps the entitlement but makes the summed remaining unknown instead of
    a partial number, and any invalid/out-of-range/expired member makes the
    combined window invalid rather than hiding it behind a plausible sum.
    """
    buckets = {}
    for index, window in enumerate(groups):
        reset = window.get('resets_at')
        if reset is not None:
            buckets.setdefault(reset, []).append(index)
    best = None
    for reset, indexes in buckets.items():
        if len(indexes) >= 2 and (best is None or len(indexes) > len(best[1])):
            best = (reset, indexes)
    if best is None:
        return None
    reset, indexes = best
    members = [groups[index] for index in indexes]
    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    limits = [w.get('limit') for w in members]
    remainings = [w.get('remaining') for w in members]
    # The entitlement (sum of limits) stays known even when some group's
    # remaining is genuinely unknown; only remaining/percentage become unknown.
    limit = sum(limits) if all(numeric(value) for value in limits) else None
    remaining = sum(remainings) if all(numeric(value) for value in remainings) else None
    expired = any(w.get('expired') is True for w in members)
    member_valid = all(w.get('valid') is True for w in members)
    durations = [w.get('duration_minutes') for w in members]
    duration = durations[0] if all(value == durations[0] for value in durations) else None
    # A combined total is only usable when every member row is itself valid and
    # current: an out-of-range or degraded group is never hidden by summation.
    valid = (member_valid and not expired and limit is not None and limit > 0
             and remaining is not None and 0 <= remaining <= limit)
    window = {'name': POOL_WINDOW, 'limit': limit, 'remaining': remaining,
              'remaining_percent': round(remaining / limit * 100, 3) if valid else None,
              'duration_minutes': duration, 'resets_at': reset, 'valid': valid,
              'seat_count': len(members)}
    seat_types = [w.get('seat_type') for w in members]
    if all(isinstance(value, str) and value for value in seat_types):
        window['seat_types'] = seat_types
    totals = [_seat_count(w, 'total') for w in members]
    if all(value is not None for value in totals):
        window['seats'] = {'total': sum(totals)}
        assigned = [_seat_count(w, 'assigned') for w in members]
        if all(value is not None for value in assigned):
            window['seats']['assigned'] = sum(assigned)
    if expired:
        window['expired'] = True
    if not member_valid:
        window['invalid_member'] = True
    if any(w.get('credits_service_degraded') is True for w in members):
        window['credits_service_degraded'] = True
    detail = []
    for member in members:
        row = dict(member)
        if not isinstance(row.get('seat_type'), str):
            row['seat_type'] = 'unknown'
        detail.append(row)
    return window, set(indexes), detail


def normalize_stats(body, now=None):
    """Normalize the official GetSubscriptionStats envelope into quota shape.

    Raises ValueError on any structurally invalid payload: a malformed response
    must never be displayed as an account state. Independent seat groups on the
    same cycle are summed into one ``Credits(pool)`` window and their untouched
    rows stay in ``seat_groups``; a single group keeps its legacy window.
    """
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        raise ValueError('Invalid quota payload')
    if body.get('Success') is not True:
        raise ValueError('Unsuccessful quota envelope')
    data = body.get('Data')
    if not isinstance(data, dict):
        raise ValueError('Quota payload has no subscription data')
    items = data.get('Items')
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError('Quota seat list has an unexpected shape')
    start = _epoch(data.get('SubscriptionStartTime'))
    end = _epoch(data.get('SubscriptionEndTime'))
    groups = []
    for row in items[:_WINDOW_CAP]:
        window = _seat_window(row, now, start)
        if window is not None:
            groups.append(window)
    combined = _combine_seat_groups(groups) if len(groups) > 1 else None
    seat_groups = None
    if combined is not None:
        pool, member_indexes, seat_groups = combined
        windows = [pool] + [window for index, window in enumerate(groups)
                            if index not in member_indexes]
    else:
        windows = groups
    valid = [w for w in windows if w['valid']]
    # Availability requires every reported row to have produced a window; the
    # combined window covers all its members, so window count is not item count.
    covered = len(groups) == len(items) and len(items) <= _WINDOW_CAP
    available = None
    if any(w['remaining'] == 0 for w in valid):
        available = False
    elif any(w.get('name') == POOL_WINDOW and w.get('remaining') == 0 for w in windows):
        # A combined ledger reporting exactly zero is exhausted even when a
        # member row is expired/invalid: no reported seat has Credits left.
        available = False
    elif valid and len(valid) == len(windows) and covered:
        available = True
    result = {'windows': windows, 'balances': [], 'available': available,
              'subscription': {'start_at': _iso(start), 'end_at': _iso(end)}}
    if seat_groups:
        result['seat_groups'] = seat_groups
    return result


def _error_detail(error):
    message = getattr(error, 'message', None)
    if not isinstance(message, str) or not message:
        return None
    return message[:_MAX_DETAIL]


def _state_for(status):
    if status in _AUTH_STATUSES:
        return 'auth_error'
    return 'rate_limited' if status == 429 else 'unavailable'


def fetch():
    """Live control-plane sample shaped like the other quota fetch adapters.

    Returns a dict in the shape quota.refresh stores, plus source and credential
    metadata. Credentials never appear in the result. Every failure is a state
    dict: nothing here raises, fabricates a sample time or reports recovery.
    """
    now = time.time()
    try:
        access_key, secret_key, source = resolve_credentials()
    except Exception:
        return {'state': 'no_credential', 'attempted_at': now, 'credential_source': None,
                'note': 'Alibaba control-plane credential resolution failed; live quota unknown. '
                        'Inference (data plane) is unaffected.'}
    if not access_key or not secret_key:
        return {'state': 'no_credential', 'attempted_at': now, 'credential_source': None,
                'note': 'Alibaba control-plane AK/SK are not registered; live quota unknown. '
                        'Inference (data plane) is unaffected.'}
    try:
        body = _call(access_key, secret_key)
    except common.HttpFailure as e:
        state = _state_for(e.status)
        if e.status is None and 'unreachable' in str(e.message or ''):
            state = 'unavailable'
        value = {'state': state, 'attempted_at': now, 'credential_source': source}
        if e.status is not None:
            value['http_status'] = e.status
        detail = _error_detail(e)
        if detail:
            value['live_error'] = {'state': state, 'attempted_at': now, 'http_status': e.status,
                                   'detail': detail}
        return value
    except ValueError:
        return {'state': 'unknown', 'attempted_at': now, 'credential_source': source,
                'live_error': {'state': 'unknown', 'attempted_at': now,
                               'detail': 'Alibaba quota response could not be read'}}
    try:
        value = normalize_stats(body, now)
    except ValueError:
        return {'state': 'unknown', 'attempted_at': now, 'credential_source': source,
                'live_error': {'state': 'unknown', 'attempted_at': now,
                               'detail': 'Alibaba quota response has an unexpected shape'}}
    value.update(state='ok', sampled_at=now, source=SOURCE_LIVE,
                 credential_source=source, action=ACTION, host=HOST)
    return value
