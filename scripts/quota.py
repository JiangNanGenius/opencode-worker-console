"""Read-only account telemetry and provider billing circuit; selection stays with the coordinator."""
import calendar
import math
import re
from datetime import datetime, timezone
import time
from concurrent.futures import ThreadPoolExecutor
from common import config, STATE, HttpFailure, auth_key, credential_identity, locked, read_json, redact, request, write_json
from providers import ARK_PROVIDER, BAILIAN_PROVIDER
import ark_quota
import bailian_quota
import routing

BUDGET_MODES = {'manual_window', 'monetary', 'prepaid', 'ignore'}


def validate_budget_signals(value, profiles):
    """Validate optional guidance-only budget sources for providers without telemetry."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError('budget_signals must be an object with at most 32 providers')
    known = {str(p.get('model', '')).split('/', 1)[0] for p in (profiles or {}).values()
             if isinstance(p, dict) and '/' in str(p.get('model', ''))}
    out = {}
    for provider, raw in value.items():
        if not isinstance(provider, str) or provider not in known or not isinstance(raw, dict):
            raise ValueError('budget_signals provider must be configured')
        mode = raw.get('mode')
        if mode not in BUDGET_MODES:
            raise ValueError('budget signal mode must be manual_window, monetary, prepaid or ignore')
        item = {'mode': mode}
        if mode == 'manual_window':
            remaining = raw.get('remaining_percent')
            duration = raw.get('duration_minutes')
            reset = raw.get('resets_at')
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or \
                    not math.isfinite(remaining) or not 0 <= remaining <= 100:
                raise ValueError('manual window remaining_percent must be between 0 and 100')
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or \
                    not math.isfinite(duration) or not 1 <= duration <= 525600:
                raise ValueError('manual window duration_minutes must be between 1 and 525600')
            try:
                parsed_reset = datetime.fromisoformat(reset.replace('Z', '+00:00')) \
                    if isinstance(reset, str) and len(reset) <= 64 else None
            except ValueError:
                parsed_reset = None
            if parsed_reset is None or parsed_reset.tzinfo is None or routing._iso_seconds(reset) is None:
                raise ValueError('manual window resets_at must be ISO-8601 with an explicit time zone')
            item.update(remaining_percent=float(remaining), duration_minutes=float(duration),
                        resets_at=reset)
        elif mode == 'monetary':
            currency = raw.get('currency')
            threshold = raw.get('low_balance')
            manual = raw.get('manual_balance')
            if not isinstance(currency, str) or not re.match(r'^[A-Z]{3,8}$', currency):
                raise ValueError('monetary budget currency must use uppercase letters')
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or \
                    not math.isfinite(threshold) or threshold < 0:
                raise ValueError('monetary low_balance must be non-negative')
            if manual is not None and (isinstance(manual, bool) or not isinstance(manual, (int, float)) or
                                       not math.isfinite(manual) or manual < 0):
                raise ValueError('manual_balance must be non-negative when supplied')
            item.update(currency=currency, low_balance=float(threshold))
            if manual is not None:
                item['manual_balance'] = float(manual)
        out[provider] = item
    return out


def normalized_budget_signals(value, profiles):
    try:
        return validate_budget_signals(value, profiles)
    except ValueError:
        return {}


def prepaid_providers(c):
    """Providers whose capacity is refillable prepaid credit, not a use-it-or-lose-it allowance.

    The mode is provider-scoped inside the existing budget_signals record, so no
    extra top-level setting is required. It changes calendar conservation and
    recovery only: live/positive capacity, expiry, credential and billing gates
    still apply through allowed(), trip() and billing_block().
    """
    if not isinstance(c, dict):
        return set()
    try:
        settings = normalized_budget_signals(c.get('budget_signals'), c.get('profiles'))
    except Exception:
        settings = {}
    return {provider for provider, item in settings.items()
            if isinstance(item, dict) and item.get('mode') == 'prepaid'}


def provider_is_prepaid(provider, c=None):
    """True for a provider explicitly configured with the prepaid budget mode."""
    try:
        return provider in prepaid_providers(config() if c is None else c)
    except Exception:
        return False


def _telemetry_identity(provider):
    """Identity of the credential the quota sampler actually uses.

    Bearer-token providers sample with the same OpenCode API key the data plane
    uses, so the shared credential_identity applies. Ark's control plane is a
    separate account AccessKey: the cache and its fresh-sample clearing use the
    AccessKey hash so a rotated control credential invalidates stale telemetry.
    Billing/model-error circuits deliberately stay bound to the OpenCode
    inference credential (common.credential_identity) via trip/billing_block;
    this helper never feeds them, so an inference-key rotation is never
    mis-attached to a control-plane identity.
    """
    if provider == ARK_PROVIDER:
        return ark_quota.credential_identity()
    if provider == BAILIAN_PROVIDER:
        return bailian_quota.credential_identity()
    return credential_identity(provider)

try:
    import zoneinfo
except ImportError:  # pragma: no cover - Python < 3.9; schedules fail closed without IANA zones.
    zoneinfo = None

ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/user/balance',
    'kimi-for-coding': 'https://api.kimi.com/coding/v1/usages',
}

# Control-plane (AK/SK signed) providers use a private fetch instead of the
# bearer-token ENDPOINTS path; credentials never travel through this module.
# Alibaba Token Plan queries the official ModelStudio GetSubscriptionStats
# OpenAPI (separate account AccessKeys from the Volcengine pair and from the
# Token Plan inference API key), so its row is live telemetry, not a frozen
# manual number. Its manual snapshot import stays allowed as the fallback
# anchor for the window before the first genuine live reading.
CONTROL_PLANE = {ARK_PROVIDER: ark_quota.fetch, BAILIAN_PROVIDER: bailian_quota.fetch}


BILLING = 'billing.json'
HISTORY = 'quota-history.json'
SNAPSHOTS = 'quota-snapshots.json'
# Manual/imported snapshot support for subscription pools: a snapshot is an
# observed console reading with its own capture time and freshness horizon. It
# is never fabricated as live telemetry, an aging snapshot reads as unknown, and
# a recorded depletion survives a reset clock or missing telemetry until a newer
# positive reading proves replenishment. Alibaba Token Plan now also has the
# official live Credits query (ModelStudio GetSubscriptionStats): the snapshot
# import stays accepted there purely as the fallback anchor and baseline before
# a first genuine live reading, while a fresh live reading always takes
# precedence over the stored snapshot.
SNAPSHOT_WINDOW = 'CreditsPool'
SNAPSHOT_FRESH_DEFAULT_MINUTES = 1440
SNAPSHOT_MAX_FRESH_MINUTES = 10080
HISTORY_RETENTION_SECONDS = 35 * 86400
HISTORY_MAX_SAMPLES = 720
MONTHLY_REASON = 'monthly_usage_limit'
BALANCE_REASON = 'insufficient_balance'
# A confirmed model-origin credential rejection (HTTP 401) is bound to the
# inference credential, never to a quota-query AK/SK pair. It is not a billing
# reason: the fix is to check/replace the inference key, not to buy capacity.
AUTH_REASON = 'credential_rejected'
GENERIC_REASON = 'billing_error'
# Explicit short usage-window exhaustion (for example a 5-hour limit). It is a real,
# self-resolving window, not a monthly plan block and not a durable billing circuit.
WINDOW_REASON = 'usage_window_limit'
REASON_SEVERITY = {GENERIC_REASON: 1, AUTH_REASON: 2, BALANCE_REASON: 2, MONTHLY_REASON: 3}
MANUAL_RETRY_NOTE = ('Manual retry authorization: releases the block so new attempts may run until a further '
                     'billing error re-opens it. It is not proof the provider quota was restored. Seen message '
                     'IDs and recovery watermarks are preserved, and a retry that is still exhausted re-blocks.')
MANUAL_RETRY_WARNING = 'Not proven recovery; new attempts may still fail while the provider is exhausted.'
# Kimi's per-window usage ratios and the duration each one represents. Only these
# known keys are accepted as a fallback, so an unknown key is never guessed into a
# window; the duration also deduplicates the ratio against the same limits row.
KNOWN_RATIO_WINDOWS = {'limit_5h': 300, 'limit_7d': 10080}
# The overall usage aggregate already represents the weekly window in this endpoint.
# A valid aggregate stays authoritative and suppresses the duplicate ratio card.
WEEKLY_RATIO_KEYS = {'limit_7d'}
MONTHLY_RESET_KEY = 'kimi_monthly_reset'
MONTHLY_RESET_DEFAULTS = {'enabled': False, 'day': 1, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
SCHEDULED_RESET_RELEASE = 'scheduled_reset'
MODEL_SUCCESS_RELEASE = 'model_success'
SNAPSHOT_RELEASE = 'snapshot_replenishment'
# The official Alibaba account API (ModelStudio GetSubscriptionStats) reports
# authoritative Credits remaining for the whole subscription, so a fresh
# positive unexpired reading is genuine recovery evidence even for monthly
# exhaustion. That is specific to this account-wide source: other providers'
# window telemetry (for example Kimi) keeps reporting window allowances while a
# hidden monthly pool is exhausted, so their MONTHLY_REASON behavior is
# deliberately unchanged and this evidence kind is never produced for them.
API_RELEASE = 'openapi_account_recovery'
_MONTHLY_TIME = re.compile(r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


def _epoch(value):
    """Positive finite epoch seconds from untrusted state, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _zone(name):
    if zoneinfo is None:
        raise ValueError('zoneinfo unavailable')
    return zoneinfo.ZoneInfo(name)


def valid_timezone(name):
    """True only for an IANA zone the host can resolve; never the host's local zone."""
    if not isinstance(name, str) or not name or len(name) > 128 or name.startswith('/') or '..' in name:
        return False
    try:
        _zone(name)
        return True
    except Exception:
        return False


def valid_monthly_time(value):
    return isinstance(value, str) and bool(_MONTHLY_TIME.match(value))


def normalize_monthly_schedule(value):
    """Fail-closed normalization of a stored schedule.

    Malformed or legacy fields fall back to generic defaults and disable the schedule, so an
    invalid record can never release a block. A personal choice such as day 19 is only ever
    active when it was explicitly stored and validated; it is never a public default.
    """
    defaults = dict(MONTHLY_RESET_DEFAULTS)
    if not isinstance(value, dict):
        return defaults
    day = value.get('day')
    time_text = value.get('time')
    zone_name = value.get('timezone')
    enabled = (value.get('enabled') is True and isinstance(day, int) and not isinstance(day, bool)
               and 1 <= day <= 31 and valid_monthly_time(time_text) and valid_timezone(zone_name))
    return {'enabled': enabled,
            'day': day if isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31
                   else defaults['day'],
            'time': time_text if valid_monthly_time(time_text) else defaults['time'],
            'timezone': zone_name if valid_timezone(zone_name) else defaults['timezone']}


def validate_monthly_schedule(value):
    """Strict settings-API validation; raises ValueError and returns a normalized copy."""
    if not isinstance(value, dict):
        raise ValueError('kimi_monthly_reset must be an object')
    enabled = value.get('enabled')
    if not isinstance(enabled, bool):
        raise ValueError('kimi_monthly_reset.enabled must be boolean')
    day = value.get('day')
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise ValueError('kimi_monthly_reset.day must be an integer between 1 and 31')
    time_text = value.get('time')
    if not valid_monthly_time(time_text):
        raise ValueError('kimi_monthly_reset.time must be 24-hour HH:MM')
    zone_name = value.get('timezone')
    if not valid_timezone(zone_name):
        raise ValueError('kimi_monthly_reset.timezone must be a valid IANA time zone')
    return {'enabled': enabled, 'day': day, 'time': time_text, 'timezone': zone_name}


def configured_monthly_schedule():
    """Normalized schedule from configuration; any read failure fails closed to disabled."""
    try:
        return normalize_monthly_schedule(config().get(MONTHLY_RESET_KEY))
    except Exception:
        return dict(MONTHLY_RESET_DEFAULTS)


def _month_shift(year, month, delta):
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _boundary_epoch(schedule, year, month):
    """Epoch seconds of the clamped monthly boundary in the schedule's own IANA zone."""
    zone = _zone(schedule['timezone'])
    day = min(schedule['day'], calendar.monthrange(year, month)[1])
    return datetime(year, month, day, int(schedule['time'][:2]), int(schedule['time'][3:]), tzinfo=zone).timestamp()


def monthly_boundaries(now, schedule):
    """(most recent elapsed, next) boundary epochs for a normalized enabled schedule."""
    zone = _zone(schedule['timezone'])
    local = datetime.fromtimestamp(now, tz=zone)
    current = _boundary_epoch(schedule, local.year, local.month)
    if current > now:
        year, month = _month_shift(local.year, local.month, -1)
        return _boundary_epoch(schedule, year, month), current
    year, month = _month_shift(local.year, local.month, 1)
    return current, _boundary_epoch(schedule, year, month)


def monthly_reset_view(now=None):
    """Read-only schedule exposure for quota consumers; next_reset_at is ISO8601 when enabled."""
    schedule = configured_monthly_schedule()
    out = dict(schedule)
    out['next_reset_at'] = None
    if schedule['enabled']:
        try:
            _, following = monthly_boundaries(time.time() if now is None else now, schedule)
            out['next_reset_at'] = datetime.fromtimestamp(following, tz=timezone.utc).isoformat()
        except Exception:
            out['enabled'] = False
    return out


def trip(provider, message='', message_id=None, occurred_at=None, reason=None,
         identity=None):
    """Open the provider billing circuit on an unequivocal model billing error.

    Durable and credential-aware: the block binds to the supplied dispatch
    credential when one is recorded and still current (falling back to the
    current credential for legacy callers); a stale old-key identity is ignored. Each seen message ID is retained without eviction, and an
    occurrence watermark at recovery ignores errors predating it, so an old
    session error can never reopen a recovered circuit. The reason kind separates
    a replenishable balance failure from hidden monthly plan exhaustion, which
    fresh usage telemetry must never clear.
    """
    if identity is not None:
        # An explicitly recorded dispatch identity is authoritative. If the key
        # has since been replaced, this is a late old-key failure: never bind it
        # to (or overwrite a circuit for) the new key.
        if not isinstance(identity, str) or not identity or credential_identity(provider) != identity:
            return
        ident = identity
    else:
        ident = credential_identity(provider)
    if reason not in REASON_SEVERITY:
        reason = GENERIC_REASON
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider, {})
        ids = b.get('message_ids', [])
        if message_id and message_id in ids:
            return  # Already recorded; never relatch from a historical error.
        watermark = max(_epoch(b.get('cleared_at')) or 0,
                        _epoch(b.get('recovery_watermark')) or 0)
        if occurred_at and watermark and occurred_at <= watermark:
            return  # Predates the recovery watermark.
        if not b.get('cleared_at') and b.get('credential') == ident and \
                REASON_SEVERITY.get(b.get('reason'), 0) > REASON_SEVERITY[reason]:
            # An active monthly block is never downgraded by a generic sibling error.
            reason = b['reason']
        if message_id:
            ids = ids + [message_id]
        blocks[provider] = {'credential': ident, 'opened_at': time.time(),
                            'message': redact(str(message))[:400], 'message_ids': ids,
                            'reason': reason}
        if b.get('credential') == ident:
            # Rejection after an authorized recovery attempt must retain its
            # backoff; a fresh positive telemetry sample is not permission to
            # reopen the same failed attempt every sampler tick.
            for name in ('recovery_retry_count', 'recovery_retry_at', 'recovery_retry_after'):
                if name in b:
                    blocks[provider][name] = b[name]
            if watermark:
                blocks[provider]['recovery_watermark'] = watermark
        write_json(STATE / BILLING, blocks)


def billing_block(provider):
    """Active billing block for the provider's current credential, or None."""
    b = read_json(STATE / BILLING, {}).get(provider)
    if not b or b.get('cleared_at'):
        return None
    ident = credential_identity(provider)
    if ident is not None and b.get('credential') is not None and b['credential'] != ident:
        return None  # Credential rotated; the circuit binds to the old credential only.
    return b


def clear(provider, sampled_since=None, identity=None, evidence='quota', evidence_at=None):
    """Close the circuit on fresh verified evidence; keep seen message IDs and watermarks.

    evidence is 'quota' (fresh positive account query), 'manual_retry' (explicit local
    authorization) or 'snapshot_replenishment' (a newer manually imported positive
    balance snapshot). Monthly plan exhaustion never clears with 'quota': the plan's usage
    endpoint keeps reporting window allowance while the monthly cycle is exhausted, so
    quota telemetry is never accepted as evidence of monthly recovery, and reported reset
    timestamps are never parsed into a recovery inference. A snapshot may clear a monthly
    block only when it was captured strictly after the block opened. Monthly blocks
    instead use the separate, provenance-guarded paths scheduled_release (configured
    local-zone boundary) and observe_model_success (verified completed model response);
    neither is inferred from telemetry or from unverified session replies. Prepaid
    providers accept only manual_retry here; their account aggregate and manual
    snapshots may describe a different seat than the current inference key.

    sampled_since/identity guard the race where an account query started before a
    concurrent trip: a stale in-flight sample must never clear a newer circuit.
    """
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        if b.get('reason') == AUTH_REASON and evidence != 'manual_retry':
            return False  # Account telemetry does not prove model-key permission.
        # Prepaid capacity is cash-like and its account aggregate may include a
        # different seat than the one behind the current inference key. A manual
        # retry, a genuine key-file change (which detaches the credential-bound
        # block) or a verified current-key model success are the only prepaid
        # recovery paths; telemetry and imported snapshots never prove that an
        # exhausted/rejected inference key started working again.
        if evidence != 'manual_retry' and provider_is_prepaid(provider):
            return False
        if b.get('reason') == MONTHLY_REASON and evidence != 'manual_retry':
            opened = _epoch(b.get('opened_at'))
            captured = routing._iso_seconds(evidence_at) if evidence == SNAPSHOT_RELEASE else None
            if captured is None or opened is None or captured <= opened:
                return False
        if identity is not None and b.get('credential') is not None and b['credential'] != identity:
            return False
        if sampled_since is not None and b.get('opened_at', 0) > sampled_since:
            return False
        blocks[provider] = {'credential': b.get('credential'), 'cleared_at': time.time(),
                            'message_ids': b.get('message_ids', []), 'reason': b.get('reason'),
                            'opened_at': b.get('opened_at'), 'message': b.get('message'),
                            'released': evidence}
        write_json(STATE / BILLING, blocks)
        return True


def apply_provider_retry_evidence(provider, reading, query_started_at, query_completed_at,
                                 inference_identity, control_identity=None, now=None):
    """Authorize a bounded real-work retry; never label telemetry as key success.

    The evidence predicate is provider-specific. State is re-read under the
    billing lock so a rotated key or a newer rejection wins. Retry metadata
    survives another rejection and bounds persistent telemetry/data-plane
    disagreement to 5/10/20/40/60 minute intervals.
    """
    if provider not in (ARK_PROVIDER, KIMI_PROVIDER):
        return False
    import provider_recovery
    now = time.time() if now is None else now
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        block = blocks.get(provider)
        if not isinstance(block, dict) or block.get('cleared_at'):
            return False
        current = credential_identity(provider)
        kwargs = dict(captured_inference_identity=inference_identity,
                      current_inference_identity=current,
                      query_started_at=query_started_at, query_completed_at=query_completed_at,
                      now=now, prepaid=provider_is_prepaid(provider))
        if provider == ARK_PROVIDER:
            proof = provider_recovery.ark_retry_evidence(
                block, reading, captured_control_identity=control_identity,
                current_control_identity=_telemetry_identity(provider),
                official_query=True, **kwargs)
        else:
            proof = provider_recovery.kimi_monthly_retry_evidence(block, reading, **kwargs)
        if not proof.get('allowed'):
            return False
        opened = _epoch(block.get('opened_at'))
        after = _epoch(block.get('recovery_retry_after')) or 0
        if opened is None or now < max(opened + 300, after):
            return False
        prior_at = _epoch(block.get('recovery_retry_at')) or 0
        count = block.get('recovery_retry_count', 0)
        count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        if now - prior_at > 21600:
            count = 0
        count = min(count + 1, 16)
        blocks[provider] = dict(block, cleared_at=now, released=proof['evidence_kind'],
                                recovery_retry_count=count, recovery_retry_at=now,
                                recovery_retry_after=now + min(300 * 2 ** count, 3600))
        write_json(STATE / BILLING, blocks)
        return True


def scheduled_release(provider, now=None):
    """Release a monthly block opened strictly before the most recent configured boundary.

    The schedule is the configured IANA-zone monthly boundary, independent of host local
    time and DST. The persisted watermark is that boundary, not this (possibly late) check
    time, so a genuine post-boundary error re-blocks for the rest of the cycle while
    earlier errors stay stale and can never relatch. Only a monthly block bound to the
    current credential is touched; this is authorization to try, never proof of recovery.
    """
    if provider != 'kimi-for-coding':
        return False
    now = time.time() if now is None else now
    schedule = configured_monthly_schedule()
    if not schedule['enabled']:
        return False
    try:
        boundary, _ = monthly_boundaries(now, schedule)
    except Exception:
        return False
    if _epoch(boundary) is None:
        return False
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at') or b.get('reason') != MONTHLY_REASON:
            return False
        opened = _epoch(b.get('opened_at'))
        if opened is None or opened >= boundary:
            return False
        # A known current credential must exactly match the block's binding; a rotated or
        # removed credential (None) never releases, and malformed legacy state fails closed.
        ident = credential_identity(provider)
        if ident is None or b.get('credential') != ident:
            return False
        blocks[provider] = dict(b, cleared_at=boundary, released=SCHEDULED_RESET_RELEASE)
        write_json(STATE / BILLING, blocks)
        return True


