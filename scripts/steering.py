"""Durable, non-replayed guidance for an in-flight delegated session."""
import time
import common


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


def settled(task, messages):
    pending = [x for x in task.get('guidance', []) if x['status'] != 'rejected']
    if not pending:
        return True
    ids = {m.get('info', {}).get('id') for m in messages}
    assistants = [m.get('info', {}) for m in messages if m.get('info', {}).get('role') == 'assistant']
    return all(x['message_id'] in ids for x in pending) and bool(assistants) and assistants[-1].get('parentID') == pending[-1]['message_id']
