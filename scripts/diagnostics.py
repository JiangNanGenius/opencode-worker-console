"""Compact, credential-redacted errors returned across the coordinator bridge."""
import re

import common
import quota

# Exact monthly-plan exhaustion is distinct from a replenishable balance failure.
MONTHLY_REASON = 'monthly_usage_limit'
BALANCE_REASON = 'insufficient_balance'
GENERIC_REASON = 'billing_error'
# A real short usage window (for example the Kimi 5-hour limit) that will refresh on
# its own. It is not a monthly plan block and must never open the durable circuit.
WINDOW_REASON = 'usage_window_limit'
# The monthly wording alone is not exhaustion: "permission to view monthly quota" or a
# stated monthly allowance must never classify. Both a monthly reference and explicit
# exhaustion/next-cycle wording are required.
EXHAUSTION_MARKERS = ('reached', 'exhaust', 'exceed', 'used up', 'will be refreshed',
                      'refreshed in the next cycle', 'purchase extra usage',
                      'upgrade your plan', 'no remaining', 'out of quota')
# Explicit duration wording identifies a real window instead of an indefinite plan
# block. A bare "rate limit exceeded, retry in 5 hours" is throttling, not a usage
# window, so 'usage', 'quota' or 'window' is also required below.
_WINDOW_DURATION = re.compile(
    r'\b(?:[1-9]\d?|one|two|three|four|five|six|seven|eight|nine|ten)[- ]?(?:hour|hours|h|day|days|week|weeks)\b'
    r'|\b(?:hourly|daily|weekly)\b')


def error(source, code, message, retryable=None, action='inspect', **details):
    return common.redact({'source': source, 'code': str(code), 'message': str(message)[:4000],
                          'retryable': retryable, 'suggested_action': action, **details})


def _monthly_limit(text):
    return 'monthly' in text and any(marker in text for marker in EXHAUSTION_MARKERS)


def billing_kind_message(status, message):
    """Reason kind for an unequivocal model billing failure, else None.

    Model-origin only: callers pass one model error's HTTP status and message.
    Auth (401) and rate-limit (429) responses are never billing; the monthly
    phrase only counts when it arrives in the model error itself, never in tool
    output or transport text.
    """
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in (401, 429):
        return None
    text = str(message or '').lower()
    if _monthly_limit(text):
        return MONTHLY_REASON
    if status == 402 or 'insufficient balance' in text or 'insufficient_balance' in text:
        return BALANCE_REASON
    return None


def usage_window_kind(status, message):
    """Explicit short usage-window exhaustion, else None.

    Distinct in every direction: a monthly plan block is handled by billing_kind_message
    and never reaches here; auth (401) and rate-limit (429) responses stay generic so
    throttling behavior is preserved; and a duration alone (for example "rate limit
    exceeded, retry in 5 hours") is throttling, not a usage window. Both a usage/quota
    or window reference and exhaustion wording are required.
    """
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in (401, 429):
        return None
    text = str(message or '').lower()
    if not text or _monthly_limit(text):
        return None
    if not ('usage' in text or 'quota' in text or 'window' in text):
        return None
    if _WINDOW_DURATION.search(text) and any(marker in text for marker in EXHAUSTION_MARKERS):
        return WINDOW_REASON
    return None


def window_kind(raw):
    """Classify a raw OpenCode model error payload for window exhaustion."""
    if not isinstance(raw, dict):
        return None
    data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
    return usage_window_kind(data.get('statusCode', raw.get('statusCode')),
                             data.get('message') or raw.get('message') or '')


def billing_kind(raw):
    """Classify a raw OpenCode model error payload; tool text never reaches here."""
    if not isinstance(raw, dict):
        return None
    data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
    return billing_kind_message(data.get('statusCode', raw.get('statusCode')),
                                data.get('message') or raw.get('message') or '')


def is_billing(raw):
    """True only for unequivocal provider billing failures on a model error payload."""
    return billing_kind(raw) is not None


def exception(exc, phase, dispatched=False):
    http = getattr(exc, 'status', None)
    return error('transport' if isinstance(exc, common.HttpFailure) else 'bridge',
                 'http_' + str(http) if http else type(exc).__name__, str(exc),
                 retryable=(http in (408, 429) or http >= 500) if http else None,
                 action='observe_existing_session' if dispatched else 'fix_and_resubmit',
                 phase=phase, http_status=http, prompt_may_have_been_accepted=dispatched)


def occurred_at(info):
    """Occurrence time (seconds) of a session message, for recovery watermarks."""
    t = info.get('time') if isinstance(info.get('time'), dict) else {}
    ts = t.get('completed') or t.get('created')
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000 if ts >= 1e12 else ts
    return None


_occurred = occurred_at  # Backward-compatible private alias.


def from_messages(messages):
    errors = []
    for message in messages:
        info = message.get('info', {})
        raw = info.get('error')
        if isinstance(raw, dict):
            data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
            kind = billing_kind(raw)
            window = None if kind else window_kind(raw)
            errors.append(error('model', raw.get('name', 'model_error'),
                                data.get('message') or raw.get('message') or raw.get('name', 'Model error'),
                                retryable=False if (kind or window) else data.get('isRetryable'),
                                action='inspect_partial_work_and_resubmit_same_tier_auto' if (kind or window)
                                else 'inspect_model_error',
                                http_status=data.get('statusCode', raw.get('statusCode')),
                                message_id=info.get('id'), provider=info.get('providerID'),
                                **({'billing': True, 'billing_reason': kind,
                                    'occurred_at': occurred_at(info)} if kind else
                                   {'usage_window': True, 'usage_window_reason': window,
                                    'occurred_at': occurred_at(info)} if window else {})))
        for part in message.get('parts', []):
            if part.get('type') != 'tool': continue
            state = part.get('state') or {}
            metadata = state.get('metadata') or {}
            exit_code = metadata.get('exit')
            failed = state.get('status') == 'error'
            if failed or isinstance(exit_code, int) and exit_code != 0:
                inp = state.get('input') or {}
                errors.append(error('tool', 'tool_failed' if failed else 'command_exit_nonzero',
                                    state.get('error') or state.get('output') or 'Command failed',
                                    action='review_and_guide', tool=part.get('tool'),
                                    command=inp.get('command'), exit_code=exit_code,
                                    message_id=info.get('id'), call_id=part.get('callID')))
    return errors[-20:]


def collect(task_id, full=False):
    t = common.task(task_id)
    result = common.read_json(common.artifact_dir(task_id) / 'result.json')
    if result and not full:
        keys = ('worker_report', 'actual_models', 'changes', 'review_flags', 'acceptance',
                'status', 'reason', 'errors', 'pending')
        result = {key: result[key] for key in keys if key in result}
    out = {'task': common.public_task(t), 'result': result}
    out['task'].pop('recovery', None)
    recovery = t.get('recovery')
    try:
        # Read-only live derivation replaces the persisted snapshot (None clears a stale
        # one); it never re-arms the billing circuit and never replays anything.
        recovery = quota.guidance(t, common.config(),
                                  quota.view(common.read_json(common.STATE / 'quota.json', {})))
    except Exception:
        pass  # Keep the persisted snapshot only when live derivation is unavailable.
    if recovery:
        out['recovery'] = recovery
        out['task']['recovery'] = recovery
    return common.redact(out)