def account_api_recovery(provider, query_started_at, inference_identity, control_identity,
                         now=None, value=None):
    """Bailian-only: clear the billing circuit on official account-API evidence.

    GetSubscriptionStats returns the authoritative whole-subscription Credits
    remaining, so a fresh positive unexpired live reading is real recovery even
    for monthly exhaustion — unlike window telemetry on other plans, whose
    MONTHLY_REASON behavior this path never touches. Guards: the inference
    credential identity captured before dispatch must be current and equal to
    the block binding; the control AK/SK pair identity captured at dispatch must
    still match; the query must have started strictly after the circuit opened.
    The control-plane hash is never passed to the generic clear(): recovery
    binds to the inference credential exactly like trip/billing_block. Seen
    message IDs and the cleared_at watermark are preserved.
    """
    if provider != BAILIAN_PROVIDER:
        return False
    # The Alibaba account aggregate is a multi-seat group value. In prepaid mode
    # another assigned seat's positive balance must never clear a confirmed
    # current-inference-key exhaustion or rejection. A combined multi-seat ledger
    # (seat_groups) is refused below for the same reason in every budget mode. A
    # key identity change, verified current-key success or explicit operator retry
    # remains available.
    if provider_is_prepaid(provider):
        return False
    now = time.time() if now is None else now
    started = _epoch(query_started_at)
    completed = _epoch(now)
    if started is None or completed is None or completed < started or \
            not isinstance(inference_identity, str) or not inference_identity or \
            not isinstance(control_identity, str) or not control_identity:
        return False
    if not bailian_quota.positive_live_proof(value, now=completed) or \
            _epoch(value.get('sampled_at')) != started:
        return False
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        # A rejected inference key is never proven valid by account-level Credits
        # telemetry, in any budget mode: that query uses the separate AK/SK pair.
        if b.get('reason') == AUTH_REASON:
            return False
        if isinstance(value.get('seat_groups'), list) and value['seat_groups']:
            # The account total spans independent seats on a shared cycle. A
            # positive sum can come entirely from another seat, so it never
            # proves this inference credential recovered: only a verified
            # current-key model success or an explicit retry may release it.
            return False
        if credential_identity(provider) != inference_identity or \
                b.get('credential') != inference_identity:
            return False
        if bailian_quota.credential_identity() != control_identity:
            return False
        opened = _epoch(b.get('opened_at'))
        if opened is None or started <= opened:
            return False
        blocks[provider] = dict(b, cleared_at=completed, released=API_RELEASE)
        write_json(STATE / BILLING, blocks)
        return True


def snapshot_store():
    """Read-only map of imported manual snapshots keyed by provider."""
    store = read_json(STATE / SNAPSHOTS, {})
    return store if isinstance(store, dict) else {}


def _bailian_plan_record(stored):
    """An old long-lived writer may only touch quota.json, never this namespace."""
    edition = bailian_quota.plan_edition()
    if edition == 'team':
        return stored if bailian_quota.record_matches_plan(stored) else bailian_quota.unknown_record()
    saved = read_json(STATE / bailian_quota.PLAN_CACHE_FILE, {})
    candidates = [value for value in (stored, saved) if bailian_quota.record_matches_plan(value)]
    if not candidates:
        return bailian_quota.unknown_record()
    # Prefer genuine sample time, then most recent attempted read. Neither a
    # foreign shared-file timestamp nor a failed query invents quota freshness.
    return max(candidates, key=lambda value: (number(value.get('sampled_at')) or 0,
                                              number(value.get('checked_at')) or 0))


def _active_snapshots():
    """Keep legacy Team anchors stored, but never apply them to Personal work."""
    return {provider: record for provider, record in snapshot_store().items()
            if isinstance(record, dict) and
            (provider != BAILIAN_PROVIDER or
             (bailian_quota.plan_edition() != 'sequential' and bailian_quota.record_matches_plan(record)))}


