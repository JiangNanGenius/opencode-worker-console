"""Compact, credential-redacted errors returned across the coordinator bridge."""
import common
import quota


def error(source, code, message, retryable=None, action='inspect', **details):
    return common.redact({'source': source, 'code': str(code), 'message': str(message)[:4000],
                          'retryable': retryable, 'suggested_action': action, **details})


def is_billing(raw):
    """True only for unequivocal provider billing failures on a model error payload.

    Tool output text and transport failures never qualify; the signal is the model
    error's HTTP 402 status or an explicit insufficient-balance message.
    """
    if not isinstance(raw, dict):
        return False
    data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
    if data.get('statusCode') == 402:
        return True
    message = str(data.get('message') or raw.get('message') or '').lower()
    return 'insufficient balance' in message or 'insufficient_balance' in message


def exception(exc, phase, dispatched=False):
    http = getattr(exc, 'status', None)
    return error('transport' if isinstance(exc, common.HttpFailure) else 'bridge',
                 'http_' + str(http) if http else type(exc).__name__, str(exc),
                 retryable=(http in (408, 429) or http >= 500) if http else None,
                 action='observe_existing_session' if dispatched else 'fix_and_resubmit',
                 phase=phase, http_status=http, prompt_may_have_been_accepted=dispatched)


def _occurred(info):
    """Occurrence time (seconds) of a session message, for recovery watermarks."""
    t = info.get('time') if isinstance(info.get('time'), dict) else {}
    ts = t.get('completed') or t.get('created')
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000 if ts >= 1e12 else ts
    return None


def from_messages(messages):
    errors = []
    for message in messages:
        info = message.get('info', {})
        raw = info.get('error')
        if isinstance(raw, dict):
            data = raw.get('data') if isinstance(raw.get('data'), dict) else {}
            billing = is_billing(raw)
            errors.append(error('model', raw.get('name', 'model_error'),
                                data.get('message') or raw.get('message') or raw.get('name', 'Model error'),
                                retryable=False if billing else data.get('isRetryable'),
                                action='inspect_partial_work_and_reselect_profile' if billing
                                else 'inspect_model_error',
                                http_status=data.get('statusCode'), message_id=info.get('id'),
                                **({'billing': True, 'occurred_at': _occurred(info)} if billing else {})))
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
