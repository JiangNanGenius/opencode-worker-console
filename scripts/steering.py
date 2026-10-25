"""Durable, non-replayed guidance for an in-flight delegated session."""
import time
import common

UNRESOLVED_TIER = ('pending', 'uncertain')


def send(task_id, text, request_id=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 24000:
        raise ValueError('Guidance must contain 1 to 24000 characters')
    if common.redact(text) != text:
        raise ValueError('Guidance contains a credential-like value')
    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 100):
        raise ValueError('Invalid request ID')
    with common.locked('control-' + task_id):
        t = common.task(task_id)
        items = t.get('guidance', [])
        if request_id:
            previous = next((x for x in items if x['request_id'] == request_id), None)
            if previous:
                if previous['text'] != text:
                    raise ValueError('Request ID was already used for different guidance')
                return previous  # Never replay a transport-ambiguous request.
        if t['status'] not in ('running', 'uncertain') or not t.get('session_id') or t.get('cancel_requested'):
            raise ValueError('Only an in-flight task can receive guidance')
        # Steer and semantic retiering serialize: an unresolved tier change owns
        # the next continuation boundary, so a new steer must never queue a
        # competing duplicate continuation.
        if (t.get('tier_change') or {}).get('status') in UNRESOLVED_TIER:
            raise ValueError('A tier change is already pending or unresolved; steer after it settles')
        status = common.api('/session/status', t['directory']).get(t['session_id'], {})
        if status.get('type') not in ('busy', 'retry'):
            raise ValueError('Session is no longer running; submit a follow-up task')
        message_id = common.message_id()
        item = {'request_id': request_id or message_id, 'message_id': message_id,
                'text': text, 'created_at': time.time(), 'status': 'pending'}
        items.append(item)
        common.update(task_id, guidance=items)
        cfg = common.config()['profiles'][t['profile']]
        provider, model = cfg['model'].split('/', 1)
        body = {'messageID': message_id, 'agent': t['profile'],
                'model': {'providerID': provider, 'modelID': model},
                'parts': [{'type':'text','text':'Coordinator guidance for the current task. Existing scope, permissions and final JSON report requirements still apply.\n\n'+text}]}
        if cfg.get('variant'): body['variant'] = cfg['variant']
        try:
            common.api('/session/'+t['session_id']+'/prompt_async', t['directory'], 'POST', body)
            item['status'] = 'accepted'
        except common.HttpFailure as error:
            item['status'] = 'rejected' if error.status and 400 <= error.status < 500 else 'uncertain'
            item['error'] = str(error)
        common.update(task_id, guidance=items)
        return item


def _observed_boundaries(task):
    """(observed_sort, requested_at, message_id) of continuations proven to have run.

    Only observed evidence counts: an applied tier boundary exists only after
    its assistant acknowledgement (never a bare HTTP acceptance), and the
    active dispatch segment's reply is itself proof the session ran that far —
    that is how a later confirmed capacity fallback or queued conservation
    switch (both rebind task['message_id'] at dispatch) is recognized.
    """
    out = []
    changes = [x for x in (task.get('tier_history') or []) if isinstance(x, dict)]
    current = task.get('tier_change')
    if isinstance(current, dict):
        changes.append(current)
    for x in changes:
        if x.get('status') == 'applied' and x.get('message_id') and x.get('created_at') is not None:
            out.append((float(x.get('applied_at') or x.get('created_at') or 0),
                        float(x.get('created_at') or 0), x['message_id']))
    if task.get('message_id') and task.get('dispatch_attempted_at') is not None:
        when = float(task['dispatch_attempted_at'])
        out.append((when, when, task['message_id']))
    return out


def _superseding_boundary(task, guidance):
    """Newest observed boundary requested after every guidance item, or None.

    Guidance requested after that boundary still owes its own reply, so
    delivery safety for newer guidance is preserved and a stale pre-guidance
    output is never accepted.
    """
    newest_guidance = max((float(x.get('created_at') or 0) for x in guidance), default=0)
    later = [b for b in _observed_boundaries(task) if b[1] >= newest_guidance]
    if not later:
        return None
    return max(later, key=lambda b: b[0])[2]


def settled(task, messages):
    pending = [x for x in task.get('guidance', []) if x['status'] != 'rejected']
    if not pending:
        return True
    ids = {m.get('info', {}).get('id') for m in messages}
    assistants = [m.get('info', {}) for m in messages if m.get('info', {}).get('role') == 'assistant']
    if not assistants or not all(x['message_id'] in ids for x in pending):
        return False
    last_parent = assistants[-1].get('parentID')
    if last_parent == pending[-1]['message_id']:
        return True
    # A tier-change continuation or a later confirmed fallback that was
    # observed as the latest assistant reply already ran past every earlier
    # guidance item in the same session.
    boundary = _superseding_boundary(task, pending)
    return bool(boundary) and last_parent == boundary