def validate_snapshot(value):
    """Strict validation for a manually imported Credits/allowance snapshot.

    The payload is an observed console reading, not a live account query. It
    always carries the capture time, the reset time and a freshness horizon so
    the UI can distinguish a fresh reading from an aging one instead of showing
    a fabricated live balance.
    """
    if not isinstance(value, dict):
        raise ValueError('snapshot must be an object')
    allowed = {'remaining', 'limit', 'resets_at', 'captured_at', 'source', 'fresh_for_minutes'}
    unknown = [key for key in value if key not in allowed]
    if unknown:
        raise ValueError('snapshot contains unsupported fields: ' + ', '.join(sorted(unknown)))
    out = {}
    for key in ('remaining', 'limit'):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or \
                (raw < 0 if key == 'remaining' else raw <= 0):
            raise ValueError('snapshot.' + key + ' must be a finite number' +
                             (' greater than zero' if key == 'limit' else ' at or above zero'))
        out[key] = float(raw)
    if out['remaining'] > out['limit']:
        raise ValueError('snapshot.remaining cannot exceed snapshot.limit')
    reset = value.get('resets_at')
    if not isinstance(reset, str) or len(reset) > 64 or routing._iso_seconds(reset) is None or \
            datetime.fromisoformat(reset.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('snapshot.resets_at must be ISO-8601 with an explicit time zone')
    out['resets_at'] = reset
    captured = value.get('captured_at')
    if captured is not None:
        if not isinstance(captured, str) or len(captured) > 64 or routing._iso_seconds(captured) is None or \
                datetime.fromisoformat(captured.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('snapshot.captured_at must be ISO-8601 with an explicit time zone')
        out['captured_at'] = captured
    source = value.get('source')
    if source is not None:
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ValueError('snapshot.source must be non-empty text of at most 200 characters')
        out['source'] = redact(source.strip())[:200]
    fresh = value.get('fresh_for_minutes')
    if fresh is not None:
        if isinstance(fresh, bool) or not isinstance(fresh, int) or \
                not 5 <= fresh <= SNAPSHOT_MAX_FRESH_MINUTES:
            raise ValueError('snapshot.fresh_for_minutes must be an integer between 5 and ' +
                             str(SNAPSHOT_MAX_FRESH_MINUTES))
        out['fresh_for_minutes'] = fresh
    return out


def _snapshot_record(provider, snapshot, now=None):
    """Telemetry-shaped record for one manual snapshot; freshness stays explicit.

    A reading whose reset time has already passed belongs to the previous
    subscription period: it is reported stale and its window stops being valid,
    so positive old-period data can never look like current healthy allowance.
    A recorded depletion stays latched regardless of age, reset clock or missing
    telemetry until a newer positive snapshot arrives.
    """
    now = time.time() if now is None else now
    captured = routing._iso_seconds(snapshot.get('captured_at')) or now
    reset = routing._iso_seconds(snapshot.get('resets_at'))
    limit = number(snapshot.get('limit'))
    remaining = number(snapshot.get('remaining'))
    fresh_for = number(snapshot.get('fresh_for_minutes'))
    fresh_for = fresh_for if fresh_for and fresh_for > 0 else float(SNAPSHOT_FRESH_DEFAULT_MINUTES)
    age = max(0.0, now - captured)
    expired = bool(reset and reset <= now)
    freshness = 'fresh' if age <= fresh_for * 60 and not expired else 'stale'
    percent = round(remaining / limit * 100, 3) if limit and remaining is not None and limit > 0 else None
    # The window duration identifies the snapshot window across imports, so it
    # is persisted once per reset period instead of drifting with capture time.
    duration = number(snapshot.get('duration_minutes'))
    if duration is None or duration <= 0:
        duration = max(1.0, (reset - captured) / 60) if reset and reset > captured else 43200.0
    depleted = routing._iso_seconds(snapshot.get('depleted_at')) is not None
    if depleted:
        available = False
    elif expired:
        available = None  # Unknown, never healthy: the reading's period ended.
    else:
        available = remaining is not None and remaining > 0
    valid = bool(limit and remaining is not None and 0 <= remaining <= limit) and not expired
    window = {'name': SNAPSHOT_WINDOW, 'limit': limit, 'remaining': remaining,
              'remaining_percent': percent, 'duration_minutes': duration,
              'resets_at': snapshot.get('resets_at'), 'valid': valid,
              'snapshot': True, 'expired': expired}
    return {**({key: snapshot[key] for key in ('plan_edition', '_plan_identity')
                 if key in snapshot} if provider == BAILIAN_PROVIDER else {}),
            'state': 'ok', 'sampled_at': captured, 'checked_at': captured,
            'windows': [window], 'balances': [], 'available': available,
            'snapshot': {'captured_at': snapshot.get('captured_at'),
                         'imported_at': snapshot.get('imported_at'),
                         'age_seconds': round(age, 3),
                         'freshness': freshness,
                         'fresh_for_minutes': int(fresh_for),
                         'source': snapshot.get('source'),
                         'limit': limit, 'remaining': remaining,
                         'remaining_percent': percent,
                         'resets_at': snapshot.get('resets_at'),
                         'expired': expired,
                         'depleted_at': snapshot.get('depleted_at'),
                         'replenished_at': snapshot.get('replenished_at')}}


def import_snapshot(provider, payload, now=None):
    """Import one manual console snapshot for a provider without live telemetry.

    A non-positive reading records a durable depletion latch that only a newer
    positive snapshot can clear; a reset clock or stale/missing telemetry never
    does. A newer positive snapshot also releases a monthly billing block
    through the provenance-guarded snapshot_replenishment path.
    """
    if not isinstance(provider, str) or not provider:
        raise ValueError('Snapshot provider is required')
    if provider not in known_providers():
        raise ValueError('Unknown provider: ' + str(provider))
    if provider in ENDPOINTS or (provider in CONTROL_PLANE and provider != BAILIAN_PROVIDER):
        raise ValueError(provider + ' already has live telemetry; a manual snapshot is not accepted')
    # The Alibaba Token Plan keeps accepting manual snapshots even with the live
    # account API configured: a snapshot is the explicit pre-live baseline and
    # the displayed fallback when a live query fails, and a fresh live reading
    # always takes precedence over it in refresh/view.
    if provider == BAILIAN_PROVIDER and bailian_quota.plan_edition() == 'sequential':
        raise ValueError('Sequential plans require separate Team and Personal readings, not a combined snapshot')
    value = validate_snapshot(payload)
    now = time.time() if now is None else now
    captured = routing._iso_seconds(value.get('captured_at')) or now
    if captured > now + 300:
        raise ValueError('snapshot.captured_at cannot be in the future')
    if now - captured > 30 * 86400:
        raise ValueError('snapshot.captured_at is older than 30 days')
    captured_iso = datetime.fromtimestamp(captured, tz=timezone.utc).isoformat()
    record = {'captured_at': captured_iso, 'imported_at': now,
              'limit': value['limit'], 'remaining': value['remaining'],
              'resets_at': value['resets_at'],
              'fresh_for_minutes': value.get('fresh_for_minutes', SNAPSHOT_FRESH_DEFAULT_MINUTES),
              'source': value.get('source')}
    if provider == BAILIAN_PROVIDER:
        record['plan_edition'] = bailian_quota.plan_edition()
        if record['plan_edition'] == 'personal':
            record['_plan_identity'] = bailian_quota.credential_identity()
    reset_epoch = routing._iso_seconds(value['resets_at'])
    record['duration_minutes'] = (max(1.0, (reset_epoch - captured) / 60)
                                  if reset_epoch and reset_epoch > captured else 43200.0)
    fresh_for = record['fresh_for_minutes']
    with locked('quota'):
        store = snapshot_store()
        previous = store.get(provider) if isinstance(store.get(provider), dict) else None
        if provider == BAILIAN_PROVIDER and not bailian_quota.record_matches_plan(previous):
            previous = None
        if previous is not None and previous.get('resets_at') == value['resets_at']:
            # Same reset period: keep the window identity stable across imports.
            record['duration_minutes'] = number(previous.get('duration_minutes')) or record['duration_minutes']
        previous_depleted = previous.get('depleted_at') if previous else None
        previous_replenished = previous.get('replenished_at') if previous else None
        if previous is not None:
            # Out-of-order readings are rejected: a positive older import must
            # never erase a newer depletion latch or claim a later period. An
            # exact re-import of the same reading is an idempotent no-op.
            prior_captured = routing._iso_seconds(previous.get('captured_at'))
            if prior_captured is not None and captured <= prior_captured:
                same = (number(previous.get('remaining')) == value['remaining'] and
                        number(previous.get('limit')) == value['limit'] and
                        previous.get('resets_at') == value['resets_at'])
                if same:
                    return {'provider': provider, 'imported': False, 'reason': 'identical_snapshot',
                            'snapshot': _snapshot_record(provider, previous, now)['snapshot']}
                raise ValueError('snapshot.captured_at must be newer than the last imported reading')
        # Only a positive reading from its own still-current period and not yet
        # past its freshness horizon proves recovery. An expired-period or
        # already-stale positive reading is stored as history but never clears a
        # depletion latch or releases a billing circuit.
        current_period = bool(reset_epoch and reset_epoch > max(captured, now))
        freshness_ok = (now - captured) <= fresh_for * 60
        usable_positive = value['remaining'] > 0 and current_period and freshness_ok
        if value['remaining'] <= 0:
            record['depleted_at'] = captured_iso
            record['replenished_at'] = None
        elif usable_positive:
            record['depleted_at'] = None
            record['replenished_at'] = captured_iso
        else:
            record['depleted_at'] = previous_depleted
            record['replenished_at'] = previous_replenished
            record['recovery_evidence'] = 'unusable_reading_not_recovery'
        store[provider] = record
        write_json(STATE / SNAPSHOTS, store)
        cached = read_json(STATE / 'quota.json', {})
        cached = cached if isinstance(cached, dict) else {}
        live = cached.get(provider, {})
        live_sample = _epoch(live.get('sampled_at')) if isinstance(live, dict) else None
        keep_live = (provider == BAILIAN_PROVIDER and isinstance(live, dict) and
                     live.get('source') in (bailian_quota.SOURCE_LIVE, 'personal_console') and
                     bailian_quota.record_matches_plan(live) and
                     live_sample is not None and live_sample >= captured)
        if not keep_live:
            cached[provider] = _snapshot_record(provider, record, now)
        write_json(STATE / 'quota.json', cached)
        if provider == BAILIAN_PROVIDER and bailian_quota.plan_edition() == 'personal' and \
                bailian_quota.record_matches_plan(cached.get(provider)):
            write_json(STATE / bailian_quota.PLAN_CACHE_FILE, cached[provider])
    # In prepaid mode a manually imported snapshot is a display anchor only; it
    # cannot prove that an already-confirmed exhausted/rejected inference key was
    # replaced. A genuine key-file identity change or verified current-key success
    # is needed (the block itself detaches on identity rotation), so the import
    # never claims recovery there.
    prepaid = provider_is_prepaid(provider)
    released = False
    if usable_positive and not keep_live and not prepaid:
        released = clear(provider, evidence=SNAPSHOT_RELEASE, evidence_at=record['captured_at'])
    return {'provider': provider, 'imported': True,
            'recovery': usable_positive and not keep_live and not prepaid,
            'released': released,
            'snapshot': _snapshot_record(provider, record, now)['snapshot']}


def observe_model_success(provider, identity, started_at, completed_at):
    """Clear an eligible current-credential block after a verified completed success.

    The caller owns provenance: it passes the request credential identity captured
    privately for the actual provider plus that request's start and completion times from
    a genuinely finished, error-free model response. The request must have started strictly
    after the block opened, so a success predating the failure can never release it, and
    completion must not precede the start. All timestamps must be positive and finite, and
    the supplied identity must still equal the provider's current credential, so a rotated
    or removed credential can never clear a block. The completion time becomes the
    watermark, preserving message IDs and the credential binding. Prepaid providers
    may also clear a confirmed balance exhaustion or auth rejection with this
    current-key proof; other block reasons never clear. Returns True only when this
    call released the block.
    """
    if not isinstance(provider, str) or not provider:
        return False
    if not isinstance(identity, str) or not identity:
        return False
    started = _epoch(started_at)
    completed = _epoch(completed_at)
    if started is None or completed is None or completed < started:
        return False
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        # Prepaid credit is refillable cash-like capacity: a verified successful
        # request bound to the current inference credential is genuine proof for
        # a confirmed balance exhaustion or auth rejection too, not only for a
        # monthly block. Other modes keep their existing monthly-only behavior.
        # Auth rejection is credential-specific in every mode: a real success on
        # the same key proves it works. Balance exhaustion is additionally
        # recoverable this way only for prepaid capacity.
        reasons = {MONTHLY_REASON, AUTH_REASON}
        if provider_is_prepaid(provider):
            reasons.add(BALANCE_REASON)
        if b.get('reason') not in reasons:
            return False
        opened = _epoch(b.get('opened_at'))
        if opened is None or started <= opened:
            return False
        ident = credential_identity(provider)
        if ident is None or identity != ident or b.get('credential') != ident:
            return False
        blocks[provider] = dict(b, cleared_at=completed, released=MODEL_SUCCESS_RELEASE)
        write_json(STATE / BILLING, blocks)
        return True


def known_providers():
    names = set(ENDPOINTS) | set(CONTROL_PLANE)
    try:
        names.update(str(p.get('model', '')).split('/', 1)[0] for p in config().get('profiles', {}).values()
                     if isinstance(p, dict) and '/' in str(p.get('model', '')))
    except Exception:
        pass
    return names


def retry_provider(provider):
    """Explicit local release/recheck authorization; never inferred from telemetry.

    This is one explicit path that may release a monthly block for new attempts, alongside
    the configured scheduled boundary release and a verified completed model success. It is
    labeled as an authorization to try again, not as proven recovery: the release stays
    open until a further billing error re-opens the block, and seen message IDs plus the
    cleared_at watermark keep historical errors from re-latching.
    """
    if not isinstance(provider, str) or provider not in known_providers():
        raise ValueError('Unknown provider: ' + str(provider))
    block = billing_block(provider)
    if not block:
        return {'provider': provider, 'released': False, 'action': 'provider_not_blocked',
                'proven_recovery': False,
                'note': 'No active billing block for this provider; nothing to release.'}
    released = clear(provider, evidence='manual_retry')
    return {'provider': provider, 'released': bool(released), 'action': 'manual_retry_authorized',
            'reason': block.get('reason'), 'proven_recovery': False,
            'message_ids_preserved': len(block.get('message_ids', [])),
            'warning': MANUAL_RETRY_WARNING, 'note': MANUAL_RETRY_NOTE}


def number(x):
    if isinstance(x, bool):
        return None
    try:
        n = float(x)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _window_key(value):
    return '%s|%s' % (value.get('name', ''), value.get('duration_minutes', ''))


def _addon_history_sample(value):
    """Observed usage-bundle numbers only; no lifetime totals or pack identity.

    The server summary describes the current active pool, so each sample keeps
    exactly what that reading supplied. When the provider later removes an
    exhausted pack, older observed amounts stay in history instead of being
    reconstructed or discarded; nothing implies cumulative purchases.
    """
    addon = value.get('addon') if isinstance(value, dict) else None
    if not isinstance(addon, dict) or addon.get('known') is not True:
        return None
    sample = {}
    percent = number(addon.get('remaining_percent'))
    if percent is not None and 0 <= percent <= 100:
        sample['remaining_percent'] = percent
    for key in ('remaining', 'limit', 'used'):
        amount = number(addon.get(key))
        if amount is not None and amount >= 0:
            sample[key] = amount
    count = addon.get('active_count')
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        sample['active_count'] = count
    expires = addon.get('nearest_expires_at')
    if isinstance(expires, str) and 0 < len(expires) <= 64:
        sample['nearest_expires_at'] = expires
    if addon.get('inconsistent') is True:
        sample['inconsistent'] = True
    return sample or None


def _record_history(result, now):
    """Retain bounded, credential-scoped quota samples without request content or secrets."""
    history = read_json(STATE / HISTORY, {})
    for provider, value in result.items():
        if value.get('state') != 'ok' or not value.get('sampled_at'):
            continue
        identity = value.get('_credential')
        record = history.get(provider, {}) if history.get(provider, {}).get('_credential') == identity else {}
        samples = record.get('samples', []) if isinstance(record.get('samples'), list) else []
        windows = {}
        for item in value.get('windows', []):
            remaining = number(item.get('remaining_percent'))
            if item.get('valid', True) and remaining is not None:
                windows[_window_key(item)] = {'remaining_percent': remaining,
                                              'resets_at': item.get('resets_at')}
        if provider == BAILIAN_PROVIDER:
            # Usage-bundle observations survive provider-side pack removal: each
            # sample keeps only the numbers that reading supplied, never a
            # reconstructed lifetime purchased/consumed total or pack identity.
            addon = _addon_history_sample(value)
            if addon is None and isinstance(value.get('plan_quotas'), dict):
                addon = _addon_history_sample(value['plan_quotas'].get('personal'))
            if addon is not None:
                windows['Addon|'] = addon
        balances = {}
        for item in value.get('balances', []):
            currency = item.get('currency') if isinstance(item, dict) else None
            remaining = number(item.get('remaining')) if isinstance(item, dict) else None
            if isinstance(currency, str) and 1 <= len(currency) <= 8 and remaining is not None and remaining >= 0:
                balances[currency] = remaining
        sampled = number(value.get('sampled_at'))
        if (windows or balances) and sampled is not None and (not samples or samples[-1].get('time') != sampled):
            samples.append({'time': sampled, 'windows': windows, 'balances': balances})
        samples = [x for x in samples if isinstance(x, dict) and number(x.get('time')) is not None
                   and now - float(x['time']) <= HISTORY_RETENTION_SECONDS][-HISTORY_MAX_SAMPLES:]
        history[provider] = {'_credential': identity, 'samples': samples}
    write_json(STATE / HISTORY, history)


def consumption_estimate(value, samples, now=None):
    """Estimate wall-clock range at observed burn rate, including idle time."""
    now = time.time() if now is None else now
    remaining = number(value.get('remaining_percent'))
    duration = number(value.get('duration_minutes'))
    reset = routing._iso_seconds(value.get('resets_at'))
    if remaining is None or remaining < 0:
        return None
    cycle_rate = None
    cycle_elapsed_hours = None
    if duration and duration > 0 and reset:
        elapsed_hours = max(0.0, (duration * 60 - max(0.0, reset - now)) / 3600)
        cycle_elapsed_hours = elapsed_hours
        used = max(0.0, 100.0 - remaining)
        if elapsed_hours >= 1 / 12 and used >= .01:
            cycle_rate = used / elapsed_hours
    key = _window_key(value)
    points = []
    for sample in samples if isinstance(samples, list) else []:
        stamp = number(sample.get('time')) if isinstance(sample, dict) else None
        row = sample.get('windows', {}).get(key) if isinstance(sample, dict) else None
        prior = number(row.get('remaining_percent')) if isinstance(row, dict) else None
        same_cycle = not value.get('resets_at') or not row or row.get('resets_at') == value.get('resets_at')
        if stamp is not None and prior is not None and same_cycle and 0 < now - stamp <= 24 * 3600:
            points.append((stamp, prior))
    points.sort()
    recent_rate = None
    span_hours = 0.0
    if points:
        oldest = next(((stamp, prior) for stamp, prior in points if now - stamp >= 300), None)
        if oldest:
            span_hours = (now - oldest[0]) / 3600
            recent_rate = max(0.0, oldest[1] - remaining) / span_hours
    if recent_rate is None and cycle_rate is None:
        return {'hours': None, 'rate_percent_per_hour': None, 'source': 'collecting',
                'idle': False, 'sample_span_hours': span_hours}
    if recent_rate is None:
        rate, source = cycle_rate, 'window_average'
    elif cycle_rate is None:
        rate, source = recent_rate, 'recent'
    else:
        recent_weight = min(.85, .35 + span_hours / 12)
        rate = recent_rate * recent_weight + cycle_rate * (1 - recent_weight)
        source = 'idle_adjusted' if recent_rate <= .001 else 'blended'
    smoothing_confidence = 1.0
    duration_hours = duration / 60.0 if duration and duration > 0 else None
    if duration_hours and cycle_elapsed_hours is not None and rate is not None:
        # A newly opened window often receives several tasks at once. Do not
        # extrapolate that first burst across the whole rolling window. Blend
        # toward the window's neutral full-cycle rate until enough wall-clock
        # evidence has accumulated; actual remaining quota is never smoothed.
        baseline_rate = 100.0 / duration_hours
        horizon = min(6.0, max(1.0, duration_hours / 24.0))
        smoothing_confidence = max(0.0, min(1.0, cycle_elapsed_hours / horizon))
        if rate > baseline_rate and smoothing_confidence < 1.0:
            rate = baseline_rate + (rate - baseline_rate) * smoothing_confidence
    if remaining == 0:
        return {'hours': 0.0,
                'rate_percent_per_hour': round(rate, 4) if rate is not None else None,
                'source': 'exhausted', 'idle': False,
                'sample_span_hours': round(span_hours, 2)}
    idle = recent_rate is not None and recent_rate <= .001 and span_hours >= 1 / 6
    # Estimate working pace separately from wall-clock pace. Idle gaps must not
    # make the next batch appear cheaper. Tiny rounding deltas are not activity.
    active_spend = active_hours = 0.0
    # Integrate fixed-duration observation blocks, including flat counter ticks.
    # Counting only downward samples makes integer counters appear faster when
    # polled more frequently. Gaps over 30 minutes remain unknown, not activity.
    block_start = block_before = last_at = last_value = None
    for stamp, prior in points + [(now, remaining)]:
        if last_at is not None and (stamp - last_at > 1800 or prior > last_value):
            block_start = block_before = None
        if block_start is None:
            block_start, block_before = stamp, prior
        elif stamp - block_start >= 1800:
            drop = block_before - prior
            if drop >= .01:
                active_spend += drop
                active_hours += (stamp - block_start) / 3600
            block_start, block_before = stamp, prior
        last_at, last_value = stamp, prior
    # Active-only rate is intentionally slower to enter than the five-minute
    # wall-clock estimate. Short rolling windows need 30 minutes of evidence;
    # daily-or-longer windows need two hours because their counters are coarse
    # and a one-percent tick can otherwise swing the entire routing curve.
    active_min_hours = .5 if not duration_hours or duration_hours <= 24 else 2.0
    raw_active_rate = active_spend / active_hours if active_hours >= active_min_hours else None
    # A short parallel burst must not take over the whole-window forecast at a
    # threshold boundary. Let sustained active burn enter continuously: short
    # windows settle over two hours, long windows over eight. The provider's
    # actual remaining percentage still updates immediately and can block calls
    # at zero, so this inertia affects projection only, never admission safety.
    active_settle_hours = 2.0 if not duration_hours or duration_hours <= 24 else 8.0
    active_confidence = 0.0
    active_rate = None
    if raw_active_rate is not None:
        active_confidence = max(0.0, min(1.0,
            (active_hours - active_min_hours) /
            max(.000001, active_settle_hours - active_min_hours)))
        active_rate = (rate or 0) + max(0.0, raw_active_rate - (rate or 0)) * active_confidence
    forecast_rate = max(rate or 0, active_rate or 0)
    hours = remaining / forecast_rate if forecast_rate > .001 else None
    return {'hours': round(min(hours, 24 * 365), 2) if hours is not None else None,
            'rate_percent_per_hour': round(rate, 4) if rate is not None else None,
            'active_rate_percent_per_hour': round(active_rate, 4) if active_rate is not None else None,
            'raw_active_rate_percent_per_hour': round(raw_active_rate, 4) if raw_active_rate is not None else None,
            'active_sample_hours': round(active_hours, 3),
            'active_min_sample_hours': active_min_hours,
            'active_settle_hours': active_settle_hours,
            'active_confidence': round(active_confidence, 3),
            'smoothing_confidence': round(smoothing_confidence, 3),
            'source': source, 'idle': idle, 'sample_span_hours': round(span_hours, 2)}


def balance_consumption_estimate(value, samples, now=None, priced=None):
    """Estimate PAYG range from official token prices and observed balance burn.

    A balance increase starts a new observation epoch. Mixing samples from before
    and after a top-up can make net burn look close to zero and inflate remaining
    runtime by hundreds of hours. When both balance burn and token pricing are
    available, use the higher burn rate: this is a range estimate, so one weak or
    idle-heavy signal must not lengthen the displayed endurance.
    """
    now = time.time() if now is None else now
    remaining = number(value.get('remaining')) if isinstance(value, dict) else None
    currency = value.get('currency') if isinstance(value, dict) else None
    if remaining is None or remaining < 0 or not isinstance(currency, str):
        return None
    points = []
    for sample in samples if isinstance(samples, list) else []:
        stamp = number(sample.get('time')) if isinstance(sample, dict) else None
        prior = number((sample.get('balances') or {}).get(currency)) if isinstance(sample, dict) else None
        if stamp is not None and prior is not None and 0 < now - stamp <= 7 * 86400:
            points.append((stamp, prior))
    points.sort()
    # Segment balance history at the latest material increase. Provider balances
    # are rounded, so ignore tiny corrections but treat an actual recharge as a
    # new capacity epoch. Include the current reading to catch a top-up between
    # the most recent stored sample and this response.
    timeline = points + [(now, remaining)]
    epoch_start = 0
    for index in range(1, len(timeline)):
        previous, current = timeline[index - 1][1], timeline[index][1]
        threshold = max(.01, max(previous, current) * .005)
        if current > previous + threshold:
            epoch_start = index
    topup_detected = epoch_start > 0
    epoch_started_at = timeline[epoch_start][0] if topup_detected else None
    points = timeline[epoch_start:-1]
    observed_capacity = max([remaining] + [prior for _, prior in points])
    # A five-minute burst extrapolates a wildly pessimistic range for model APIs.
    # Require one hour of wall-clock evidence, then use the longest available span
    # so idle periods and uneven task dispatch are naturally included.
    oldest = next(((stamp, prior) for stamp, prior in points if now - stamp >= 3600), None)
    priced_rate = number(priced.get('rate_balance_per_hour')) if isinstance(priced, dict) else None
    if not oldest and priced_rate is None:
        return {'hours': 0.0 if remaining == 0 else None,
                'rate_balance_per_hour': None,
                'source': 'exhausted' if remaining == 0 else 'collecting',
                'idle': False, 'sample_span_hours': 0.0,
                'observed_capacity': round(observed_capacity, 6)}
    span_hours = (now - oldest[0]) / 3600 if oldest else number(priced.get('sample_span_hours')) or 0.0
    wall_rate = max(0.0, oldest[1] - remaining) / span_hours if oldest and span_hours > 0 else None
    if wall_rate is not None and priced_rate is not None:
        rate = max(wall_rate, priced_rate)
        source = 'conservative_balance_or_official_pricing'
    elif wall_rate is not None:
        rate, source = wall_rate, 'wall_clock'
    else:
        rate, source = priced_rate, 'official_token_pricing'
    if remaining == 0:
        result = {'hours': 0.0, 'rate_balance_per_hour': round(rate, 6),
                  'source': 'exhausted', 'idle': False,
                  'confidence': 'high' if span_hours >= 24 else 'medium' if span_hours >= 6 else 'low',
                  'sample_span_hours': round(span_hours, 2),
                  'observed_capacity': round(observed_capacity, 6)}
        if topup_detected:
            result['balance_epoch_started_at'] = epoch_started_at
        if isinstance(priced, dict):
            for key in ('estimated_spend_cny', 'tokens', 'task_count', 'pricing_effective'):
                if key in priced:
                    result[key] = priced[key]
        return result
    idle = rate <= .000001
    hours = remaining / rate if not idle else None
    confidence = 'high' if span_hours >= 24 else 'medium' if span_hours >= 6 else 'low'
    result = {'hours': round(min(hours, 24 * 365), 2) if hours is not None else None,
              'rate_balance_per_hour': round(rate, 6), 'source': source, 'idle': idle,
              'confidence': confidence, 'sample_span_hours': round(span_hours, 2),
              'observed_capacity': round(observed_capacity, 6)}
    if topup_detected:
        result['balance_epoch_started_at'] = epoch_started_at
    if isinstance(priced, dict):
        for key in ('estimated_spend_cny', 'tokens', 'task_count', 'pricing_effective'):
            if key in priced:
                result[key] = priced[key]
    return result


def window(name, detail, duration=None):
    limit, remaining = number(detail.get('limit')), number(detail.get('remaining'))
    if remaining is None and 'remaining' not in detail and limit is not None:
        # The live endpoint omits remaining on an exhausted window and reports used
        # instead. Derive remaining only from two valid numbers; malformed input stays
        # unknown rather than silently reading as 0%.
        used = number(detail.get('used'))
        if used is not None and used >= 0:
            remaining = max(0.0, limit - used)
    valid = limit is not None and limit > 0 and remaining is not None and 0 <= remaining <= limit
    resets = detail.get('resetTime')
    if isinstance(resets, (int, float)):
        try:
            resets = datetime.fromtimestamp(resets / 1000 if resets >= 1e12 else resets, timezone.utc).isoformat() if resets > 0 else None
        except (ValueError, OSError, OverflowError):
            resets = None
    return {'name': name, 'limit': limit, 'remaining': remaining,
            'remaining_percent': round(remaining / limit * 100, 3) if valid else None,
            'duration_minutes': duration, 'resets_at': resets, 'valid': valid}


def normalize(provider, body):
    if not isinstance(body, dict):
        raise ValueError('Invalid quota payload')
    if provider == 'deepseek':
        balances = []
        for b in body.get('balance_infos', []):
            amount = number(b.get('total_balance'))
            if isinstance(b.get('currency'), str) and amount is not None:
                balances.append({'currency': b['currency'], 'remaining': amount})
        return {'available': body.get('is_available') if isinstance(body.get('is_available'), bool) else None,
                'balances': balances, 'windows': []}
    windows = []
    units = {'TIME_UNIT_MINUTE': 1, 'TIME_UNIT_HOUR': 60, 'TIME_UNIT_DAY': 1440,
             'TIME_UNIT_SECOND': 1 / 60}
    for i, row in enumerate(body.get('limits', [])):
        w = row.get('window') or {}
        duration = number(w.get('duration'))
        minutes = duration * units[w.get('timeUnit')] if duration is not None and w.get('timeUnit') in units else None
        windows.append(window('window_' + str(i), row.get('detail') or {}, minutes))
    if isinstance(body.get('usage'), dict):
        # Kimi's aggregate is the shared seven-day plan pool. Carry that known
        # duration into the normalized record so reset-aware routing can judge
        # whether a small remainder is actually enough to reach the next reset.
        windows.append(window('overall', body['usage'], KNOWN_RATIO_WINDOWS['limit_7d']))
    # usages.<known key>.used_ratio is an explicit per-window fraction (0..1) and is a
    # fallback only when that window is otherwise absent or unusable. Unknown keys, a
    # ratio outside 0..1 (overage is not evidence of a specific exhausted window), a
    # window already parsed from limits (same duration) and the weekly ratio duplicated
    # by a valid overall aggregate are skipped. Malformed input stays unknown and the
    # console never shows duplicate cards for one real window.
    usages = body.get('usages')
    if isinstance(usages, dict):
        overall_valid = any(w['name'] == 'overall' and w['valid'] for w in windows)
        known = {w['duration_minutes'] for w in windows
                 if w['valid'] and w.get('duration_minutes') is not None}
        for key, detail in usages.items():
            duration = KNOWN_RATIO_WINDOWS.get(key)
            if duration is None or not isinstance(detail, dict) or duration in known:
                continue
            if key in WEEKLY_RATIO_KEYS and overall_valid:
                continue
            ratio = number(detail.get('used_ratio'))
            if ratio is None or not 0 <= ratio <= 1:
                continue
            derived = window('usages.' + key, {
                'limit': 1, 'remaining': 1.0 - ratio,
                'resetTime': detail.get('reset_time', detail.get('resetTime'))}, duration)
            derived['derived'] = True
            windows.append(derived)
            known.add(duration)
    valid = [w for w in windows if w['valid']]
    return {'windows': windows, 'available': False if any(w['remaining'] == 0 for w in valid) else
            (True if windows and len(valid) == len(windows) else None), 'balances': []}


def fetch_one(provider):
    now = time.time()
    control = CONTROL_PLANE.get(provider)
    if control is not None:
        # Signed control-plane adapters own their full failure taxonomy and
        # credential boundary; values are never visible here.
        return control()
    try:
        body = request(ENDPOINTS[provider], headers={'Authorization': 'Bearer ' + auth_key(provider),
                                                  'Accept': 'application/json'})
        value = normalize(provider, body)
        value.update(state='ok', sampled_at=now)
        return value
    except HttpFailure as e:
        state = 'auth_error' if e.status in (401, 403) else 'rate_limited' if e.status == 429 else 'unavailable'
        return {'state': state, 'http_status': e.status, 'attempted_at': now}
    except ValueError:
        return {'state': 'unknown', 'attempted_at': now}


def _bailian_display(value, last, anchor, now):
    """Alibaba live-failure display: keep the genuine reading, never fake a refresh.

    A failed live query (missing credential, auth rejection, unreachable or
    malformed response) keeps the previous live reading with its original
    sample time so consumers age it, or — before any live reading succeeded —
    the manual snapshot anchor with its own explicit freshness. The
    ``live_error`` states why the live API did not refresh the row. A failed
    attempt never advances sampled_at, never fabricates numbers and never
    reports recovery.
    """
    if not bailian_quota.record_matches_plan(last):
        last = {}
    if not bailian_quota.record_matches_plan(anchor):
        anchor = None
    error = dict(value.get('live_error')) if isinstance(value.get('live_error'), dict) else {}
    error.setdefault('state', value.get('state', 'unknown'))
    error.setdefault('attempted_at', value.get('attempted_at') or now)
    if isinstance(last, dict) and last.get('source') in (bailian_quota.SOURCE_LIVE, 'personal_console') and last.get('sampled_at') and \
            last.get('windows') and not isinstance(last.get('snapshot'), dict):
        kept = dict(last)
        kept.update(state=value.get('state', 'unknown'),
                    attempted_at=value.get('attempted_at') or now)
    elif isinstance(anchor, dict):
        kept = _snapshot_record(BAILIAN_PROVIDER, anchor, now)
        kept['live_error'] = error
        return kept
    else:
        kept = dict(value)
    kept['live_error'] = error
    return kept


def _providers_in_use():
    """Telemetry-bearing providers referenced by at least one configured profile."""
    try:
        models = [v['model'] for v in config()['profiles'].values()]
    except Exception:
        return []
    known = set(ENDPOINTS) | set(CONTROL_PLANE)
    return [p for p in known if any(str(m).split('/', 1)[0] == p for m in models)]


def refresh(force=False):
    with locked('quota'):
        old = read_json(STATE / 'quota.json', {})
        if bailian_quota.plan_edition() in ('personal', 'sequential'):
            old[BAILIAN_PROVIDER] = _bailian_plan_record(old.get(BAILIAN_PROVIDER))
        now = time.time()
        providers = _providers_in_use()
        identities = {p: _telemetry_identity(p) for p in providers}
        # A configured monthly schedule releases the previous cycle's Kimi monthly block on
        # every refresh, including cache hits. It is authorization to try, never quota
        # proof, and it never touches non-monthly or credential-mismatched blocks.
        scheduled_release('kimi-for-coding', now)
        same = all(old.get(p, {}).get('_credential') == identities[p] for p in providers)
        recent = old and all(now - old.get(p, {}).get('checked_at', 0) < 60 for p in providers)
        snapshots = _active_snapshots()
        if not force and same and recent and all(p in old for p in snapshots):
            snapshot = view(old)
            observe_load(config(), snapshot, now)
            return snapshot
        result = {}
        # Inference-credential identities are captured before dispatch and rebound to
        # recovery afterwards: the Alibaba control-pair hash only invalidates cached
        # telemetry, it never proves billing recovery.
        inference_identities = {p: credential_identity(p) for p in providers}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(providers)))) as pool:
            for p, value in zip(providers, pool.map(fetch_one, providers)):
                last = old.get(p, {}) if identities[p] == old.get(p, {}).get('_credential') else {}
                if p == BAILIAN_PROVIDER and _telemetry_identity(p) != identities[p]:
                    # A response from a rotated/removed control identity is not
                    # a current-account reading. Force the next sampler retry.
                    last = {}
                    value = {'state': 'unknown', 'attempted_at': now,
                             'live_error': {'state': 'unknown', 'attempted_at': now,
                                            'detail': 'Quota credentials changed during the query'}}
                if p == BAILIAN_PROVIDER and value.get('state') != 'ok':
                    # A failed live query never fabricates a refresh: the last genuine
                    # live reading is kept with its original sample time, or before any
                    # live reading the manual snapshot anchor with its own explicit
                    # freshness, each carrying the sanitized live_error.
                    value = _bailian_display(value, last, snapshots.get(p), now)
                    if bailian_quota.plan_edition() in ('personal', 'sequential'):
                        value.update(plan_edition=bailian_quota.plan_edition(), _plan_identity=identities[p])
                elif value['state'] not in ('ok', 'auth_error', 'no_credential') and last.get('sampled_at'):
                    value = dict(last, **value)
                value.update(checked_at=now, _credential=identities[p])
                result[p] = value
                if value['state'] == 'ok' and value.get('available') is True:
                    if p == BAILIAN_PROVIDER:
                        # Only an official live account-API reading proves Alibaba
                        # recovery; a snapshot-shaped fallback display never does.
                        if value.get('source') == bailian_quota.SOURCE_LIVE:
                            account_api_recovery(p, value.get('sampled_at'),
                                                 inference_identities[p], identities[p], value=value)
                    elif p == ARK_PROVIDER:
                        # The signed account query and the inference request
                        # use different credentials. Validate both generations
                        # and authorize only a bounded real-work retry.
                        apply_provider_retry_evidence(
                            p, value, now, time.time(), inference_identities[p], identities[p])
                    else:
                        # Fresh positive account query recovers; a query started before a
                        # concurrent trip must never clear that newer circuit.
                        clear(p, sampled_since=now, identity=identities[p])
        if KIMI_PROVIDER in result:
            # The authenticated account reading is add-only: it never replaces the
            # inference-key provider windows, never invents a missing month pool and
            # never reports a failed fetch as a fresh successful sample.
            account_started = time.time()
            result[KIMI_PROVIDER]['account'] = _kimi_account_snapshot(
                result[KIMI_PROVIDER].get('account'))
            apply_provider_retry_evidence(
                KIMI_PROVIDER, result[KIMI_PROVIDER]['account'], account_started, time.time(),
                inference_identities.get(KIMI_PROVIDER))
        # Manual snapshots are merged here so every consumer of the cached quota
        # file sees them, while their freshness stays explicit in view().
        for provider, record in snapshots.items():
            if provider not in result:
                result[provider] = _snapshot_record(provider, record, now)
        write_json(STATE / 'quota.json', result)
        if bailian_quota.plan_edition() in ('personal', 'sequential') and \
                bailian_quota.record_matches_plan(result.get(BAILIAN_PROVIDER)):
            write_json(STATE / bailian_quota.PLAN_CACHE_FILE, result[BAILIAN_PROVIDER])
        _record_history(result, now)
        snapshot = view(result)
        observe_load(config(), snapshot, now)
        return snapshot


