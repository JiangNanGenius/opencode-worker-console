"""Read-only account telemetry and provider billing circuit; selection stays with the coordinator."""
import math
from datetime import datetime, timezone
import time
from concurrent.futures import ThreadPoolExecutor
from common import config, STATE, HttpFailure, auth_key, credential_identity, locked, read_json, redact, request, write_json

ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/user/balance',
    'kimi-for-coding': 'https://api.kimi.com/coding/v1/usages',
}


BILLING = 'billing.json'
MONTHLY_REASON = 'monthly_usage_limit'
BALANCE_REASON = 'insufficient_balance'
GENERIC_REASON = 'billing_error'
REASON_SEVERITY = {GENERIC_REASON: 1, BALANCE_REASON: 2, MONTHLY_REASON: 3}
MANUAL_RETRY_NOTE = ('Manual retry authorization: releases the block so new attempts may run until a further '
                     'billing error re-opens it. It is not proof the provider quota was restored. Seen message '
                     'IDs and recovery watermarks are preserved, and a retry that is still exhausted re-blocks.')
MANUAL_RETRY_WARNING = 'Not proven recovery; new attempts may still fail while the provider is exhausted.'


def trip(provider, message='', message_id=None, occurred_at=None, reason=None):
    """Open the provider billing circuit on an unequivocal model billing error.

    Durable and credential-aware: the block binds to the current credential. Each seen
    message ID is retained without eviction, and an occurrence watermark at recovery
    ignores errors predating it, so an old session error can never reopen a recovered
    circuit. The reason kind separates a replenishable balance failure from hidden
    monthly plan exhaustion, which fresh usage telemetry must never clear.
    """
    ident = credential_identity(provider)
    if reason not in REASON_SEVERITY:
        reason = GENERIC_REASON
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider, {})
        ids = b.get('message_ids', [])
        if message_id and message_id in ids:
            return  # Already recorded; never relatch from a historical error.
        if occurred_at and b.get('cleared_at') and occurred_at <= b['cleared_at']:
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


def clear(provider, sampled_since=None, identity=None, evidence='quota'):
    """Close the circuit on fresh verified evidence; keep seen message IDs and watermarks.

    evidence is 'quota' (fresh positive account query) or 'manual_retry' (explicit local
    authorization). Monthly plan exhaustion only clears with 'manual_retry': the plan's
    usage endpoint keeps reporting window allowance while the monthly cycle is exhausted,
    so quota telemetry is never accepted as evidence of monthly recovery, and reported
    reset timestamps are never parsed into a recovery inference. There is deliberately no
    automatic success-based clearing, because a session reply carries no verifiable
    request credential provenance.

    sampled_since/identity guard the race where an account query started before a
    concurrent trip: a stale in-flight sample must never clear a newer circuit.
    """
    with locked('billing'):
        blocks = read_json(STATE / BILLING, {})
        b = blocks.get(provider)
        if not b or b.get('cleared_at'):
            return False
        if b.get('reason') == MONTHLY_REASON and evidence != 'manual_retry':
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


def known_providers():
    names = set(ENDPOINTS)
    try:
        names.update(str(p.get('model', '')).split('/', 1)[0] for p in config().get('profiles', {}).values()
                     if isinstance(p, dict) and '/' in str(p.get('model', '')))
    except Exception:
        pass
    return names