def view(values):
    out = {}
    history = read_json(STATE / HISTORY, {})
    now = time.time()
    try:
        prepaid = prepaid_providers(config())
    except Exception:
        prepaid = set()
    import workload
    try:
        loads = workload.snapshot(config(), now)
    except Exception:
        loads = {}
    priced = None
    if 'deepseek' in values:
        try:
            import economics
            priced = economics.recent_deepseek_spend(config(), now)
        except Exception:
            priced = None
    snapshots = _active_snapshots()
    merged = {}
    provider_ids = set(values) | set(snapshot_store())
    if bailian_quota.plan_edition() in ('personal', 'sequential') and \
            (STATE / bailian_quota.PLAN_CACHE_FILE).exists():
        provider_ids.add(BAILIAN_PROVIDER)
    for p in provider_ids:
        stored = values.get(p)
        if p == BAILIAN_PROVIDER:
            stored = _bailian_plan_record(stored)
        if p in snapshots and (not isinstance(stored, dict) or isinstance(stored.get('snapshot'), dict)):
            record = _snapshot_record(p, snapshots[p], now)
            if p == BAILIAN_PROVIDER and isinstance(stored, dict) and \
                    isinstance(stored.get('live_error'), dict):
                # The stored live-query failure stays visible on the rebuilt
                # snapshot display: the anchor is shown as the manual reading it
                # is, plus why the live API did not refresh it.
                record['live_error'] = stored['live_error']
            merged[p] = record
        elif isinstance(stored, dict):
            merged[p] = stored
    for p, v in merged.items():
        v = {k: x for k, x in v.items() if not k.startswith('_')}
        if p == BAILIAN_PROVIDER and v.get('plan_edition') == 'sequential':
            plans = v.get('plan_quotas') if isinstance(v.get('plan_quotas'), dict) else {}
            v['plan_quotas'] = {edition: bailian_quota.public_plan_record(plans.get(edition))
                                for edition in ('team', 'personal')}
            v['windows'] = []  # Team Credits and Personal percentage are never one pool.
        v['windows'] = [dict(item) for item in v.get('windows', [])]
        samples = history.get(p, {}).get('samples', []) if isinstance(history.get(p), dict) else []
        if p == BAILIAN_PROVIDER and bailian_quota.plan_edition() in ('personal', 'sequential') and \
                history.get(p, {}).get('_credential') != bailian_quota.credential_identity():
            samples = []  # A previous Team/key burn rate is not Personal consumption.
        for item in v['windows']:
            estimate = consumption_estimate(item, samples, now)
            if estimate is not None:
                item['consumption_estimate'] = estimate
        v['balances'] = [dict(item) for item in v.get('balances', [])]
        for item in v['balances']:
            estimate = balance_consumption_estimate(item, samples, now, priced if p == 'deepseek' else None)
            if estimate is not None:
                item['consumption_estimate'] = estimate
        if isinstance(v.get('seat_groups'), list):
            # Per-seat detail stays outside the combined Credits window: it is
            # optional display metadata for independent seats sharing one cycle,
            # never a second quota constraint for admission.
            v['seat_groups'] = [{k: x for k, x in item.items() if not k.startswith('_')}
                                for item in v['seat_groups'] if isinstance(item, dict)][:8]
        if isinstance(v.get('snapshot'), dict):
            # A manual snapshot is never shown as live telemetry: staleness
            # follows its own freshness horizon instead of the 15-minute cache
            # window, and an aging reading counts as unknown, not healthy.
            v['stale'] = v.get('state') != 'ok' or v['snapshot'].get('freshness') != 'fresh'
        else:
            v['stale'] = v.get('state') != 'ok' or time.time() - v.get('sampled_at', 0) > 900
        if p == BAILIAN_PROVIDER and isinstance(v.get('live_error'), dict):
            v['stale'] = True
        if p == BAILIAN_PROVIDER:
            # Resolve current slot generations outside scheduling's admission lock.
            # This is callable/probe metadata, never account Credits or healthy proof.
            try:
                import bailian_pool
                pool = bailian_pool.recovery_available(
                    config(), after=(billing_block(p) or {}).get('opened_at'))
                if pool.get('configured'):
                    v['inference_pool'] = dict(pool, sampled_at=time.time())
            except Exception:
                # Configured-but-unreadable pools must not look callable.
                try:
                    if config().get('bailian_pool', {}).get('enabled') is True:
                        v['inference_pool'] = {'configured': True, 'admission_available': False,
                                               'available': False, 'sampled_at': time.time()}
                except Exception:
                    pass
        if p in (ARK_PROVIDER, BAILIAN_PROVIDER):
            # Safe metadata only: the credential source label, never a value or
            # identity. No AK/SK, hash or reference path leaves this boundary.
            source = v.get('credential_source')
            v['credential_source'] = source if source in ('credential_reference', 'environment') else None
        block = billing_block(p)
        if block:
            # Overlay the active circuit so a stale positive cache is never advertised
            # and an authentic but misleading window snapshot never reads as callable.
            # The credential hash stays private; only redacted, actionable fields show.
            reason = block.get('reason') or GENERIC_REASON
            telemetry_available = v.get('available')
            billing = {'opened_at': block.get('opened_at'), 'message': block.get('message'),
                       'reason': reason, 'telemetry_state': v.get('state'),
                       'telemetry_available': telemetry_available}
            v.update(available=False, state='billing_blocked', billing=billing,
                     billing_reason=reason)
            if reason == MONTHLY_REASON:
                v['monthly_plan_exhausted'] = True
                billing['warning'] = ('Monthly plan quota is exhausted for this billing cycle. Reported '
                                      'window remaining values are authentic account telemetry but do not '
                                      'mean the provider is callable until real evidence of recovery appears.')
        if p == KIMI_PROVIDER:
            # Read-only schedule exposure for the console: the same normalized setting plus
            # the next configured boundary in ISO8601 when the schedule is enabled.
            v['monthly_reset'] = monthly_reset_view()
            # Weekly-reserve and account-pool metadata for display. The guard
            # itself always re-derives from the same record, never from this view,
            # and the effective-release fields come from the same authoritative
            # helper admission and the native gate use, so they cannot drift.
            try:
                reserve_config = config()
            except Exception:
                reserve_config = {}
            v['weekly_reserve'] = kimi_weekly_reserve_state(
                reserve_config, {KIMI_PROVIDER: v})
            account = v.get('account')
            if isinstance(account, dict):
                age = time.time() - (_epoch(account.get('sampled_at')) or 0)
                account['stale'] = account.get('state') != 'ok' or age > KIMI_ACCOUNT_MAX_AGE
            v['account_total'] = kimi_account_total_state({KIMI_PROVIDER: v})
        v['workload'] = loads.get(p, {})
        import capacity
        v['capacity_forecast'] = capacity.provider(v, now)
        if p in prepaid:
            # Prepaid Credits are refillable, so "will this allowance last until
            # the renewal date" is not a valid calendar-pressure question. Keep
            # the real reset time and observed runtime estimate, but neutralize
            # only the display pace ratio so a far renewal does not label usable
            # prepaid capacity as about to run out. Admission/control still use
            # actual zero, expiry and credential gates.
            v['prepaid'] = True
            forecast = v['capacity_forecast']
            if forecast.get('windows') and not forecast.get('exhausted'):
                for item in forecast['windows']:
                    if isinstance(item.get('remaining_percent'), (int, float)) and \
                            item.get('remaining_percent') > 0:
                        item['runway'] = 1.0
                forecast['runway'] = 1.0
        fitted = iter(v['capacity_forecast']['windows'])
        for item in v['windows']:
            item['capacity_forecast'] = next(fitted, None) if capacity.window(item, now) is not None else None
        out[p] = v
    return out


def allowed(provider, q, complexity, threshold=0, retry_monthly=False, prepaid=None):
    v = q.get(provider, {})
    if prepaid is None:
        prepaid = provider_is_prepaid(provider)
    # Preserve the account endpoint's verdict separately from the monthly-error overlay.
    # Explicit retries only bypass the hidden monthly flag, never confirmed empty quota.
    telemetry = v.get('billing') or {}
    state = telemetry.get('telemetry_state', v.get('state'))
    available = (telemetry.get('telemetry_available') if v.get('state') == 'billing_blocked'
                 else v.get('available'))
    # A quota-query AK/SK failure is control-plane telemetry, not an inference-key
    # failure. For prepaid providers it must not disable otherwise usable
    # inference by itself; a confirmed zero/expired window or an active
    # inference-key circuit below still blocks. Other modes keep their hard gate.
    if state == 'auth_error' and not prepaid:
        return False, 'credential_rejected'
    if state == 'rate_limited' and not prepaid:
        return False, 'quota_endpoint_rate_limited'
    # Ark without control-plane credentials keeps quota unknown: the data plane
    # may still serve, and a zero-window check below only applies to real
    # telemetry. Unknown quota is never treated as unlimited or as empty.
    windows = v.get('windows', [])
    empty_window = any(w.get('valid', True) and w.get('remaining') == 0
                       for w in windows)
    if available is False or empty_window:
        return False, 'quota_exhausted_or_account_unavailable'
    if prepaid:
        # Retain confirmed expiry protection when there is no usable positive
        # window left. A positive window alongside an expired seat-type group
        # still represents callable capacity, so it is not blocked here.
        usable = any(w.get('valid') is not False and
                     isinstance(w.get('remaining'), (int, float)) and
                     not isinstance(w.get('remaining'), bool) and w.get('remaining') > 0
                     for w in windows)
        expired = any(w.get('expired') is True for w in windows)
        if expired and not usable:
            return False, 'quota_exhausted_or_account_unavailable'
    pool = v.get('inference_pool') if provider == BAILIAN_PROVIDER else None
    pool_fresh = (isinstance(pool, dict) and pool.get('configured') is True and
                  isinstance(pool.get('sampled_at'), (int, float)) and
                  0 <= time.time() - pool['sampled_at'] <= 90)
    if isinstance(pool, dict) and pool.get('configured') and (
            not pool_fresh or pool.get('admission_available') is not True):
        return False, 'bailian_slots_waiting_or_unavailable'
    block = billing_block(provider)
    # A real same-key model success or a leased-at-dispatch due probe can pass an
    # old quota/balance provider circuit. Never clear it here or bypass auth,
    # genuine account zero, expiry, or other providers' policies.
    pool_retry = (pool_fresh and pool.get('available') is True and
                  (pool.get('healthy') is True or pool.get('authorized_probe') is True) and
                  block and (block.get('reason') in (MONTHLY_REASON, BALANCE_REASON) or
                             block.get('reason') == AUTH_REASON and pool.get('credential_rotated') is True))
    if block and not pool_retry and not (retry_monthly and block.get('reason') == MONTHLY_REASON):
        return False, 'provider_billing_blocked'
    if pool_retry:
        return True, 'bailian_current_slot_recovery_or_due_probe'
    fresh_enough = time.time() - v.get('sampled_at', 0) <= 900
    # The former provider-level Kimi reserve is retired: the weekly allowance is
    # now protected per MODEL (the reserved k3.1-pro Ultra model keeps the last
    # slice) through kimi_request_allowed(), which runs in profile admission and
    # in the loopback gate's preflight. The five-hour window never participates
    # in that weekly decision, and a short-window dip must not fake depletion.
    if block:
        return True, 'explicit_profile_monthly_retry'
    return True, 'quota_unknown' if v.get('stale', True) or available is None else 'quota_available'


KIMI_PROVIDER = 'kimi-for-coding'
KIMI_ACCOUNT_MAX_AGE = 900
KIMI_ACCOUNT_WEEKLY_KIND = 'weekly'
KIMI_ACCOUNT_MONTH_KINDS = ('month_total', 'month_code')
KIMI_RESERVED_MODEL_ID = 'k3.1-pro'
# Explicit seven-day window identities. The legacy ``overall`` aggregate is kept
# as a separate, lower-priority shape: it described this endpoint's shared weekly
# plan pool historically, but it must never be renamed into the account total and
# must never override the authenticated account's own weekly verdict.
KIMI_TELEMETRY_WEEKLY_NAMES = ('usages.limit_7d', 'limit_7d', 'limit7d')
KIMI_LEGACY_WEEKLY_NAME = 'overall'


def _kimi_profile_record(q):
    value = (q or {}).get(KIMI_PROVIDER) if isinstance(q, dict) else None
    return value if isinstance(value, dict) else {}


def _fresh_epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return 0 <= time.time() - float(value) <= KIMI_ACCOUNT_MAX_AGE


def kimi_account_record(q):
    """Fresh authenticated-account reading from the private adapter, else None."""
    record = _kimi_profile_record(q).get('account')
    if not isinstance(record, dict) or record.get('state') != 'ok':
        return None
    if not _fresh_epoch(record.get('sampled_at')):
        return None
    return record


def kimi_account_total_state(q):
    """Account-pool state for the account currently bound to the inference key.

    ``month_total``/``month_code`` are real account pools, never weekly pools and
    never estimated from a four-week rule. A real zero in either pool blocks every
    Kimi model, including the reserved Ultra model, but only while the adapter has
    verified that the account is the inference-key account. An unverified or
    mismatched binding is display-only and cannot block or clear inference, and a
    missing month window stays ``not_reported`` until a real reading exists.
    """
    record = _kimi_profile_record(q).get('account')
    empty = {'authority': 'none', 'status': 'not_reported', 'blocks': False,
             'windows': [], 'remaining_percent': None}
    if not isinstance(record, dict):
        return empty
    binding = record.get('account_binding') if isinstance(record.get('account_binding'), dict) else {}
    state = binding.get('state')
    authority = 'verified' if state == 'verified' else (
        'mismatch' if state == 'mismatch' else 'unverified')
    windows = [dict(w) for w in record.get('windows') or []
               if isinstance(w, dict) and w.get('kind') in KIMI_ACCOUNT_MONTH_KINDS]
    summary = {'authority': authority, 'status': 'not_reported', 'blocks': False,
               'windows': [{key: w.get(key) for key in
                            ('name', 'kind', 'remaining_percent', 'resets_at')}
                           for w in windows],
               'remaining_percent': None}
    values = [float(w['remaining_percent']) for w in windows
              if isinstance(w.get('remaining_percent'), (int, float))
              and not isinstance(w.get('remaining_percent'), bool)
              and math.isfinite(w['remaining_percent'])]
    if values:
        summary['remaining_percent'] = min(values)
    if not windows:
        summary['status'] = 'unverified' if authority != 'verified' else 'not_reported'
        return summary
    if not values:
        summary['status'] = 'unknown'
        return summary
    if authority != 'verified':
        summary['status'] = 'unverified'
        summary['note'] = ('Account windows are display-only until the adapter verifies '
                           'this account as the inference-key account.')
        return summary
    summary['status'] = 'exhausted' if any(value <= 0 for value in values) else 'available'
    summary['blocks'] = summary['status'] == 'exhausted'
    return summary


def _weekly_window(windows, names=(), kind=None, allow_duration=True):
    """One valid weekly-shaped window by kind, exact name, then explicit duration.

    Exact names always win over a same-duration window, and the legacy ``overall``
    aggregate is never picked up by the duration fallback: it is only ever read
    through its own explicit legacy lookup.
    """
    entries = [window for window in (windows if isinstance(windows, list) else [])
               if isinstance(window, dict) and window.get('valid', True) is True
               and isinstance(window.get('remaining_percent'), (int, float))
               and not isinstance(window.get('remaining_percent'), bool)
               and math.isfinite(window['remaining_percent'])]
    if kind is not None:
        for window in entries:
            if window.get('kind') == kind:
                return dict(window)
        return None
    for window in entries:
        if window.get('name') in names:
            return dict(window)
    if allow_duration and names != (KIMI_LEGACY_WEEKLY_NAME,):
        for window in entries:
            if window.get('duration_minutes') == 10080 and \
                    window.get('name') != KIMI_LEGACY_WEEKLY_NAME:
                return dict(window)
    return None


def kimi_weekly_state(q, c=None):
    """Weekly-pool state with explicit seven-day windows taking priority.

    ``status`` is ``known`` (a fresh weekly percentage), ``not_applicable`` (the
    authenticated account's plan has no weekly window; the weekly reserve cannot
    apply and is not simulated from the account total) or ``unknown`` (no
    trustworthy weekly reading; ordinary Kimi models then fail closed instead of
    being treated as unlimited or as zero).

    The verified account's weekly window wins, then this endpoint's explicit
    ``limit_7d`` window, then the legacy ``overall`` aggregate. A five-hour window
    never participates, and an unverified account can never override telemetry.
    """
    record = kimi_account_record(q)
    if record is not None:
        binding = record.get('account_binding') if isinstance(record.get('account_binding'), dict) else {}
        if binding.get('state') == 'verified':
            applicability = record.get('weekly_applicability')
            if applicability == 'not_applicable':
                return {'status': 'not_applicable', 'remaining_percent': None,
                        'source': 'account', 'resets_at': None, 'window': None,
                        'as_of': record.get('sampled_at')}
            if applicability == 'applicable':
                window = _weekly_window(record.get('windows'), kind=KIMI_ACCOUNT_WEEKLY_KIND)
                if window is not None:
                    return {'status': 'known',
                            'remaining_percent': float(window['remaining_percent']),
                            'source': 'account_weekly', 'resets_at': window.get('resets_at'),
                            'window': window, 'as_of': record.get('sampled_at')}
    view = _kimi_profile_record(q)
    if view.get('stale', True) or view.get('state') != 'ok':
        return {'status': 'unknown', 'remaining_percent': None, 'source': 'none',
                'resets_at': None, 'window': None, 'as_of': None}
    windows = view.get('windows') if isinstance(view.get('windows'), list) else []
    window = _weekly_window(windows, names=KIMI_TELEMETRY_WEEKLY_NAMES)
    source = 'telemetry_limit_7d'
    if window is None:
        window = _weekly_window(windows, names=(KIMI_LEGACY_WEEKLY_NAME,),
                                allow_duration=False)
        source = 'telemetry_overall'
    if window is None:
        return {'status': 'unknown', 'remaining_percent': None, 'source': 'none',
                'resets_at': None, 'window': None, 'as_of': view.get('sampled_at')}
    return {'status': 'known', 'remaining_percent': float(window['remaining_percent']),
            'source': source, 'resets_at': window.get('resets_at'),
            'window': window, 'as_of': view.get('sampled_at')}


def kimi_weekly_remaining_percent(q):
    """Fresh authoritative Kimi weekly remaining percent, else None.

    Kept for callers that only need the number; explicit seven-day windows are
    preferred and the legacy ``overall`` weekly aggregate is the fallback.
    """
    weekly = kimi_weekly_state(q)
    return weekly['remaining_percent'] if weekly['status'] == 'known' else None


def kimi_weekly_reserve_percent(c=None):
    """Share of the Kimi weekly pool reserved for k3.1-pro (0-100, default 30).

    The legacy ``kimi_reserve_percent`` is folded in as an additional floor so an
    existing explicit operator value is preserved without doubling protection.
    """
    def value(raw, default):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return default
        return max(0, min(100, int(raw)))
    c = c if isinstance(c, dict) else {}
    return max(value(c.get('kimi_weekly_reserve_percent'), 30),
               value(c.get('kimi_reserve_percent'), 0))


def kimi_weekly_reserve_state(c, q, now=None):
    """One authoritative reserved-threshold contract for dispatch, gate, CLI and UI.

    Existing display fields (``reserve_percent``, ``status``,
    ``remaining_percent``, ``source``, ``resets_at``) are preserved, and the
    effective-release fields are derived here only: ``effective_reserve_percent``,
    ``released_percent``, ``emergency_reserve_percent``, ``dynamic_enabled``,
    ``release_state``, ``remaining_hours`` and bounded demand metadata. Only a
    verified seven-day weekly reading with a trustworthy future reset inside the
    second half of its window, plus fresh quiet Ultra demand for the current
    account, may lower the threshold. Unknown, stale, future-dated or
    other-account evidence always keeps the configured base, and a verified plan
    without a weekly pool reports ``not_applicable`` instead of a simulated
    reserve. ``now`` is injectable so policy tests are deterministic; production
    callers use the current clock.
    """
    import kimi_reserve
    now = time.time() if now is None else float(now)
    base = kimi_weekly_reserve_percent(c)
    dynamic = kimi_reserve.dynamic_enabled(c)
    weekly = kimi_weekly_state(q, c)
    demand = None
    if dynamic and weekly.get('status') == 'known':
        try:
            identity = credential_identity(KIMI_PROVIDER)
        except Exception:
            identity = None
        demand = kimi_reserve.read_demand(identity, now)
    return kimi_reserve.compute_reserve_state(base, dynamic, weekly, demand, now)


def kimi_weekly_reserve_display(account_record=None, c=None):
    """Reserve state for the account API without any additional live sample.

    The console account endpoint already fetched one account reading; this helper
    never starts another network or Keychain call. It reuses the shared quota
    cache only when that cache is fresh and belongs to the current inference
    credential, so the existing telemetry fallback still applies. Otherwise it
    shapes the already-read account record into the same authoritative helper,
    which keeps a missing weekly window unknown rather than inventing a reserve.
    """
    if c is None:
        try:
            c = config()
        except Exception:
            c = {}
    state = None
    try:
        cached = read_json(STATE / 'quota.json', {})
        record = cached.get(KIMI_PROVIDER) if isinstance(cached, dict) else None
        checked = record.get('checked_at') if isinstance(record, dict) else None
        if isinstance(record, dict) and not isinstance(checked, bool) and \
                isinstance(checked, (int, float)) and 0 <= time.time() - checked <= KIMI_ACCOUNT_MAX_AGE and \
                record.get('_credential') == credential_identity(KIMI_PROVIDER):
            state = view({KIMI_PROVIDER: dict(record)})
    except Exception:
        state = None
    if state is None:
        raw = {'state': 'unavailable', 'stale': True, 'sampled_at': 0,
               'available': None, 'windows': []}
        record = _clean_account_record(account_record)
        if record is not None:
            raw['account'] = record
        state = {KIMI_PROVIDER: raw}
    return kimi_weekly_reserve_state(c, state)


def kimi_request_allowed(model, c, q, now=None):
    """Authoritative per-model Kimi admission shared by dispatch and the gate.

    Every Kimi model first passes the ordinary provider admission gate: an
    enforced-zero five-hour window, an unavailable account, a credential
    rejection, control-plane rate limiting, confirmed prepaid expiry or an open
    billing circuit blocks new requests even for the reserved model. Only after
    that, the reserved ``k3.1-pro`` Ultra model may use the protected final
    weekly slice, while every other Kimi model is paused at or below the
    effective weekly reserve and fails closed while the weekly reading is
    unknown; explicit profile requests cannot bypass either rule. A verified
    same-account ``month_total``/``month_code`` reading of real zero blocks every
    Kimi model including Ultra. Unauthenticated adapters, missing month windows
    and unverified bindings never become a fabricated zero.

    The reserve threshold is ``kimi_weekly_reserve_state``'s effective value, so
    the dynamic release (and its demand/credential gates) applies identically to
    pool dispatch, the loopback native gate, the CLI view and the console. That
    helper returns the base whenever the weekly window or demand evidence is not
    fully trustworthy, so this comparison can never widen admission on unknown
    data. A monthly billing circuit is never relaxed by naming a profile: the
    operator must run the explicit ``quota --retry-provider kimi-for-coding``
    release first, which clears the circuit for new attempts. That release never
    bypasses an empty window, an unavailable account, an auth failure or a
    verified zero account pool, so this helper always enforces the circuit
    itself.
    """
    if not isinstance(model, str) or not model.strip():
        return False, 'unknown_model'
    text = model.strip()
    provider, sep, name = text.partition('/')
    if sep and provider != KIMI_PROVIDER:
        return True, 'not_a_kimi_model'
    name = name if sep else text
    full = text if sep else KIMI_PROVIDER + '/' + name
    if routing.is_retired_model(full):
        return False, 'retired_model'
    # Base provider admission applies to EVERY Kimi model, the reserved Ultra
    # model included: an enforced-zero five-hour window, an unavailable account,
    # a credential rejection, control-plane rate limiting, confirmed prepaid
    # expiry or an open billing circuit must stop new requests/rounds too.
    # Ultra's only exemption is the weekly reserve below. allowed() no longer
    # contains a Kimi-reserve branch, so this call cannot recurse.
    admitted, why = allowed(KIMI_PROVIDER, q, 'ultra', threshold=0, prepaid=False)
    if not admitted:
        return False, why
    if kimi_account_total_state(q).get('blocks') is True:
        return False, 'kimi_account_total_exhausted'
    if name == KIMI_RESERVED_MODEL_ID:
        return True, 'weekly_reserve_beneficiary'
    reserve = kimi_weekly_reserve_state(c, q, now)
    if reserve['status'] == 'not_applicable':
        return True, 'weekly_not_applicable'
    if reserve['status'] != 'known':
        return False, 'kimi_weekly_unknown'
    if float(reserve['remaining_percent']) <= float(reserve['effective_reserve_percent']):
        return False, 'kimi_weekly_reserved_for_ultra'
    return True, 'kimi_weekly_available'


def kimi_ultra_low_weekly(q, c):
    """True when Ultra should drop to one concurrent task.

    Requires a fresh weekly reading at or below the low threshold AND a
    reset-aware runway below one (predicted not to last until the reset). Unknown
    or not-applicable weekly data never invents pressure.
    """
    threshold = c.get('kimi_low_weekly_threshold_percent', 5) if isinstance(c, dict) else 5
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        threshold = 5
    weekly = kimi_weekly_state(q, c)
    if weekly['status'] != 'known' or float(weekly['remaining_percent']) > float(threshold):
        return False, weekly['remaining_percent']
    window = weekly.get('window')
    runway = routing._window_runway(window, time.time()) if window else None
    return runway is None or runway < 1.0, weekly['remaining_percent']


def kimi_ultra_limit(c):
    """Concurrent Ultra cap while the weekly reserve is nearly exhausted."""
    def value(raw, default):
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 16:
            return default
        return raw
    c = c if isinstance(c, dict) else {}
    return value(c.get('kimi_low_weekly_ultra_limit'),
                 value(c.get('kimi_low_weekly_k3_limit'), 1))


def _clean_account_record(record):
    """Sanitized account-adapter schema from one already-fetched reading."""
    if not isinstance(record, dict):
        return None
    sampled = record.get('sampled_at')
    sampled = _epoch(sampled)  # Missing/invalid sample time must never become fresh.
    cleaned = {
        'state': record.get('state') if record.get('state') in ('ok', 'unavailable') else 'unavailable',
        'source': record.get('source') if isinstance(record.get('source'), str) else 'unknown',
        'sampled_at': sampled,
        'windows': [dict(w) for w in record.get('windows') or [] if isinstance(w, dict)][:8],
        'weekly_applicability': record.get('weekly_applicability')
            if record.get('weekly_applicability') in ('applicable', 'not_applicable', 'unknown') else 'unknown',
        'account_binding': record.get('account_binding')
            if isinstance(record.get('account_binding'), dict) else {'state': 'unverified'},
        'available': record.get('available') if isinstance(record.get('available'), bool) else None,
    }
    account = record.get('account')
    if isinstance(account, dict):
        cleaned['account'] = {key: str(account[key])[:80] for key in ('nickname', 'userLevelName')
                              if isinstance(account.get(key), str)}
    error = record.get('error')
    if isinstance(error, str) and error:
        cleaned['error'] = error[:120]
    elif error:
        cleaned['error'] = 'account_reading_error'
    if 'stale' in record:
        cleaned['stale'] = record['stale'] is not False
    if record.get('live_error'):
        cleaned['live_error'] = {'state': 'unavailable'}
    if 'http_status' in record:
        status = number(record['http_status'])
        if status is None or isinstance(record['http_status'], bool):
            cleaned['stale'] = True
        else:
            cleaned['http_status'] = status
    return cleaned


def _kimi_account_snapshot(previous=None):
    """Account adapter reading for the cached view; never fabricates a reading.

    Only the adapter's normalized, already-sanitized schema is carried. A failed
    or unavailable fetch keeps the previous record with its original sample time
    so consumers age it honestly; a missing month window stays absent.
    """
    try:
        import kimi_account
        record = kimi_account.fetch()
    except Exception:
        record = None
    if not isinstance(record, dict):
        return previous if isinstance(previous, dict) else None
    return _clean_account_record(record)


def tier_of(t):
    """Work tier used by legacy routing and by an opt-in routing_policy."""
    if t.get('tier') in ('fast', 'normal', 'deep', 'ultra'):
        return 'background' if t['tier'] == 'normal' else t['tier']
    return 'deep' if t.get('complexity') == 'deep' else 'fast' if t.get('urgency') == 'fast' else 'background'


def _tier(t):  # Backward-compatible private alias.
    return tier_of(t)


def _profile_name(t, c):
    requested = t.get('requested_profile', 'auto')
    legacy = c.get('routing', {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'})
    tier = tier_of(t)
    profile = requested if requested != 'auto' else legacy.get(tier)
    if profile is None and tier == 'ultra':
        # A legacy single-profile routing record has no ultra key: the reserved
        # native model is the only legitimate Ultra destination.
        profile = next((name for name, value in (c.get('profiles') or {}).items()
                        if isinstance(value, dict) and value.get('model') == routing.KIMI_RESERVED_MODEL), None)
    return profile if profile in c['profiles'] else None


def _preferred_name(t, c):
    """Most preferred configured profile for a task, following policy order when set.

    Guidance only: this never consumes admission counters and never switches a task.
    """
    requested = t.get('requested_profile', 'auto')
    if requested != 'auto':
        return _profile_name(t, c)
    stages = (routing.configured(c) or {}).get(tier_of(t), [])
    for entry in (e for stage in stages for e in stage):
        p = c['profiles'].get(entry['profile'])
        if isinstance(p, dict) and p.get('enabled') is not False and p.get('model'):
            return entry['profile']
    return _profile_name(t, c)


def _task_provider(t, c):
    """Configured provider for the task's pinned or routed profile, with its name."""
    name = t.get('profile')
    if name not in c['profiles']:
        name = _preferred_name(t, c)
    if not name or name not in c['profiles'] or not c['profiles'][name].get('model'):
        return None, None
    return c['profiles'][name]['model'].split('/', 1)[0], name


def _active_reason(provider):
    block = billing_block(provider) if provider else None
    return (block.get('reason') or GENERIC_REASON) if block else None


def _compact_billing_kind(error):
    kind = str(error.get('billing_reason') or '')
    if kind:
        return kind
    if error.get('billing'):
        return GENERIC_REASON
    try:
        import diagnostics  # Local import avoids a module cycle; classification stays single-sourced.
        return diagnostics.billing_kind_message(error.get('http_status'), error.get('message'))
    except Exception:
        return None


def _task_billing_kind(t):
    reason = str(t.get('reason') or '')
    if reason.startswith('provider_billing_'):
        return reason[len('provider_billing_'):] or GENERIC_REASON
    for e in t.get('errors') or []:
        if isinstance(e, dict) and e.get('source') == 'model':
            kind = _compact_billing_kind(e)
            if kind:
                return kind
    return GENERIC_REASON


def _default_tier_profile(tier, c):
    """Implicit legacy default for one tier, using the ordinary resolver.

    Old three-tier installs may carry no ``routing`` record and no
    ``routing_policy``. ``_profile_name`` then falls back to its built-in
    fast/background/deep map (plus the reserved Ultra model). The capability
    floor must agree with that resolver instead of building an empty eligible
    set that rejects every Deep or Ultra task; the independent retired-model
    admission check still runs after this floor.
    """
    return _profile_name({'requested_profile': 'auto', 'tier': tier}, c)


def effective_capability_floor(t):
    """The capability floor that admits a task, including the tier's implicit minimum.

    Deep and Ultra describe an admission capability, including retained tasks created
    before explicit floors existed. A weaker explicit floor cannot turn an automatic
    fallback into an implicit downgrade below the tier's settled floor: the implicit
    minimum for a Deep task is Deep, and the implicit minimum for an Ultra task is
    Deep. That Deep minimum is exactly the authorized one-level Ultra capacity
    fallback: a default Ultra task may continue on the Deep primary when Ultra
    capacity is CONFIRMED exhausted (see ``ultra_capacity_fallback_enabled``), and it
    may never be routed below Deep. An explicit ``capability_floor=ultra`` stays a
    hard requirement: it is never raised, never relaxed, and never falls back.
    """
    floor = t.get('capability_floor')
    ranks = routing.CAPABILITY_RANKS
    task_tier = tier_of(t)
    minimum = 'deep' if task_tier in ('deep', 'ultra') else None
    if minimum is not None and ranks.get(floor, -1) < ranks[minimum]:
        return minimum
    return floor


# The bridge accepts only these stop triggers as confirmed capacity evidence;
# they mirror the worker's park gate. Auth, permission and unknown delivery
# errors never appear here and therefore never authorize a capacity fallback.
CAPACITY_STOP_TRIGGERS = frozenset(('provider_usage_or_rate_limit',
                                    'provider_billing_monthly_usage_limit',
                                    'provider_billing_insufficient_balance'))
# The proof horizon mirrors allowed()'s fresh_enough window and the recovery path:
# only a recent same-credential reading can prove exhaustion is happening NOW.
CAPACITY_PROOF_MAX_AGE = 900


def _capacity_circuit_is_auth(provider):
    block = billing_block(provider) if provider else None
    return bool(block) and block.get('reason') == AUTH_REASON


def _capacity_record_is_fresh(provider, q, now=None):
    """True only for a recent, non-stale, non-auth provider reading."""
    record = (q or {}).get(provider) if isinstance(q, dict) else None
    if not isinstance(record, dict) or record.get('stale', True):
        return False
    telemetry = record.get('billing') if isinstance(record.get('billing'), dict) else {}
    if telemetry.get('telemetry_state') == 'auth_error' or record.get('state') == 'auth_error':
        return False
    now = time.time() if now is None else now
    sampled = record.get('sampled_at')
    return (isinstance(sampled, (int, float)) and not isinstance(sampled, bool) and
            math.isfinite(sampled) and sampled <= now and
            now - sampled <= CAPACITY_PROOF_MAX_AGE)


def _fresh_zero_capacity_window(provider, q, now=None):
    """Prove a real same-credential zero capacity right now.

    A valid window whose remaining reads zero, or a verified Kimi account-total
    zero through the existing authoritative helpers, proves exhaustion. A generic
    ``available is False`` with no zero window proves nothing; stale retained
    zeros, future or unbound samples prove nothing either.

    ``view()`` strips private underscore fields, so the credential binding cannot
    travel inside ``q``. It is re-validated from the raw captured state instead:
    the raw record must describe the SAME sample time the view shows, must carry
    the sampler identity binding, and that binding must still match the current
    telemetry credential. No identity material is copied into any returned value.
    """
    record = (q or {}).get(provider) if isinstance(q, dict) else None
    if not isinstance(record, dict) or not _capacity_record_is_fresh(provider, q, now):
        return False

    def zero(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool) and
                math.isfinite(value) and float(value) <= 0)

    def bound_zero_window():
        raw = read_json(STATE / 'quota.json', {}).get(provider)
        if not isinstance(raw, dict) or raw.get('sampled_at') != record.get('sampled_at'):
            return False
        bound = raw.get('_credential')
        if not bound:
            # No inference/sampler binding: the reading could belong to another account.
            return False
        try:
            return _telemetry_identity(provider) == bound
        except Exception:
            return False
    if any(isinstance(w, dict) and w.get('valid') is not False and
           (zero(w.get('remaining')) or zero(w.get('remaining_percent')))
           for w in record.get('windows') or []):
        return bound_zero_window()
    if provider != KIMI_PROVIDER:
        return False
    # Reuse the existing authoritative account helpers: a verified binding to the
    # current inference account plus a fresh real month-pool zero is proof even
    # when the inference-window sampler itself has no zero row.
    if kimi_account_record(q) is None:
        return False
    total = kimi_account_total_state(q)
    return total.get('authority') == 'verified' and total.get('blocks') is True


def _capacity_exclusion_proof(t, provider):
    """True only for an exclusion the bridge itself recorded from a capacity stop.

    The exclusion must carry a capacity stop trigger, its original credential
    identity, and that identity must still match the current one. Operator or
    coordinator exclusions without this provenance never authorize a fallback.
    """
    record = (t.get('_capacity_exclusions') or {}).get(provider)
    if not isinstance(record, dict):
        return False
    if record.get('reason') not in CAPACITY_STOP_TRIGGERS:
        return False
    identity = record.get('identity')
    if not identity:
        return False
    try:
        current = credential_identity(provider)
    except Exception:
        return False
    return current == identity


def is_capacity_exhaustion_reason(why, provider=None, t=None, q=None, now=None):
    """Re-prove ongoing capacity exhaustion for the authorized Ultra -> Deep fallback.

    The caller never trusts the bare denial string: ``allowed()`` returns
    ``quota_exhausted_or_account_unavailable`` for merely ``available is False``
    or a stale retained zero, so this helper re-validates the evidence NOW:

    * a fresh same-credential zero window or a verified Kimi account-total zero;
    * an open monthly/balance billing circuit -- an AUTH circuit is never
      exhaustion, even alongside stale or zero telemetry;
    * a bailian slot-generation waiting answer (admission already required a
      pool sample at most 90 seconds old before returning it);
    * a task-scoped continuation exclusion whose bridge-recorded capacity stop
      trigger and credential identity still match, while the provider's current
      telemetry is not an auth rejection.

    Disabled or retired profiles, manual routing-guard exclusions, reserved-for-
    Fast reservations, credential rejections, control-plane rate limits, generic
    unavailable answers, stale or unbound readings, identity mismatches and
    unknown telemetry all fail closed: the Ultra task waits or reports the real
    blocker instead of moving to Max.
    """
    if _capacity_circuit_is_auth(provider):
        return False
    if why == 'quota_exhausted_or_account_unavailable':
        return _fresh_zero_capacity_window(provider, q, now)
    if why == 'kimi_account_total_exhausted':
        # Reuse the authoritative helpers end to end: the month-pool zero counts
        # only while the ACCOUNT reading itself is fresh and authenticated, as
        # well as the provider record being fresh and non-auth right now.
        return (kimi_account_record(q or {}) is not None and
                kimi_account_total_state(q or {}).get('blocks') is True and
                _capacity_record_is_fresh(provider, q, now))
    if why == 'bailian_slots_waiting_or_unavailable':
        # admission returns this for a missing/stale pool sample too; only a
        # pool reading inside the same 90-second freshness horizon counts.
        record = (q or {}).get(provider) if isinstance(q, dict) else None
        pool = record.get('inference_pool') if isinstance(record, dict) else None
        at = pool.get('sampled_at') if isinstance(pool, dict) else None
        now = time.time() if now is None else now
        return (isinstance(at, (int, float)) and not isinstance(at, bool) and
                0 <= now - at <= 90)
    if why == 'provider_billing_blocked':
        block = billing_block(provider) if provider else None
        return bool(block) and block.get('reason') in (MONTHLY_REASON, BALANCE_REASON)
    if why == 'continuation_excluded_provider':
        if not (provider and _capacity_exclusion_proof(t or {}, provider)):
            return False
        record = (q or {}).get(provider) if isinstance(q, dict) else None
        if isinstance(record, dict):
            telemetry = record.get('billing') if isinstance(record.get('billing'), dict) else {}
            if telemetry.get('telemetry_state') == 'auth_error' or \
                    record.get('state') == 'auth_error':
                return False
        return True
    return False


def ultra_capacity_fallback_enabled(t, c):
    """The authorized default Ultra -> Deep one-level capacity fallback.

    Applies only to automatic Ultra work with no explicit Ultra floor. Explicit
    model pins never reach this path (``route`` keeps a pin or fails; it does not
    re-chain), an explicit ``capability_floor=ultra`` stays a hard requirement,
    and an operator may disable the chain with ``ultra_fallback_to_deep=false``
    to restore pure waiting. Landing remains Deep-primary-only: this never routes
    Ultra below Deep.
    """
    c = c if isinstance(c, dict) else {}
    return (tier_of(t) == 'ultra' and (t.get('requested_profile') or 'auto') == 'auto'
            and effective_capability_floor(t) != 'ultra'
            and c.get('ultra_fallback_to_deep', True) is not False)


def meets_capability_floor(name, t, c):
    floor = effective_capability_floor(t)
    if floor not in ('normal', 'deep', 'ultra'):
        return True
    if floor in ('deep', 'ultra'):
        # Physical model capability outranks stage membership: a known lower model
        # can never satisfy a Deep or Ultra floor just because a legacy or edited
        # policy places it in a primary stage, and an explicit Ultra floor keeps
        # rejecting Max/Flash even if Kimi is disabled or removed. Unknown models
        # make no claim and stay admitted by membership, preserving generic setups.
        if not routing.meets_capability_rank((c.get('profiles') or {}).get(name), floor):
            return False
    policy = routing.configured(c) or {}
    eligible = set()
    # A capability floor means the profile must sit in the PRIMARY stage of a tier
    # at or above the requested capability. An Ultra floor accepts only the
    # configured Ultra stage itself; the Deep floor accepts the Ultra stage and the
    # Deep stage, which is exactly the authorized one-level fallback landing zone.
    tiers = {'normal': ('background', 'deep', 'ultra'),
             'deep': ('deep', 'ultra'),
             'ultra': ('ultra',)}[floor]
    for tier in tiers:
        stages = policy.get(tier) or []
        if stages:
            eligible.update(entry['profile'] for entry in stages[0])
            continue
        mapped = (c.get('routing') or {}).get(tier)
        if mapped:
            eligible.add(mapped)
            continue
        # This tier has neither a policy stage nor an explicit mapping. Infer
        # the same implicit default the ordinary resolver uses for it, so old
        # three-tier installs keep their default Deep/Ultra admissions without
        # letting a lower-tier fallback satisfy a higher-capability floor. The
        # inference is per tier: an explicit Ultra mapping must not shadow a
        # legitimate implicit Deep primary.
        inferred = _default_tier_profile(tier, c)
        if inferred:
            eligible.add(inferred)
    return name in eligible


def _other_plan_provider_available(t, c, q, provider, spill_provider):
    """True when the task tier still has a usable subscription peer besides provider.

    The configured spill target is the pay-as-you-go fallback, so it is not a
    subscription peer here: this answers the "only this plan remains" question.
    Capability floors, routing guards and provider admission all apply, but the
    check never consumes credits or changes state.
    """
    policy = routing.configured(c) or {}
    prepaid = prepaid_providers(c)
    for entry in routing.tier_entries(policy, tier_of(t)):
        name = entry['profile']
        profile = (c.get('profiles') or {}).get(name) or {}
        if profile.get('enabled') is False:
            continue
        model = profile.get('model')
        other = str(model).split('/', 1)[0] if isinstance(model, str) and '/' in str(model) else None
        if not other or other == provider or other == spill_provider:
            continue
        if not meets_capability_floor(name, t, c):
            continue
        if name in (t.get('excluded_profiles') or []) or other in (t.get('excluded_providers') or []):
            continue
        if allowed(other, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0),
                   prepaid=other in prepaid)[0]:
            return True
    return False