def retry_provider(provider):
    """Explicit local release/recheck authorization; never inferred from telemetry.

    This is the only path that may release a monthly block for new attempts. It is
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


def window(name, detail, duration=None):
    limit, remaining = number(detail.get('limit')), number(detail.get('remaining'))
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
        minutes = duration * units[w['timeUnit']] if duration is not None and w.get('timeUnit') in units else None
        windows.append(window('window_' + str(i), row.get('detail') or {}, minutes))
    if isinstance(body.get('usage'), dict):
        windows.append(window('overall', body['usage']))
    # Do not guess the units of usages.*.used_ratio, or turn missing fields into 0%.
    valid = [w for w in windows if w['valid']]
    return {'windows': windows, 'available': False if any(w['remaining'] == 0 for w in valid) else
            (True if windows and len(valid) == len(windows) else None), 'balances': []}


def fetch_one(provider):
    now = time.time()
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


def refresh(force=False):
    with locked('quota'):
        old = read_json(STATE / 'quota.json', {})
        now = time.time()
        providers = [p for p in ENDPOINTS if any(v['model'].split('/', 1)[0] == p for v in config()['profiles'].values())]
        identities = {p: credential_identity(p) for p in providers}
        same = all(old.get(p, {}).get('_credential') == identities[p] for p in providers)
        recent = old and all(now - old.get(p, {}).get('checked_at', 0) < 60 for p in providers)
        if not force and same and recent:
            return view(old)
        result = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            for p, value in zip(providers, pool.map(fetch_one, providers)):
                last = old.get(p, {}) if identities[p] == old.get(p, {}).get('_credential') else {}
                if value['state'] not in ('ok', 'auth_error') and last.get('sampled_at'):
                    value = dict(last, **value)
                value.update(checked_at=now, _credential=identities[p])
                result[p] = value
                if value['state'] == 'ok' and value.get('available') is True:
                    # Fresh positive account query recovers; a query started before a
                    # concurrent trip must never clear that newer circuit.
                    clear(p, sampled_since=now, identity=identities[p])
        write_json(STATE / 'quota.json', result)
        return view(result)


def view(values):
    out = {}
    for p, v in values.items():
        v = {k: x for k, x in v.items() if not k.startswith('_')}
        v['stale'] = v.get('state') != 'ok' or time.time() - v.get('sampled_at', 0) > 900
        block = billing_block(p)
        if block:
            # Overlay the active circuit so a stale positive cache is never advertised
            # and an authentic but misleading window snapshot never reads as callable.
            # The credential hash stays private; only redacted, actionable fields show.
            reason = block.get('reason') or GENERIC_REASON
            telemetry_available = v.get('available')
            billing = {'opened_at': block.get('opened_at'), 'message': block.get('message'),
                       'reason': reason}
            v.update(available=False, state='billing_blocked', billing=billing,
                     billing_reason=reason)
            if reason == MONTHLY_REASON:
                v['monthly_plan_exhausted'] = True
                billing['telemetry_available'] = telemetry_available is True
                billing['warning'] = ('Monthly plan quota is exhausted for this billing cycle. Reported '
                                      'window remaining values are authentic account telemetry but do not '
                                      'mean the provider is callable until real evidence of recovery appears.')
        out[p] = v
    return out


def allowed(provider, q, complexity, threshold=0):
    v = q.get(provider, {})
    if billing_block(provider):
        # An observed billing failure overrides even a fresh cached positive sample.
        return False, 'provider_billing_blocked'
    if v.get('state') == 'auth_error':
        return False, 'credential_rejected'
    if v.get('state') == 'rate_limited':
        return False, 'quota_endpoint_rate_limited'
    fresh_enough = time.time() - v.get('sampled_at', 0) <= 900
    if fresh_enough and v.get('available') is False:
        return False, 'quota_exhausted_or_account_unavailable'
    if provider == 'kimi-for-coding' and fresh_enough:
        pcts = [w['remaining_percent'] for w in v.get('windows', []) if w.get('remaining_percent') is not None]
        if pcts and min(pcts) < threshold and complexity != 'deep':
            return False, 'reserve_kimi_for_complex_work'
    return True, 'quota_unknown' if v.get('stale', True) or v.get('available') is None else 'quota_available'


def _tier(t):
    return 'deep' if t.get('complexity') == 'deep' else 'fast' if t.get('urgency') == 'fast' else 'background'


def _profile_name(t, c):
    requested = t.get('requested_profile', 'auto')
    routing = c.get('routing', {'fast': 'fast-code', 'background': 'senior-code', 'deep': 'deep-research'})
    profile = requested if requested != 'auto' else routing.get(_tier(t))
    return profile if profile in c['profiles'] else None


def _task_provider(t, c):
    """Configured provider for the task's pinned or routed profile, with its name."""
    name = t.get('profile')
    if name not in c['profiles']:
        name = _profile_name(t, c)
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


def route(t, c, q):
    profile = _profile_name(t, c)
    if not profile or c['profiles'][profile].get('enabled') is False:
        return None, 'profile_disabled_or_missing'
    provider = c['profiles'][profile]['model'].split('/', 1)[0]
    ok, why = allowed(provider, q, t.get('complexity', 'normal'), c.get('kimi_reserve_percent', 0))
    if ok:
        requested = t.get('requested_profile', 'auto')
        return profile, ('explicit_profile' if requested != 'auto' else _tier(t)) + ':' + why
    # Never silently switch profiles or models; the coordinator re-selects deliberately.
    return None, why


def _unrecovered_provider(provider, kind):
    """Provider a billing failure still discredits in guidance until recovery is recorded.

    Historical evidence can predate this classification (or a circuit may have been
    pruned), so guidance stays conservative read-only: the failed shared provider is not
    offered as a candidate until a cleared circuit records a verified recovery path
    (fresh quota replenishment for a balance block or an explicit manual retry
    authorization). This never trips, clears or mutates the circuit.
    """
    if not provider or not kind or billing_block(provider):
        return None
    b = read_json(STATE / BILLING, {}).get(provider)
    if b and b.get('cleared_at'):
        if b.get('released') == 'manual_retry':
            return None
        if kind != MONTHLY_REASON and b.get('released') == 'quota':
            return None
    return provider