def _allowed_profile(name, t, c, q):
    """(provider, ok, why) for one enabled profile, respecting quota and circuits."""
    p = c.get('profiles', {}).get(name)
    if not isinstance(p, dict) or p.get('enabled') is False or not p.get('model'):
        return None, False, 'profile_disabled_or_missing'
    provider = str(p['model']).split('/', 1)[0]
    if not meets_capability_floor(name, t, c):
        return provider, False, 'capability_floor_unavailable'
    if name in (t.get('excluded_profiles') or []):
        return provider, False, 'routing_guard_excluded_profile'
    if provider in (t.get('excluded_providers') or []):
        return provider, False, 'continuation_excluded_provider'
    model = p.get('model')
    if routing.is_retired_model(model):
        # Historical records stay intact, but a retired model is never a
        # new-task entry, an automatic fallback or a continuation target.
        return provider, False, 'retired_model'
    if provider == KIMI_PROVIDER:
        kimi_ok, kimi_reason = kimi_request_allowed(model, c, q)
        if not kimi_ok:
            return provider, False, kimi_reason
    policy = routing.configured(c) or {}
    spill = routing.configured_spillover(c, policy)
    prepaid = prepaid_providers(c)
    fast = (policy.get('fast') or [[]])[0]
    fast_names = {entry['profile'] for entry in fast}
    fast_providers = {str((c.get('profiles', {}).get(n) or {}).get('model', '')).split('/')[0]
                      for n in fast_names}
    # Admission protection must survive loss of the peer. Weight-only protection
    # is bypassed when capacity failover leaves a single expensive candidate.
    # Prepaid capacity is neutral for this calendar reservation: another plan's
    # scarcity must not reserve a prepaid provider for Fast.
    if provider not in prepaid and (t.get('requested_profile') or 'auto') == 'auto' and \
            spill and spill.get('enabled') and \
            len(fast_providers) == 1 and provider in fast_providers and \
            (name not in fast_names or tier_of(t) != 'fast'):
        import economics
        fit = economics._window_fit(provider, q.get(provider) or {})
        runway = number(fit.get('pressure_runway'))
        endpoint = float(spill.get('level2_runway_percent') or 0) / 100 * .9
        window = economics._selected_window(provider, q.get(provider) or {}) or {}
        durable = (number(window.get('duration_minutes')) or 0) > 1440
        if durable and fit.get('status') == 'ok' and runway is not None and endpoint > 0:
            # A plan that is behind its own reset schedule loses its expensive
            # automatic candidates first. The recent sample may be cheap because
            # cheap work ran, so while the plan is behind schedule and no other
            # subscription peer exists, the candidate's configured cost multiple
            # is charged against the same endpoint. The Fast source itself and
            # near-reset recovery stay on the ordinary smooth curve, and unknown
            # cost or a stale plan is never treated as exhausted.
            pressured = runway <= endpoint
            if not pressured and fit.get('schedule_scarce'):
                spill_profile = (c.get('profiles', {}).get(spill.get('profile')) or {})
                spill_model = spill_profile.get('model') if isinstance(spill_profile, dict) else None
                spill_provider = str(spill_model).split('/', 1)[0] if isinstance(spill_model, str) \
                    and '/' in spill_model else None
                multiple = economics.candidate_cost_multiple(c, provider, name, fast_names)
                if multiple > 1 and not _other_plan_provider_available(t, c, q, provider, spill_provider):
                    pressured = runway / multiple <= endpoint
            if pressured:
                return provider, False, 'provider_reserved_for_fast'
    ok, why = allowed(provider, q, t.get('complexity', 'normal'),
                      c.get('kimi_reserve_percent', 0), prepaid=provider in prepaid)
    return provider, ok, why


def _first_available_policy_stage(tier, t, c, q, policy):
    """Return the first usable stage without advancing its admission counter."""
    for index, stage in enumerate((policy or {}).get(tier) or []):
        candidates = []
        providers = {}
        for entry in stage:
            provider, ok, why = _allowed_profile(entry['profile'], t, c, q)
            if ok:
                candidates.append((entry, why))
                providers[entry['profile']] = provider
        if candidates:
            return candidates, providers, index
    return [], {}, None


def _level2_conservation_active(tier, spill_config, guidance, prepaid_available=False):
    """Second-level load shedding: Normal temporarily reuses the Fast source pool.

    Level one is the ordinary bounded spillover. Level two is deliberately narrower:
    it affects only automatic Normal work, requires complete reset-aware telemetry,
    and never changes Deep routing or an explicit profile request. A Normal stage
    with usable prepaid capacity is not downgraded merely because a renewable
    plan elsewhere is low.
    """
    if prepaid_available or tier != 'background' or not spill_config or \
            not spill_config.get('enabled') or \
            tier not in spill_config.get('tiers', ()) or \
            guidance.get('quota_posture') != 'fast_preferred' or \
            guidance.get('conservation_refill_safe'):
        return False
    if guidance.get('conservation_level') == 2:
        return True
    threshold = guidance.get('level2_runway_percent', spill_config.get('level2_runway_percent', 0))
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold <= 0:
        return False
    combined = guidance.get('capacity_pressure_percent', guidance.get('combined_remaining_percent'))
    if (guidance.get('capacity_pressure_percent') is not None or guidance.get('combined_remaining_complete')) and isinstance(combined, (int, float)) and \
            not isinstance(combined, bool) and math.isfinite(combined):
        return float(combined) <= float(threshold)
    runways = guidance.get('runway') or {}
    values = list(runways.values()) if isinstance(runways, dict) else []
    if not values or any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                         not math.isfinite(value) for value in values):
        return False
    return max(float(value) for value in values) <= float(threshold) / 100.0


def _spillover_runway_view(guidance, providers, source_specific=False):
    """Return the pressure signal appropriate for one tier's actual sources.

    Normal's multi-plan stage can use the combined pool because its adaptive pair
    can move work between subscriptions. Fast has only its configured source, so a
    healthy Kimi plan must not hide an exhausted Ark Auto allowance.
    """
    if source_specific:
        runways = guidance.get('provider_runway') or guidance.get('runway') or {}
        return {provider: runways.get(provider) for provider in providers}
    combined = guidance.get('capacity_pressure_percent', guidance.get('combined_remaining_percent'))
    if (guidance.get('capacity_pressure_percent') is not None or guidance.get('combined_remaining_complete')) and isinstance(combined, (int, float)) and \
            not isinstance(combined, bool) and math.isfinite(combined):
        return {provider: max(0.0, float(combined) / 100.0) for provider in providers}
    return guidance.get('runway') or {}


def _spillover_source(guidance, providers, tier, level2):
    """Choose the runway signal and optional source that fallback may replace.

    A multi-plan Normal stage should continue using a healthy subscription, but
    one healthy plan must not force a depleted peer to keep its full share. In
    that mixed state, fallback replaces only the constrained provider. As reset
    approaches, its reset-aware runway rises and the replacement fades away.
    """
    guidance = guidance if isinstance(guidance, dict) else {}
    providers = set(providers)
    source_specific = tier == 'fast' or level2 or len(providers) == 1
    view = _spillover_runway_view(guidance, providers, source_specific)
    replacement = None
    if not source_specific and len(providers) > 1:
        specific = _spillover_runway_view(guidance, providers, True)
        values = {provider: specific.get(provider) for provider in providers}
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) and
               math.isfinite(value) for value in values.values()):
            threshold = float(guidance.get('runway_threshold_percent') or 0) / 100.0
            constrained = min(values, key=values.get)
            if threshold > 0 and values[constrained] < threshold <= max(values.values()):
                source_specific = True
                view = specific
                replacement = constrained
    values = ([view.get(replacement)] if replacement else
              [view.get(provider) for provider in providers])
    runway = max((float(value) for value in values
                  if isinstance(value, (int, float)) and not isinstance(value, bool) and
                  math.isfinite(value)), default=None)
    return source_specific, view, replacement, runway


def _provider_conservation_level(guidance, source_specific, runway):
    """Return one provider pool's load-shedding level, independent of global capacity."""
    if not source_specific or isinstance(runway, bool) or not isinstance(runway, (int, float)) or \
            not math.isfinite(runway):
        return 0
    threshold = guidance.get('runway_threshold_percent', 0) if isinstance(guidance, dict) else 0
    level2 = guidance.get('level2_runway_percent', 0) if isinstance(guidance, dict) else 0
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) for value in (threshold, level2)):
        return 0
    if level2 > 0 and float(runway) <= float(level2) / 100.0:
        return 2
    return 1 if threshold > 0 and float(runway) < float(threshold) / 100.0 else 0


def _level2_spillover_cap(c, q, spill_config, target_provider, runway=None, now=None):
    """Return a price-aware Level 2 cap that yields to severe plan pressure.

    Off-peak may use the configured high cap throughout Level 2. At peak prices
    the cap starts at the Level 1 ceiling, then rises continuously to the same
    high cap once source-plan runway falls below three quarters of the Level 2 threshold.
    """
    base = int(spill_config.get('max_share_percent') or 0)
    if target_provider != 'deepseek':
        return base
    high = spill_config.get('level2_offpeak_share_percent', base)
    floor = spill_config.get('level2_min_balance_cny', 0)
    if isinstance(high, bool) or not isinstance(high, int) or high < base:
        return base
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not math.isfinite(floor):
        return base
    record = q.get('deepseek') if isinstance(q, dict) else None
    if not isinstance(record, dict) or record.get('stale', True) or record.get('available') is not True:
        return base
    balances = [item.get('remaining') for item in record.get('balances') or []
                if isinstance(item, dict) and item.get('currency') == 'CNY' and
                isinstance(item.get('remaining'), (int, float)) and
                not isinstance(item.get('remaining'), bool) and math.isfinite(item.get('remaining'))]
    if not balances or sum(balances) < float(floor):
        return base
    try:
        import economics
        peak = economics.deepseek_peak(time.time() if now is None else now)
    except Exception:
        return base
    high = min(routing.MAX_LEVEL2_SPILLOVER_SHARE, high)
    if not peak:
        return high
    threshold = float(spill_config.get('level2_runway_percent') or 0) / 100.0
    if isinstance(runway, bool) or not isinstance(runway, (int, float)) or \
            not math.isfinite(runway) or threshold <= 0:
        return base
    critical = threshold * 0.75
    progress = min(1.0, max(0.0, (threshold - float(runway)) /
                            max(0.000001, threshold - critical)))
    return int(round(base + (high - base) * progress))


def _dynamic_runway_override(tier, provider_by_profile, q, guidance=None, now=None):
    """Use durable per-pool pressure for adaptive Normal and Deep balancing.

    Kimi's short and weekly counters are shared by K2.8 and K3. A Normal burst
    can therefore tighten the five-hour counter even when K3 caused little of
    it. Balancing follows each provider's durable weekly/monthly fit; immediate
    exhausted windows are still filtered by ``allowed`` before this function.

    A smooth local-pressure penalty is applied below the ordinary runway
    threshold. This lets a healthy peer absorb nearly all same-tier work when
    one subscription pool is severely constrained, without falsely declaring
    the combined high-capability pool degraded. When both providers have the
    same pressure their ratio stays unchanged and global conservation remains
    responsible for lower-tier spillover.
    """
    if tier not in ('background', 'deep'):
        return None
    import economics
    now = time.time() if now is None else now
    providers = set(provider_by_profile.values())
    guidance_runways = ((guidance or {}).get('provider_runway') or
                        (guidance or {}).get('runway')) if isinstance(guidance, dict) else None
    prepaid = set((guidance or {}).get('prepaid_providers') or ()) \
        if isinstance(guidance, dict) else set()
    values = {}
    for provider in providers:
        if provider in prepaid:
            # Positive callable prepaid capacity is neutral for dynamic weights;
            # actual zero/expiry/credential gates already ran in admission.
            values[provider] = 1.0
            continue
        runway = guidance_runways.get(provider) if isinstance(guidance_runways, dict) else None
        if isinstance(runway, bool) or not isinstance(runway, (int, float)) or not math.isfinite(runway):
            fitted = economics._window_fit(provider, q.get(provider) or {}, now)
            runway = number(fitted.get('pressure_runway')) if fitted.get('status') == 'ok' else None
        values[provider] = runway if runway is not None and runway >= 0 else None
    threshold = (guidance or {}).get('runway_threshold_percent') if isinstance(guidance, dict) else None
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) and \
            math.isfinite(threshold) and threshold > 0:
        threshold = float(threshold) / 100.0
        for provider, runway in values.items():
            if runway is None or runway >= threshold:
                continue
            ratio = max(0.0, min(1.0, runway / threshold))
            smooth = ratio * ratio * (3.0 - 2.0 * ratio)
            values[provider] = runway * smooth
    # Protect a sole Fast source independently of relative peer pressure.
    # Otherwise two depleted pools converge to their baseline and re-admit
    # expensive work to the pool that must also serve Fast. This continuous
    # reservation reaches zero at the same critical endpoint as Fast spillover.
    fast_sources = set(((guidance or {}).get('fast_stage') or {}).get('configured') or [])
    critical = number((guidance or {}).get('level2_runway_percent'))
    if len(fast_sources) == 1 and len(providers) > 1 and critical and threshold:
        reserved = next(iter(fast_sources))
        original = guidance_runways.get(reserved) if isinstance(guidance_runways, dict) else None
        peers = [v for p, v in values.items() if p != reserved and v is not None and v > 0]
        endpoint = critical / 100.0 * .90
        if reserved in values and peers and number(original) is not None and threshold > endpoint:
            fraction = max(0.0, min(1.0, (original - endpoint) / (threshold - endpoint)))
            reserve_factor = fraction * fraction * (3.0 - 2.0 * fraction)
            if values[reserved] is not None:
                values[reserved] *= reserve_factor
    return values


def _conserved_deep_stage(c, q, stage, guidance, task=None):
    """Conserve only where the replacement satisfies the task capability floor.

    Preserve an admissible original candidate rather than replacing it with a
    lower-capability profile that admission will subsequently reject.
    """
    if int((guidance or {}).get('conservation_level') or 0) < 2:
        return stage
    policy = routing.configured(c) or {}
    normal = (policy.get('background') or [[]])[0]
    prepaid = set((guidance or {}).get('prepaid_providers') or ())
    replacements = {}
    for entry in normal:
        profile = c.get('profiles', {}).get(entry['profile']) or {}
        provider = str(profile.get('model', '')).split('/', 1)[0]
        if profile.get('enabled') is not False:
            replacements.setdefault(provider, entry['profile'])
    result = []
    for entry in stage:
        profile = c.get('profiles', {}).get(entry['profile']) or {}
        provider = str(profile.get('model', '')).split('/', 1)[0]
        # Global conservation must never substitute a usable prepaid Max stage
        # with its cheaper Flash sibling merely because a renewable plan is low.
        replacement = entry['profile'] if provider in prepaid else \
            replacements.get(provider, entry['profile'])
        if not meets_capability_floor(replacement, task or {'tier': 'deep'}, c):
            replacement = entry['profile']
        result.append({**entry, 'profile': replacement})
    return result


def _ultra_capacity_fallback(t, c, q, policy, batch):
    """Land an automatic, CONFIRMED-exhausted Ultra task on the Deep tier's own route.

    Only reached through the capacity-proof gate, never from an explicit pin or an
    explicit Ultra floor, and never below Deep: every landing candidate re-runs the
    full profile admission (capability floor with physical model semantics, the
    retired gate, provider quota/circuit, task exclusions), so only a Deep-or-above
    primary can pass. Conservation substitution and spillover blending are
    deliberately not applied here -- they are same-tier load logic and could shed
    to a cheaper sibling; this chain is a one-level capacity fallback. The Ultra
    tier's own admission credits carry the choice so Deep pool accounting is never
    polluted by Ultra fallback traffic.
    """
    stages = (policy or {}).get('deep')
    if not stages:
        name = _default_tier_profile('deep', c)
        if not name:
            return None, None
        stages = [[{'profile': name, 'weight': 1}]]
    for index, stage in enumerate(stages):
        candidates = []
        for entry in stage:
            record = (c.get('profiles') or {}).get(entry['profile']) or {}
            # Physical landing allowlist: this user-authorized step-down lands
            # exactly on the known Deep primary model (Qwen3.8 Max class), never
            # on an unknown or custom profile that merely sits in a Deep stage.
            # Unknown-rank compatibility remains for the generic capability
            # floors elsewhere; here an unknown Deep primary is a negative: the
            # chain refuses and the Ultra task waits instead.
            if routing.model_capability(record.get('model')) != 'deep':
                continue
            _, ok, why = _allowed_profile(entry['profile'], t, c, q)
            if ok:
                candidates.append((entry, why))
        if not candidates:
            continue
        entries = [entry for entry, _ in candidates]
        if batch is not None:
            chosen, credits = routing.advance(entries, batch.credits_for('ultra'))
            batch.propose('ultra', credits)
        else:
            chosen, _ = routing.advance(entries, routing.Admissions.load().credits.get('ultra', {}))
        why = next(reason for entry, reason in candidates if entry['profile'] == chosen)
        return chosen, 'routing_policy:ultra_fallback_deep:stage' + str(index) + ':' + why
    return None, None


def _policy_route(t, c, q, stages, batch=None):
    """First stage with an admissible candidate, then a weighted choice within it.

    Never consumes counters here: with a daemon batch the advance is only proposed and
    choose_ready commits it after the owner/scope checks pass, while a preview call
    (batch=None) reads the stored credits without writing them. Within one batch each
    committed proposal moves the credits, so subsequent choices distribute correctly.

    Inside a stage the stored weights are the baseline; quota-aware dynamics scale
    them in memory by each candidate's current valid headroom. The stored policy is
    never rewritten, explicit profiles stay pinned and a running task is never
    migrated.
    """
    preview = None
    first_reason = None
    tier = tier_of(t)
    policy = routing.configured(c)
    spill_config = routing.configured_spillover(c, policy)
    spill_guidance = None
    ultra_rejections = []
    for index, stage in enumerate(stages):
        if tier == 'deep' and index == 0:
            spill_guidance = spill_guidance or tier_guidance(c, q)
            stage = _conserved_deep_stage(c, q, stage, spill_guidance, t)
        candidates = []
        for entry in stage:
            provider, ok, why = _allowed_profile(entry['profile'], t, c, q)
            if ok:
                candidates.append((entry, why))
            elif first_reason is None:
                first_reason = why
            if tier == 'ultra' and not ok:
                ultra_rejections.append((provider, why))
        if candidates:
            base_entries = [entry for entry, _ in candidates]
            provider_by_profile = {}
            for entry, _ in candidates:
                profile = c['profiles'].get(entry['profile']) or {}
                provider_by_profile[entry['profile']] = str(profile.get('model', '')).split('/', 1)[0]
            level2 = False
            if spill_config and spill_config['enabled'] and index == 0:
                spill_guidance = spill_guidance or tier_guidance(c, q)
                prepaid_available = any(
                    provider in (spill_guidance.get('prepaid_providers') or ())
                    for provider in provider_by_profile.values())
                if _level2_conservation_active(tier, spill_config, spill_guidance,
                                               prepaid_available=prepaid_available):
                    fast_candidates, fast_providers, fast_stage_index = _first_available_policy_stage(
                        'fast', t, c, q, policy)
                    if fast_candidates:
                        candidates = fast_candidates
                        base_entries = [entry for entry, _ in candidates]
                        provider_by_profile = fast_providers
                        level2 = True
            adaptive = routing.dynamic_stage(
                c, 'fast' if level2 else tier,
                fast_stage_index if level2 else index, policy)
            if adaptive and spill_guidance is None:
                spill_guidance = tier_guidance(c, q)
            effective_tier = 'fast' if level2 else tier
            entries, dynamic_reason, _ = routing.dynamics(
                base_entries, provider_by_profile, q, adaptive=adaptive,
                runway_override=_dynamic_runway_override(
                    effective_tier, provider_by_profile, q, spill_guidance))
            if level2:
                dynamic_reason = 'quota_conservation_level2:' + dynamic_reason
            # A later fallback may absorb a bounded share before subscriptions hit
            # zero. Explicit profile requests never reach this function, and only
            # stage zero is blended; once a stage is unavailable, normal ordered
            # fallback semantics remain in charge.
            if spill_config and spill_config['enabled'] and index == 0 and tier in spill_config['tiers']:
                target = spill_config['profile']
                target_provider, target_ok, target_reason = _allowed_profile(target, t, c, q)
                source_providers = set(provider_by_profile.values())
                if target_ok and target_provider not in source_providers:
                    spill_guidance = spill_guidance or tier_guidance(c, q)
                    source_specific, runway_view, replacement_provider, source_runway = \
                        _spillover_source(spill_guidance, source_providers, tier, level2)
                    global_level = int(spill_guidance.get('conservation_level') or 0)
                    # A mixed same-tier stage first moves work to its healthy
                    # subscription peer. Cash fallback for only the constrained
                    # member remains a global-conservation action; provider-local
                    # spillover is reserved for a tier whose stage has no peer.
                    source_level = (_provider_conservation_level(
                        spill_guidance, source_specific, source_runway)
                        if replacement_provider is None else 0)
                    active_level = max(global_level, source_level)
                    if active_level > 0 and \
                            (source_specific or spill_guidance.get('quota_posture') == 'fast_preferred'):
                        provider_by_profile[target] = target_provider
                        level2_active = level2 or (
                            source_specific and active_level >= 2)
                        entries, spill_reason, _ = routing.spillover(
                            entries, provider_by_profile, target,
                            runway_view,
                            spill_guidance.get('runway_threshold_percent', 38),
                            spill_config['max_share_percent'],
                            level2_threshold_percent=(spill_guidance.get('level2_runway_percent')
                                                      if level2_active else None),
                            level2_max_share_percent=(_level2_spillover_cap(
                                c, q, spill_config, target_provider, source_runway)
                                if level2_active else None),
                            replace_provider=replacement_provider)
                        if source_level > global_level:
                            dynamic_reason += ':quota_source_level' + str(source_level)
                        dynamic_reason += ':' + spill_reason
                        if any(entry['profile'] == target for entry in entries):
                            candidates.append(({'profile': target, 'weight': 1}, target_reason))
            if batch is not None:
                chosen, credits = routing.advance(entries, batch.credits_for(tier))
                batch.propose(tier, credits)
            else:
                if preview is None:
                    preview = routing.Admissions.load().credits
                chosen, _ = routing.advance(entries, preview.get(tier, {}))
            why = next(reason for entry, reason in candidates if entry['profile'] == chosen)
            return chosen, 'routing_policy:' + tier + ':stage' + str(index) + ':' + why + ':' + dynamic_reason
    # The authorized one-level Ultra capacity fallback. It fires only when the
    # Ultra tier's own candidates were all refused with FRESH, re-validated
    # capacity-exhaustion proof -- never for a disabled or retired profile, an
    # auth or control-plane failure, an operator exclusion without bridge
    # provenance, or unknown telemetry. An explicit Ultra floor or pin, and an
    # operator who disabled the chain, keep the old hard wait.
    if tier == 'ultra' and ultra_rejections and ultra_capacity_fallback_enabled(t, c) and \
            all(is_capacity_exhaustion_reason(why, provider, t, q)
                for provider, why in ultra_rejections):
        profile, reason = _ultra_capacity_fallback(t, c, q, policy, batch)
        if profile:
            return profile, reason
    return None, 'routing_policy:' + (first_reason or 'no_enabled_candidate')


def _ultra_fallback_for_legacy(t, c, q, batch, why, provider):
    """Gate the one-level Ultra step-down for the legacy single-profile path.

    Same capacity proof and the same strict physical Deep landing as the policy
    path, so a saved legacy config without any routing_policy stays compatible.
    Returns ``(None, why)`` unchanged whenever the proof, the enablement or the
    landing capacity fails, preserving the historical wait/no-route behavior.
    """
    if not ultra_capacity_fallback_enabled(t, c) or \
            not is_capacity_exhaustion_reason(why, provider, t, q):
        return None, why
    return _ultra_capacity_fallback(t, c, q, routing.configured(c), batch)


def route(t, c, q, batch=None):
    """Profile selection for a task. Previews never mutate durable admission counters.

    An explicit requested_profile always pins the task and keeps the existing monthly
    retry behavior. With requested_profile=auto and a configured routing_policy for the
    task's tier, the first stage that has an enabled candidate passing provider
    admission wins and the candidate is picked by weighted round robin. Without a
    policy the legacy single-profile routing is unchanged.
    """
    requested = t.get('requested_profile', 'auto')
    if requested != 'auto':
        profile = _profile_name(t, c)
        if not profile or c['profiles'][profile].get('enabled') is False:
            return None, 'profile_disabled_or_missing'
        if not meets_capability_floor(profile, t, c):
            return None, 'capability_floor_unavailable'
        model = c['profiles'][profile]['model']
        if routing.is_retired_model(model):
            return None, 'retired_model'
        provider = model.split('/', 1)[0]
        if provider == KIMI_PROVIDER:
            # Explicit profile selection is an operator override for routing
            # choice, never for the weekly reserve (the reserved Ultra slice
            # belongs to k3.1-pro alone) and never a monthly-circuit bypass: the
            # circuit must be released explicitly before this profile can infer.
            kimi_ok, kimi_reason = kimi_request_allowed(model, c, q)
            if not kimi_ok:
                return None, kimi_reason
        ok, why = allowed(provider, q, t.get('complexity', 'normal'),
                          c.get('kimi_reserve_percent', 0),
                          retry_monthly=provider != KIMI_PROVIDER,
                          prepaid=provider in prepaid_providers(c))
        if ok and why == 'explicit_profile_monthly_retry':
            return profile, why
        if ok:
            return profile, 'explicit_profile:' + why
        # Never silently switch profiles or models; the coordinator re-selects deliberately.
        return None, why
    stages = (routing.configured(c) or {}).get(tier_of(t))
    if stages:
        return _policy_route(t, c, q, stages, batch)
    profile = _profile_name(t, c)
    if not profile or c['profiles'][profile].get('enabled') is False:
        return None, 'profile_disabled_or_missing'
    if not meets_capability_floor(profile, t, c):
        return None, 'capability_floor_unavailable'
    if profile in (t.get('excluded_profiles') or []):
        return None, 'routing_guard_excluded_profile'
    model = c['profiles'][profile]['model']
    if routing.is_retired_model(model):
        return None, 'retired_model'
    provider = model.split('/', 1)[0]
    excluded_here = provider in (t.get('excluded_providers') or [])
    if excluded_here:
        # A task-level continuation/provider exclusion is checked BEFORE any
        # fallback reasoning, mirroring the policy path's admission order inside
        # _allowed_profile. Only an exclusion the bridge itself recorded from a
        # capacity stop, with a still-matching credential identity, may carry
        # this task's one-level Ultra step-down. A manual or coordinator
        # exclusion never authorizes landing on Max, however empty the
        # excluded provider's telemetry currently reads; the historical
        # admission result below is preserved untouched for it.
        if ultra_capacity_fallback_enabled(t, c) and \
                is_capacity_exhaustion_reason('continuation_excluded_provider', provider, t, q):
            fb, fb_reason = _ultra_capacity_fallback(t, c, q, routing.configured(c), batch)
            if fb:
                return fb, fb_reason
    if provider == KIMI_PROVIDER:
        # Legacy single-profile routing has no policy stage, but the weekly
        # reserve still governs every automatic Kimi model (Ultra included via
        # its base provider gate).
        kimi_ok, kimi_reason = kimi_request_allowed(model, c, q)
        if not kimi_ok:
            if excluded_here:
                return None, kimi_reason
            return _ultra_fallback_for_legacy(t, c, q, batch, kimi_reason, provider)
    ok, why = allowed(provider, q, t.get('complexity', 'normal'),
                      c.get('kimi_reserve_percent', 0),
                      prepaid=provider in prepaid_providers(c))
    if ok:
        return profile, tier_of(t) + ':' + why
    return (None, why) if excluded_here else \
        _ultra_fallback_for_legacy(t, c, q, batch, why, provider)


def _unrecovered_provider(provider, kind, c=None):
    """Provider a billing failure still discredits in guidance until recovery is recorded.

    Historical evidence can predate this classification (or a circuit may have been
    pruned), so guidance stays conservative read-only: the failed shared provider is not
    offered as a candidate until a cleared circuit records a verified recovery path
    (fresh quota replenishment for a balance block, an explicit manual retry
    authorization, a configured scheduled monthly release, or a verified completed model
    success). A genuine inference-key identity change detaches the credential-bound block,
    so an old failure no longer discredits the new key. Auth rejection is credential-specific
    in every mode; prepaid mode additionally accepts current-key model success for a confirmed
    balance block. This never trips, clears or mutates the circuit.
    """
    if not provider or not kind:
        return None
    if billing_block(provider):
        return provider
    b = read_json(STATE / BILLING, {}).get(provider)
    if b and not b.get('cleared_at'):
        # billing_block returned None because the current credential differs from
        # the recorded one (or the record lacks a binding). A rotation detaches the
        # old block; guidance must not keep excluding the provider for that old key.
        ident = credential_identity(provider)
        if ident is not None and b.get('credential') is not None and b['credential'] != ident:
            return None
    if b and b.get('cleared_at'):
        released = b.get('released')
        if released == 'manual_retry':
            return None
        if kind == MONTHLY_REASON and released in (SCHEDULED_RESET_RELEASE, MODEL_SUCCESS_RELEASE):
            return None
        if kind != MONTHLY_REASON and released == 'quota':
            return None
        if kind == AUTH_REASON and released == MODEL_SUCCESS_RELEASE:
            return None
        if provider_is_prepaid(provider, c) and released == MODEL_SUCCESS_RELEASE:
            return None
    return provider


def _profile_order(t, c, policy):
    """Profiles in recovery preference order for the selected task tier.

    Without a policy this is exactly the configured profile order, preserving legacy
    guidance. With a policy, only the task's declared stages participate; recovery
    never changes capability tier merely to reach another profile.
    """
    names = list(c.get('profiles', {}))
    if not policy:
        return names
    ordered, seen = [], set()

    def add(name):
        if name in c['profiles'] and name not in seen:
            seen.add(name)
            ordered.append(name)

    tier = tier_of(t)
    for entry in routing.tier_entries(policy, tier):
        add(entry['profile'])
    if tier == 'ultra' and ultra_capacity_fallback_enabled(t, c):
        # Guidance must offer the same authorized one-level landing the
        # dispatcher can actually use: the Deep tier participates too, but only
        # profiles that pass the capability floor below are ever returned.
        for entry in routing.tier_entries(policy, 'deep'):
            add(entry['profile'])
    return ordered


def alternatives(t, c, q, exclude=None):
    """Currently dispatchable profiles for deliberate coordinator re-selection.

    Provider-level billing blocks apply to every profile on that provider, so sibling
    Kimi profiles (K2.8 and K3) can never be alternatives to each other while blocked.
    Ordering follows routing_policy preference when configured; the configured variant
    is carried so a continuation preserves maximum reasoning. For an automatic Ultra
    task the one-level Deep landing participates, restricted to the same strict
    physical Deep model the dispatcher may land on; nothing below Deep is ever offered.
    """
    out = []
    threshold = c.get('kimi_reserve_percent', 0)
    prepaid = prepaid_providers(c)
    policy = routing.configured(c) or {}
    ultra_names = {entry['profile'] for entry in routing.tier_entries(policy, 'ultra')}
    strict_landing = tier_of(t) == 'ultra' and ultra_capacity_fallback_enabled(t, c)
    for name in _profile_order(t, c, routing.configured(c)):
        p = c['profiles'][name]
        if p.get('enabled') is False or not p.get('model') or not meets_capability_floor(name, t, c):
            continue
        model = p['model']
        if strict_landing and name not in ultra_names and \
                routing.model_capability(model) != 'deep':
            # An unknown or weaker profile sitting in a Deep stage is not the
            # authorized landing model; keep guidance equal to dispatch.
            continue
        provider = str(model).split('/', 1)[0]
        if provider == exclude or routing.is_retired_model(model):
            continue
        if provider == KIMI_PROVIDER:
            kimi_ok, _kimi_reason = kimi_request_allowed(model, c, q)
            if not kimi_ok:
                continue
        ok, why = allowed(provider, q, t.get('complexity', 'normal'), threshold,
                          prepaid=provider in prepaid)
        if ok:
            out.append({'profile': name, 'model': p['model'], 'provider': provider,
                        'variant': p.get('variant'), 'route': why})
    return out


def _options(t, c, q, blocked_reason, partial_work=False, billing_reason=None, provider=None,
             exclude=None, window=False):
    """Machine-readable recovery that preserves the task tier and bridge routing."""
    if exclude is None:
        exclude = _unrecovered_provider(provider, billing_reason, c)
    alts = alternatives(t, c, q, exclude=exclude)
    task_tier = 'normal' if tier_of(t) == 'background' else tier_of(t)
    retry_command = 'delegate-opencode quota --retry-provider ' + provider if provider else None
    # Eligibility for the existing executable manual release, never a claim
    # that a named profile can infer immediately (Kimi requires the release).
    retry_allowed = bool(provider and not window and billing_reason == MONTHLY_REASON and
                         allowed(provider, q, t.get('complexity', 'normal'),
                                 c.get('kimi_reserve_percent', 0), retry_monthly=True,
                                 prepaid=provider in prepaid_providers(c))[0])
    release_required = bool(provider == KIMI_PROVIDER and retry_allowed)
    if alts:
        action = ('inspect_partial_work_and_resubmit_same_tier_auto' if partial_work else
                  'resubmit_same_tier_auto')
        autonomous = {
            'action': 'resubmit_same_tier_auto',
            'instruction': ('Continue autonomously: do not ask the user and do not wait for quota while a '
                            'viable alternative exists. Review partial work and submit a continuation with '
                            'the same tier, profile=auto and this failed task as parent. The bridge inherits '
                            'the failed provider exclusion and chooses the next configured stage. Never '
                            'replay prompt text or deployed side effects; carry forward only the remaining '
                            'authorized work.'),
            'tier': task_tier, 'profile': 'auto',
            'requires_user_approval': False, 'wait_for_quota': False,
            'retry_command': retry_command if release_required else None,
            'retry_is_proof': False,
            'manual_release_required': release_required,
            'profile_inference_available': not release_required}
    else:
        action = ('retry_or_reselect_after_usage_window' if window else
                  'top_up_provider_account_then_resubmit' if partial_work else
                  'top_up_provider_account_or_wait_for_quota')
        instruction = ('No viable route in this task tier is currently dispatchable. Report this specific '
                       'blockage; do not expect this task to resume automatically.')
        if task_tier == 'ultra':
            # Ultra is the reserved-capability tier. The user-authorized default
            # permits exactly one automatic step DOWN to the Deep primary, and
            # only on re-validated capacity-exhaustion proof. Reaching this
            # branch means neither an Ultra nor a Deep landing was dispatchable.
            if ultra_capacity_fallback_enabled(t, c):
                action = 'wait_for_ultra_or_deep_capacity'
                instruction = ('Ultra capacity is exhausted and the automatic one-level step-down found '
                               'no dispatchable Deep primary (Max blocked, disabled, or unproven right '
                               'now). The SAME task keeps waiting; it never moves below Deep and never '
                               'blends into cheaper pools. When Ultra capacity recovers, the bridge '
                               'returns the task at a safe idle checkpoint with the same session and '
                               'exactly-once continuation. Resolve the reported account or quota '
                               'blocker, or use an explicit same-task retier to Deep with a handoff '
                               'checkpoint if the work no longer needs Ultra.')
            else:
                action = 'wait_for_ultra_capacity'
                instruction = ('Ultra is the reserved native Kimi K3.1 Pro tier and no automatic '
                               'fallback applies to this task: it carries an explicit Ultra capability '
                               'floor or the operator disabled the one-level Deep step-down. Wait for '
                               'the weekly reserve/account pool to recover and resolve the reported '
                               'account or billing blocker; if the work genuinely no longer needs '
                               'Ultra, the coordinator may request an explicit same-task retier to '
                               'Deep with a handoff checkpoint.')
        if billing_reason == MONTHLY_REASON and retry_allowed:
            # Waiting on window telemetry cannot prove a monthly cycle recovered.
            action = 'top_up_or_authorize_manual_retry'
            instruction = ('No viable alternative profile is currently dispatchable. The monthly plan is '
                           'exhausted for this billing cycle; every Kimi model shares the same provider '
                           'block, so none is an alternative while it is open. Monthly exhaustion is advisory: a task '
                           'may still be submitted with this provider as an explicit requested_profile to try, '
                           'and a verified successful reply clears the flag. Do not infer a monthly reset from '
                           'usage-window telemetry or reported reset timestamps; the configured schedule or a '
                           'local retry authorization' + (' (' + retry_command + ')' if retry_command else '') +
                           ' can release the block for new attempts.')
            if release_required:
                # A named Kimi profile is no longer a circuit bypass.
                action = 'release_manual_retry_then_resubmit'
                instruction = ('No viable alternative profile is currently dispatchable. The monthly Kimi '
                               'plan is exhausted for this billing cycle and every Kimi model shares the '
                               'same provider block. A named profile no longer bypasses it: first run the '
                               'explicit manual release' +
                               (' (' + retry_command + ')' if retry_command else '') +
                               ', then resubmit; a verified successful reply can also clear the block. '
                               'This is authorization to try again, not proof of recovery. A genuinely '
                               'empty short window, an unavailable account or a verified zero account '
                               'pool is never released and still blocks.')
        autonomous = {'action': action, 'instruction': instruction,
                      'requires_user_approval': False,
                      'wait_for_quota': False if window else not retry_allowed,
                      'retry_command': retry_command, 'retry_is_proof': False,
                      'manual_release_required': release_required,
                      'profile_inference_available': not release_required}
    out = {'blocked_reason': blocked_reason, 'billing_reason': billing_reason,
           # Eligibility to run the explicit manual release; not proof that a
           # named profile can infer now (Kimi requires the release first).
           'explicit_retry_allowed': retry_allowed,
           'manual_release_required': release_required,
           'blocked_provider': provider, 'suggested_action': action, 'alternatives': alts,
           'automatic_fallback': False, 'autonomous_reselection': bool(alts),
           'autonomous_next_action': autonomous,
           'note': ('automatic_fallback=false means no automatic transition occurred for this terminal task; '
                    'autonomous_reselection=' + ('true' if alts else 'false') + '. ' +
                    ('Continue the same tier with profile=auto without asking or waiting.' if alts else
                     'Report the blockage; no route in this tier is dispatchable right now.') +
                    ' Original prompts are never replayed; a continuation lets the bridge choose the '
                    'next stage while preserving prior evidence.')}
    if window:
        # Explicit short-window exhaustion: never a durable circuit, never a monthly
        # block, and never a guessed recovery time. It is coordinator guidance only.
        out['usage_window_exhausted'] = True
        out['note'] = ('automatic_fallback=false means no automatic transition occurred for this terminal task. '
                       'The provider reported an explicit usage-window exhaustion, which is not a durable '
                       'billing block and not a monthly plan block; no reset time is inferred and the '
                       'provider is not blocked permanently. ' +
                       ('Continue the same tier with profile=auto and the failed task as parent.' if alts else
                        'Report the blockage and retry later or with another provider.'))
    return out


def recovery(t, c, q):
    """Blockage guidance for a queued task; None when routing currently succeeds."""
    profile, why = route(t, c, q)
    if profile:
        return None
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, why, billing_reason=_active_reason(provider), provider=provider)


def billing_failure_recovery(t, c, q):
    """Guidance for a task that failed on an unequivocal provider billing error."""
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, 'provider_billing_error', partial_work=True,
                    billing_reason=_task_billing_kind(t), provider=provider)


def _task_window_exhausted(t):
    """True only for explicit short usage-window exhaustion recorded on the task."""
    if str(t.get('reason') or '') == 'provider_usage_window_limit':
        return True
    return any(isinstance(e, dict) and e.get('source') == 'model' and e.get('usage_window')
               for e in t.get('errors') or [])


def window_failure_recovery(t, c, q):
    """Guidance for a task stopped by an explicit usage-window exhaustion.

    The failed provider is excluded from the offered candidates for this guidance only
    (a sibling profile on the same provider shares the window), no circuit is tripped
    and no reset time is inferred, so the provider is never blocked permanently.
    """
    provider, _ = _task_provider(t, c)
    return _options(t, c, q, 'provider_usage_window_limit', partial_work=True,
                    billing_reason=None, provider=provider, exclude=provider, window=True)