def alternatives(t, c, q, exclude=None):
    """Currently dispatchable profiles for deliberate coordinator re-selection.

    Provider-level billing blocks apply to every profile on that provider, so sibling
    Kimi profiles (K2.8 and K3) can never be alternatives to each other while blocked.
    The configured variant is carried so a continuation preserves maximum reasoning.
    """
    out = []
    threshold = c.get('kimi_reserve_percent', 0)
    for name, p in c['profiles'].items():
        if p.get('enabled') is False or not p.get('model'):
            continue
        provider = p['model'].split('/', 1)[0]
        if provider == exclude:
            continue
        ok, why = allowed(provider, q, t.get('complexity', 'normal'), threshold)
        if ok:
            out.append({'profile': name, 'model': p['model'], 'provider': provider,
                        'variant': p.get('variant'), 'route': why})
    return out


def _options(t, c, q, blocked_reason, partial_work=False, billing_reason=None, provider=None):
    """Machine-readable blockage options. Codex autonomously re-selects a viable
    alternative and continues; topping up is the fallback only when none exist.

    automatic_fallback=False always means the bridge itself never switches or replays;
    autonomous_reselection=True is the separate coordinator instruction to choose a
    candidate and continue without asking the user or waiting for quota.
    """
    alts = alternatives(t, c, q, exclude=_unrecovered_provider(provider, billing_reason))
    retry_command = 'delegate-opencode quota --retry-provider ' + provider if provider else None
    if alts:
        action = 'inspect_partial_work_and_reselect_profile' if partial_work else 'reselect_profile'
        preferred = alts[0]
        autonomous = {
            'action': 'reselect_profile_and_resubmit',
            'instruction': ('Continue autonomously: do not ask the user and do not wait for quota while a '
                            'viable alternative exists. Review the partial work and submit a continuation as '
                            'a new task with one of the candidate profiles (preferred: ' +
                            preferred['profile'] + '), preserving the configured maximum reasoning variant. '
                            'Never replay prompt text or deployed side effects; carry forward only the '
                            'remaining authorized work.'),
            'preferred_profile': preferred['profile'], 'preferred_model': preferred['model'],
            'preferred_variant': preferred.get('variant'), 'candidates': alts,
            'requires_user_approval': False, 'wait_for_quota': False}
    else:
        action = 'top_up_provider_account_then_resubmit' if partial_work else \
            'top_up_provider_account_or_wait_for_quota'
        instruction = ('No viable alternative profile is currently dispatchable. Report this specific '
                       'blockage; do not expect this task to resume automatically.')
        if billing_reason == MONTHLY_REASON:
            # Waiting on window telemetry cannot prove a monthly cycle recovered.
            action = 'top_up_or_authorize_manual_retry'
            instruction = ('No viable alternative profile is currently dispatchable. The monthly plan is '
                           'exhausted for this billing cycle; K2.8 and K3 share the same provider block, so '
                           'neither is an alternative while it is open. Report the blockage; do not infer a '
                           'monthly reset from usage-window telemetry. New attempts require explicit local '
                           'retry authorization' + (' (' + retry_command + ')' if retry_command else '') + '.')
        autonomous = {'action': action, 'instruction': instruction,
                      'requires_user_approval': False,
                      'wait_for_quota': billing_reason != MONTHLY_REASON,
                      'retry_command': retry_command, 'retry_is_proof': False}
    return {'blocked_reason': blocked_reason, 'billing_reason': billing_reason,
            'blocked_provider': provider, 'suggested_action': action, 'alternatives': alts,
            'automatic_fallback': False, 'autonomous_reselection': bool(alts),
            'autonomous_next_action': autonomous,
            'note': ('automatic_fallback=false means the bridge never switches or replays by itself; '
                     'autonomous_reselection=' + ('true' if alts else 'false') + '. ' +
                     ('Pick a candidate profile and continue without asking the user or waiting.' if alts else
                      'Report the blockage; no candidate is dispatchable right now.') +
                     ' Profiles are pinned and prompts are never replayed; submit a new task with an '
                     'explicit alternative profile to continue authorized work.')}


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


def guidance(t, c, q):
    """Read-only recovery options for a queued blockage or billing-failed task.

    Never mutates the billing circuit: historical compact billing evidence informs
    guidance only, and a monthly failure stays sticky without any live mutation. Queued
    tasks get live quota blockage options; other statuses are never marked blocked by
    unrelated quota state.
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
    if t.get('status') == 'queued':
        return recovery(t, c, q)
    return None