def routing_status(c, q):
    """Read-only effective dynamic admission shares per same-capability stage.

    Safe console metadata only: base/effective weights and shares plus the
    reason; never quota secrets, credential material or raw endpoint data. The
    stored policy is never read as telemetry and never rewritten here.
    """
    policy = routing.configured(c)
    if not policy:
        return None
    spill_config = routing.configured_spillover(c, policy)
    spill_guidance = tier_guidance(c, q) if spill_config and spill_config['enabled'] else None
    out = {}
    for tier in routing.TIERS:
        stages = policy.get(tier)
        if not stages:
            continue
        stage_views = []
        for index, stage in enumerate(stages):
            if tier == 'deep' and index == 0:
                spill_guidance = spill_guidance or tier_guidance(c, q)
                stage = _conserved_deep_stage(c, q, stage, spill_guidance)
            candidates = []
            denied = {}
            for entry in stage:
                profile = c.get('profiles', {}).get(entry['profile'])
                if not isinstance(profile, dict) or profile.get('enabled') is False:
                    continue
                provider = str(profile.get('model', '')).split('/', 1)[0]
                complexity = 'deep' if tier in ('deep', 'ultra') else 'normal'
                _, ok, admission_reason = _allowed_profile(
                    entry['profile'], {'tier': tier, 'complexity': complexity, 'requested_profile': 'auto'}, c, q)
                if ok:
                    candidates.append(entry)
                elif admission_reason == 'provider_reserved_for_fast':
                    denied[entry['profile']] = {'provider': provider, 'share': 0.0,
                        'effective_weight': 0, 'reason': admission_reason}
            level2 = False
            prepaid_ids = set((spill_guidance or {}).get('prepaid_providers') or ())
            prepaid_available = any(
                str((((c.get('profiles') or {}).get(entry['profile'])) or {}).get('model', '')).split('/', 1)[0]
                in prepaid_ids for entry in candidates)
            if spill_config and spill_config['enabled'] and index == 0 and \
                    _level2_conservation_active(tier, spill_config, spill_guidance,
                                                prepaid_available=prepaid_available):
                fast_candidates, _, fast_stage_index = _first_available_policy_stage(
                    'fast', {'complexity': 'normal'}, c, q, policy)
                if fast_candidates:
                    candidates = [entry for entry, _ in fast_candidates]
                    level2 = True
            provider_by_profile = {}
            for entry in candidates:
                profile = c['profiles'].get(entry['profile']) or {}
                provider_by_profile[entry['profile']] = str(profile.get('model', '')).split('/', 1)[0]
            adaptive = routing.dynamic_stage(
                c, 'fast' if level2 else tier,
                fast_stage_index if level2 else index, policy)
            if adaptive and spill_guidance is None:
                spill_guidance = tier_guidance(c, q)
            effective_tier = 'fast' if level2 else tier
            effective, reason, info = routing.dynamics(
                candidates, provider_by_profile, q, adaptive=adaptive,
                runway_override=_dynamic_runway_override(
                    effective_tier, provider_by_profile, q, spill_guidance))
            if level2:
                reason = 'quota_conservation_level2:' + reason
            source_providers = set(provider_by_profile.values())
            source_specific, runway_view, replacement_provider, source_runway = \
                _spillover_source(spill_guidance, source_providers, tier, level2)
            global_level = int((spill_guidance or {}).get('conservation_level') or 0)
            source_level = (_provider_conservation_level(
                spill_guidance, source_specific, source_runway)
                if replacement_provider is None else 0)
            active_level = max(global_level, source_level)
            if spill_config and index == 0 and tier in spill_config['tiers'] and \
                    active_level > 0 and \
                    (source_specific or spill_guidance.get('quota_posture') == 'fast_preferred'):
                target = spill_config['profile']
                target_profile = c.get('profiles', {}).get(target) or {}
                target_provider = str(target_profile.get('model', '')).split('/', 1)[0]
                complexity = 'deep' if tier == 'deep' else 'normal'
                target_ok, _ = allowed(target_provider, q, complexity,
                                       c.get('kimi_reserve_percent', 0),
                                       prepaid=target_provider in prepaid_providers(c))
                if target_ok and target_provider not in set(provider_by_profile.values()):
                    provider_by_profile[target] = target_provider
                    level2_active = level2 or (
                        source_specific and active_level >= 2)
                    effective, spill_reason, spill_info = routing.spillover(
                        effective, provider_by_profile, target,
                        runway_view,
                        spill_guidance.get('runway_threshold_percent', 38),
                        spill_config['max_share_percent'],
                        level2_threshold_percent=(spill_guidance.get('level2_runway_percent')
                                                  if level2_active else None),
                        level2_max_share_percent=(_level2_spillover_cap(
                            c, q, spill_config, target_provider, source_runway)
                            if level2_active else None),
                        replace_provider=replacement_provider)
                    if spill_info:
                        if source_level > global_level:
                            reason += ':quota_source_level' + str(source_level)
                        reason += ':' + spill_reason
                        info = spill_info
            info.update(denied)
            if info:
                stage_views.append({'reason': reason, 'members': info})
        if stage_views:
            out[tier] = stage_views
    return out or None


def _control_policy(c):
    import hashlib
    import json
    fields = ('profiles', 'routing_policy', 'routing', 'routing_dynamics', 'budget_signals', 'quota_spillover', 'fast_bias_runway_percent', 'bailian_plan_edition', 'bailian_pool')
    # Bump when the meaning of the persisted conservation level changes. Version 2
    # separates renewable short-window pressure from durable plan capacity. Prepaid
    # mode is part of budget_signals, so enabling it already changes the signature
    # and clears a level that was computed before the provider became prepaid.
    payload = {'controller_schema': 6, **{k: c.get(k) for k in fields}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def observe_load(c, q, now=None):
    """One writer at telemetry refresh; previews never move controller state."""
    import capacity
    now = time.time() if now is None else now
    raw = tier_guidance(c, q, _raw=True)
    previous = read_json(STATE / 'load-control.json', {})
    signature = _control_policy(c)
    if previous.get('policy') != signature:
        previous = {}
    observation = _plan_window_observation(c, q)
    replenished = _observed_replenishment(previous.get('quota_observation'), observation)
    state = capacity.transition(raw['conservation_level'], raw['capacity_pressure_percent'],
                                [raw['runway_threshold_percent'], raw['level2_runway_percent']],
                                previous, now, replenished)
    state['level_since'] = previous.get('level_since', now) \
        if previous.get('level') == state['level'] else now
    state['policy'] = signature
    state['quota_observation'] = observation
    state['replenished_at'] = now if replenished else previous.get('replenished_at')
    write_json(STATE / 'load-control.json', state)
    return state


def _plan_window_observation(c, q):
    """Return the persisted, non-secret counters used to prove a real refill."""
    providers = set()
    policy = routing.configured(c)
    spill = routing.configured_spillover(c, policy)
    spill_model = ((c.get('profiles') or {}).get((spill or {}).get('profile')) or {}).get('model')
    spill_provider = spill_model.split('/', 1)[0] if isinstance(spill_model, str) and '/' in spill_model else None
    prepaid = prepaid_providers(c)
    for tier in ('fast', 'background'):
        for stage in (policy or {}).get(tier) or []:
            for entry in stage:
                model = ((c.get('profiles') or {}).get(entry.get('profile')) or {}).get('model')
                if isinstance(model, str) and '/' in model:
                    provider = model.split('/', 1)[0]
                    if provider != spill_provider and provider not in prepaid:
                        providers.add(provider)
    result = {}
    for provider in providers:
        record = q.get(provider) or {}
        if record.get('stale', True) or record.get('state') != 'ok':
            continue
        windows = record.get('windows') or []
        durable = [w for w in windows if isinstance(w, dict) and
                   w.get('valid', True) is True and
                   (number(w.get('duration_minutes')) or 0) > 1440]
        for index, window in enumerate(durable or windows):
            if not isinstance(window, dict) or window.get('valid', True) is not True:
                continue
            value = window.get('remaining_percent')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            name = str(window.get('name') or f'window-{index}')
            duration = window.get('duration_minutes')
            key = f'{provider}|{name}|{duration}'
            result[key] = {'remaining_percent': float(value), 'resets_at': window.get('resets_at')}
    return result


def _observed_replenishment(previous, current):
    """True only when an authoritative quota counter has actually increased."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    for key, value in current.items():
        old = previous.get(key)
        if not isinstance(old, dict) or not isinstance(value, dict):
            continue
        before, after = old.get('remaining_percent'), value.get('remaining_percent')
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (before, after)) and float(after) >= float(before) + 0.5:
            return True
    return False


def tier_guidance(c, q, _raw=False):
    """Quota-aware tie-breaker for work that is genuinely eligible for Fast or Normal.

    This never changes a submitted tier and must never downgrade work that needs Normal.
    It only tells the coordinator when the configured Fast plan deserves more of the
    overlap: particularly when the preferred Normal pool has lost a provider or all
    Normal alternatives to the Fast provider are also below the configured window floor.
    """
    threshold_base = c.get('fast_bias_runway_percent', 38)
    if isinstance(threshold_base, bool) or not isinstance(threshold_base, (int, float)):
        threshold_base = 38
    threshold_base = max(0.0, min(400.0, float(threshold_base)))
    now = time.time()
    policy = routing.configured(c)

    spill_config = routing.configured_spillover(c, policy)
    level2_base = (spill_config or {}).get('level2_runway_percent', 0)
    if isinstance(level2_base, bool) or not isinstance(level2_base, (int, float)):
        level2_base = 0
    level2_base = max(0.0, min(100.0, float(level2_base)))

    # Shared, provider-neutral burn forecasts supply effective refills. The UI
    # pool percentage and conservation pressure use each subscription's durable
    # weekly/monthly window. Short rolling windows remain authoritative for
    # immediate admission and per-stage balancing, but a burst inside a renewable
    # five-hour window must not make an otherwise full weekly plan look depleted.
    configured_provider_ids = set()
    for tier_name in ('fast', 'background'):
        for stage in (policy or {}).get(tier_name) or []:
            for entry in stage:
                model = ((c.get('profiles') or {}).get(entry.get('profile')) or {}).get('model')
                if isinstance(model, str) and '/' in model:
                    configured_provider_ids.add(model.split('/', 1)[0])
    pool_fit = None
    budget_settings = normalized_budget_signals(c.get('budget_signals'), c.get('profiles'))
    prepaid_provider_ids = {provider for provider, item in budget_settings.items()
                            if isinstance(item, dict) and item.get('mode') == 'prepaid'}
    spill_provider = str(((c.get('profiles') or {}).get((spill_config or {}).get('profile')) or {}).get('model', '')).split('/')[0]
    # Prepaid credit is not a use-it-or-lose-it renewable plan: it must never
    # create calendar conservation, weight attenuation, proactive downgrades or
    # spillover merely because another plan is low. Its positive capacity is
    # neutral/healthy for control, while allowed() still enforces real zero,
    # expiry and inference-credential gates.
    plan_provider_ids = {p for p in configured_provider_ids
                         if (q.get(p) or {}).get('windows') and p != spill_provider and
                         p not in prepaid_provider_ids}
    durable_fits = {}
    if plan_provider_ids:
        import economics
        candidate = economics.work_pool(c, q, now=now, include_payg_balance=False,
                                        exclude_providers=prepaid_provider_ids)
        if candidate.get('complete') and isinstance(candidate.get('remaining_percent'), (int, float)):
            pool_fit = candidate
        durable_fits = {provider: economics._window_fit(provider, q.get(provider) or {}, now)
                        for provider in plan_provider_ids}
    refill = (pool_fit.get('refills') or [None])[0] if pool_fit else None
    relief = 0.0
    if refill and isinstance(refill.get('hours_until'), (int, float)) and \
            isinstance(refill.get('projected_remaining_percent'), (int, float)):
        current = float(pool_fit['remaining_percent'])
        gain = max(0.0, float(refill['projected_remaining_percent']) - current)
        imminence = max(0.0, min(1.0, 1.0 - float(refill['hours_until']) / 24.0))
        relief = imminence * max(0.0, min(1.0, gain / 30.0))
    threshold_percent = threshold_base * (1.0 - 0.30 * relief)
    level2_threshold = level2_base * (1.0 - 0.30 * relief)
    threshold = threshold_percent / 100.0

    def configured_stages(tier):
        if policy and policy.get(tier):
            return policy[tier]
        name = (c.get('routing') or {}).get(tier)
        return [[{'profile': name, 'weight': 1}]] if isinstance(name, str) else []

    def provider_for(entry):
        profile = (c.get('profiles') or {}).get(entry.get('profile'))
        if not isinstance(profile, dict) or profile.get('enabled') is False:
            return None
        model = profile.get('model')
        return model.split('/', 1)[0] if isinstance(model, str) and '/' in model else None

    def stage_state(tier):
        for index, stage in enumerate(configured_stages(tier)):
            configured = []
            available = []
            for entry in stage:
                provider = provider_for(entry)
                if not provider:
                    continue
                if provider not in configured:
                    configured.append(provider)
                ok, _ = allowed(provider, q, 'normal', c.get('kimi_reserve_percent', 0),
                                prepaid=provider in prepaid_provider_ids)
                if ok and provider not in available:
                    available.append(provider)
            if available:
                return {'index': index, 'configured': configured, 'available': available}
        return {'index': None, 'configured': [], 'available': []}

    def remaining(provider):
        durable = durable_fits.get(provider)
        if durable and isinstance(durable.get('source_amount'), (int, float)):
            return float(durable['source_amount'])
        values = []
        for window in (q.get(provider) or {}).get('windows') or []:
            if not isinstance(window, dict):
                continue
            value = window.get('remaining_percent')
            if window.get('valid', True) and isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(value):
                values.append(float(value))
        return min(values) if values else None

    def signal(provider):
        setting = budget_settings.get(provider) or {}
        mode = setting.get('mode', 'telemetry')
        if mode == 'ignore':
            return {'mode': mode, 'value': None, 'low': None}
        if mode == 'prepaid':
            # Neutral for control: actual availability is admission's hard gate.
            # Keep the authentic reading available for display/diagnostics.
            record = q.get(provider) or {}
            windows = [item for item in record.get('windows') or []
                       if isinstance(item, dict) and item.get('valid') is not False and
                       isinstance(item.get('remaining_percent'), (int, float)) and
                       not isinstance(item.get('remaining_percent'), bool)]
            worst = min(windows, key=lambda item: item['remaining_percent']) if windows else None
            return {'mode': mode, 'value': 1.0, 'low': False,
                    'remaining_percent': worst.get('remaining_percent') if worst else None,
                    'resets_at': worst.get('resets_at') if worst else None}
        if mode == 'manual_window':
            window = {'valid': True, 'remaining_percent': setting['remaining_percent'],
                      'duration_minutes': setting['duration_minutes'], 'resets_at': setting['resets_at']}
            value = routing._window_runway(window, now)
            return {'mode': mode, 'value': value, 'low': value <= threshold if value is not None else None,
                    'remaining_percent': setting['remaining_percent'], 'resets_at': setting['resets_at']}
        if mode == 'monetary':
            amount = setting.get('manual_balance')
            source = 'manual'
            if amount is None:
                source = 'telemetry'
                matches = [item.get('remaining') for item in (q.get(provider) or {}).get('balances') or []
                           if isinstance(item, dict) and item.get('currency') == setting['currency'] and
                           isinstance(item.get('remaining'), (int, float)) and
                           not isinstance(item.get('remaining'), bool)]
                amount = sum(matches) if matches else None
            low = amount <= setting['low_balance'] if amount is not None else None
            return {'mode': mode, 'value': amount, 'low': low, 'source': source,
                    'currency': setting['currency'], 'low_balance': setting['low_balance']}
        value = routing._runway(q.get(provider), now)
        return {'mode': 'telemetry', 'value': value,
                'low': value <= threshold if value is not None else None}

    fast = stage_state('fast')
    normal = stage_state('background')
    fast_providers = set(fast['available'])
    normal_providers = set(normal['available'])
    overlap = fast_providers & normal_providers
    alternate = normal_providers - fast_providers
    providers = sorted(fast_providers | normal_providers)
    configured_providers = sorted(set(fast['configured']) | set(normal['configured']) | configured_provider_ids)
    readings = {provider: remaining(provider) for provider in configured_providers}
    signals = {provider: signal(provider) for provider in configured_providers}
    runways = {provider: item['value'] if item['mode'] in ('telemetry', 'manual_window') else None
               for provider, item in signals.items()}
    combined_remaining = pool_fit.get('remaining_percent') if pool_fit else None
    combined_complete = bool(pool_fit)
    # Control uses working-rate durable runway; idle-inclusive runway is UI-only.
    # Short-window availability must not zero or restore the weekly/monthly pool.
    pressure_signals = {
        provider: (fit.get('pressure_runway') if fit.get('status') == 'ok' and
                   not fit.get('stale') else None)
        for provider, fit in durable_fits.items()
    }
    for provider, fit in durable_fits.items():
        record = q.get(provider) or {}
        if record.get('state') == 'ok' and not record.get('stale', True) and not fit.get('stale'):
            # A measured zero in the selected plan window is authoritative even
            # when its forecast is marked unavailable rather than ok.
            if fit.get('source_amount') == 0:
                pressure_signals[provider] = 0.0
    plan_signals = pressure_signals
    reliable = bool(plan_signals) and all(v is not None for v in plan_signals.values())
    provider_runways = {
        provider: (1.0 if provider in prepaid_provider_ids
                   else pressure_signals.get(provider, runways.get(provider)))
        for provider in configured_providers
    }
    provider_levels = {
        provider: _provider_conservation_level(
            {'runway_threshold_percent': threshold_percent,
             'level2_runway_percent': level2_threshold}, True, runway)
        for provider, runway in provider_runways.items()
    }
    pressure_percent = max(plan_signals.values()) * 100 if reliable else None
    # Forecast refills are display-only. They must never release conservation
    # before the authoritative provider telemetry actually reports a top-up.
    refill_safe = False
    lost_normal = set(normal['configured']) - normal_providers
    fast_low = bool(overlap) and any(signals.get(provider, {}).get('low') is True for provider in overlap)
    alternates_healthy = any(signals.get(provider, {}).get('low') is False for provider in alternate)
    alternates_low = bool(alternate) and all(signals.get(provider, {}).get('low') is True
                                             for provider in alternate)
    combined_low = not refill_safe and pressure_percent is not None and pressure_percent <= threshold_percent
    fast_bias = threshold > 0 and (combined_low or (bool(overlap) and not alternates_healthy and
        (fast_low or alternates_low)))
    if fast_bias and lost_normal and fast_low:
        reason = 'normal_pool_provider_unavailable'
    elif fast_bias and alternates_low:
        reason = 'normal_alternative_windows_low'
    elif fast_bias:
        reason = 'shared_fast_plan_window_low'
    elif overlap and alternates_healthy:
        reason = 'healthier_normal_plan_available'
    else:
        reason = 'no_fast_bias'
    posture = ('fast_preferred' if fast_bias else
               'normal_flexible' if reason == 'healthier_normal_plan_available' else 'neutral')
    level2_low = False
    if spill_config and spill_config.get('enabled') and 'background' in spill_config.get('tiers', ()) and \
            combined_low and posture == 'fast_preferred' and level2_threshold > 0:
        if pressure_percent is not None:
            level2_low = pressure_percent <= level2_threshold
        else:
            values = list(runways.values())
            level2_low = bool(values) and all(isinstance(value, (int, float)) and
                                               not isinstance(value, bool) and math.isfinite(value)
                                               for value in values) and \
                max(float(value) for value in values) <= level2_threshold / 100.0
    conservation_level = (2 if level2_low else
                          1 if spill_config and spill_config.get('enabled') and
                          posture == 'fast_preferred' and combined_low else 0)
    raw_level = conservation_level
    if not _raw:
        control = read_json(STATE / 'load-control.json', {})
        if control.get('policy') == _control_policy(c) and reliable:
            held = int(control.get('level', raw_level))
            if held > raw_level:
                conservation_level = held
                posture, fast_bias = 'fast_preferred', True
    return {'prefer_fast_when_both_fit': fast_bias, 'quota_posture': posture, 'reason': reason,
            'conservation_level': conservation_level,
            'raw_conservation_level': raw_level,
            'capacity_pressure_percent': pressure_percent,
            'capacity_confidence': ('observed' if reliable and all(
                durable_fits.get(p, {}).get('fit_source') == 'observed_burn'
                for p in plan_provider_ids if (q.get(p) or {}).get('available') is True)
                else 'prior' if reliable else 'unknown'),
            'runway_threshold_percent': threshold_percent,
            'runway_threshold_base_percent': threshold_base,
            'level2_runway_percent': level2_threshold,
            'level2_runway_base_percent': level2_base,
            'threshold_refill_relief': relief,
            'next_refill': refill,
            'conservation_refill_safe': refill_safe,
            'fast_stage': fast,
            'normal_stage': normal, 'remaining_percent': readings, 'runway': runways,
            'provider_runway': provider_runways,
            'provider_conservation_level': provider_levels,
            'prepaid_providers': sorted(prepaid_provider_ids),
            'combined_remaining_percent': combined_remaining,
            'combined_remaining_complete': combined_complete,
            'budget_signals': signals,
            'rule': ('Quota is only a tie-breaker. fast_preferred leans Fast when both tiers fit; '
                     'normal_flexible permits Fast or Normal based on error/rework risk while a '
                     'Normal-only subscription is healthy; neutral means classify by task needs.')}


def compact_guidance(g):
    """Coordinator contract: classify the work; keep capacity routing internal."""
    return {
        'instruction': (
            'Choose Fast, Normal, Deep or Ultra from unresolved uncertainty and the expected '
            'benefit of faster model work. Normal is the default for ordinary executable work; '
            'choose Fast only when direction, constraints and acceptance are clear and faster '
            'model work is reasonably expected to materially advance a concrete near-term '
            'milestone — an actually ready downstream consumer currently blocked by this work '
            'or an explicitly urgent delivery/deadline; a routine serial dependency or resource '
            'ownership alone is not enough. Choose Deep for high uncertainty such as an '
            'ambiguous goal, an unknown root cause, architecture trade-offs or unusually '
            'complex logic. Reserve Ultra for the most extreme-complexity work where the cost '
            'of a wrong architecture or a missed constraint dominates, and treat it as a '
            'finite reserved resource: if an ordinary task makes no substantive progress in '
            'two consecutive rounds, check the checkpoint and escalate deliberately to Ultra '
            'rather than silently retrying. Resolve environment, permission or authentication '
            'blockers first; once the situation is clear, execute at Normal. Keep profile=auto. '
            'The bridge privately owns provider balancing, conservation, fallback, the Kimi '
            'weekly reserve and same-session continuation.'
        )
    }


def guidance(t, c, q):
    """Read-only recovery options for a queued blockage or failed task.

    Never mutates the billing circuit: historical compact billing evidence informs
    guidance only, and a monthly failure stays sticky without any live mutation. An
    explicit short usage-window exhaustion gets policy-ordered alternatives without a
    durable block. Queued tasks get live quota blockage options; other statuses are
    never marked blocked by unrelated quota state.
    """
    billing_kind = None
    reason = str(t.get('reason') or '')
    if reason.startswith('provider_billing_'):
        billing_kind = reason[len('provider_billing_'):] or GENERIC_REASON
    else:
        for e in t.get('errors') or []:
            if isinstance(e, dict) and e.get('source') == 'model':
                billing_kind = _compact_billing_kind(e)
                if billing_kind:
                    break
    if billing_kind and t.get('status') in ('failed', 'needs_attention'):
        return billing_failure_recovery(t, c, q)
    if _task_window_exhausted(t) and t.get('status') in ('failed', 'needs_attention'):
        return window_failure_recovery(t, c, q)
    if t.get('status') == 'queued':
        return recovery(t, c, q)
    return None
