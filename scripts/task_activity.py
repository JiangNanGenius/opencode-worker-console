"""Live task activity and token usage for the Worker Desk console.

Read-only building blocks for ``GET /console-api/task/ID`` and the task list.
In-flight tasks are sampled from the authenticated loopback OpenCode session
with a short timeout and a small TTL cache so concurrent console threads never
overlap backend fetches for the same task; finished/closed tasks are rebuilt
from retained artifacts and never touch the network.

Response contract::

    activity = {events: [{id, type, status, label, text, time}],
                sampled_at, source, stale, error, has_more}
    usage    = {input, output, reasoning, cache_read, cache_write,
                total, cost, source, complete}

Activity is a bounded, chronological (oldest to newest) window of the most
recent events. Only redacted assistant/user text and short tool summaries are
included; raw reasoning text and tool output dumps are never exposed. Stable
event IDs (native part/message IDs or deterministic content hashes) mean a
polling client can update in place instead of duplicating rows. ``has_more``
means older or additional events exist beyond the window.

Usage counting semantics (truthful, never increment-on-poll):

* Every assistant message is counted exactly once by its real native message
  ID, so re-polling or overlapping pages cannot inflate totals.
* ``total`` is the provider-supplied ``tokens.total`` summed across unique
  messages when supplied; otherwise it is the sum of the reported component
  values. A provider total is never added to the components it already covers,
  so reasoning, output and cache values are not double counted.
* ``input``, ``output``, ``reasoning`` and nested ``cache.read``/
  ``cache.write`` (or flat ``cache_read``/``cache_write``) are summed
  independently. Cache reads are per-request provider accounting; OpenCode's
  own session aggregate reports them the same way. Verified against native
  sessions: the session aggregate equals the per-component sum over unique
  assistant messages and each message's provider ``total`` equals
  ``input + output + reasoning + cache.read + cache.write``, so reasoning is
  disjoint from output and cache values are additive, never overlapping.
* Missing values are ``None`` (``null``) and never coerced to ``0``; a real
  reported ``0`` stays ``0``.
* ``complete`` is True only when every contract number is reported for a full
  assistant history. Partial evidence (for example ``result.json`` carries no
  cache/cost breakdown) stays usable but is explicitly incomplete.
"""
import hashlib
import json
import math
import re
import threading
import time

import common

LIVE_TTL = 3.0
LIST_LIVE_TTL = 30.0
LIVE_TIMEOUT = 2.0
MESSAGE_WINDOW = 40
MAX_EVENTS = 60
RESULT_PREFIX_BYTES = 4096
CACHE_LIMIT = 64
RETAINED_LIMIT = 16
BUSY_ERROR = 'live session refresh already in progress'

COMPONENTS = ('input', 'output', 'reasoning', 'cache_read', 'cache_write')
DISPLAY_LIMITS = {'tool': 180, 'assistant': 320, 'user': 240, 'error': 200,
                  'pending': 200, 'guidance': 240, 'status': 160}
# Raw strings are capped well above the display cap (and before redaction) so a
# credential that would be visible in the displayed prefix is always seen whole
# by the redactor.
RAW_LIMITS = {'tool': 600, 'assistant': 8000, 'user': 4000, 'error': 800,
              'pending': 800, 'guidance': 800, 'status': 400}
_RESULT_TOKENS = re.compile(r'"tokens"\s*:\s*\{[^{}]*\}')

_cache = {}
_cache_guard = threading.Lock()
_locks = {}
_locks_guard = threading.Lock()
_result_cache = {}
_messages_cache = {}


def _now():
    return time.time()


def _num(value):
    """Finite, non-negative number or None. Booleans are never numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _seconds(value):
    number = _num(value)
    if number is None or number <= 0:
        return None
    return number / 1000 if number >= 1e12 else number


def _trim(value, limit):
    return value[:limit] if isinstance(value, str) and len(value) > limit else value


def _stable_id(*parts):
    digest = hashlib.sha1('\x1f'.join('' if part is None else str(part) for part in parts).encode()).hexdigest()
    return 'evt_' + digest[:20]


def empty_usage(source='none'):
    return {'input': None, 'output': None, 'reasoning': None, 'cache_read': None,
            'cache_write': None, 'total': None, 'cost': None, 'source': source, 'complete': False}


def _usage(components, total, cost, source, complete):
    return {'input': components.get('input'), 'output': components.get('output'),
            'reasoning': components.get('reasoning'), 'cache_read': components.get('cache_read'),
            'cache_write': components.get('cache_write'), 'total': total, 'cost': cost,
            'source': source, 'complete': bool(complete)}


def _token_components(tokens):
    cache = tokens.get('cache') if isinstance(tokens.get('cache'), dict) else {}
    nested_read = _num(cache.get('read'))
    nested_write = _num(cache.get('write'))
    flat_read = _num(tokens.get('cache_read'))
    flat_write = _num(tokens.get('cache_write'))
    return {'input': _num(tokens.get('input')), 'output': _num(tokens.get('output')),
            'reasoning': _num(tokens.get('reasoning')),
            'cache_read': flat_read if flat_read is not None else nested_read,
            'cache_write': flat_write if flat_write is not None else nested_write}


def usage_from_tokens(tokens, source='result'):
    """Usage from a flat/nested token map (result snapshot or legacy task field)."""
    if not isinstance(tokens, dict) or not tokens:
        return empty_usage(source)
    components = _token_components(tokens)
    supplied_total = _num(tokens.get('total'))
    known = [value for value in components.values() if value is not None]
    total = supplied_total if supplied_total is not None else (sum(known) if known else None)
    complete = all(components[name] is not None for name in COMPONENTS) and total is not None
    return _usage(components, total, None, source, complete)


def usage_from_session(session):
    """Usage from the native session aggregate (no provider total; sum components)."""
    tokens = session.get('tokens') if isinstance(session, dict) and isinstance(session.get('tokens'), dict) else {}
    components = _token_components(tokens)
    supplied_total = _num(tokens.get('total'))
    known = [value for value in components.values() if value is not None]
    total = supplied_total if supplied_total is not None else (sum(known) if known else None)
    cost = _num(session.get('cost')) if isinstance(session, dict) else None
    complete = all(components[name] is not None for name in COMPONENTS) and total is not None and cost is not None
    return _usage(components, total, cost, 'live', complete)


def usage_from_messages(messages, complete_history=True, source='saved'):
    """Recompute usage from real assistant message IDs (deduplicated, never incremented)."""
    unique = {}
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            info = message.get('info') if isinstance(message, dict) else None
            if not isinstance(info, dict) or info.get('role') != 'assistant':
                continue
            message_id = info.get('id')
            key = message_id if isinstance(message_id, str) and message_id else 'position:%d' % index
            unique[key] = info
    if not unique and complete_history:
        # A complete, empty history really did use zero tokens.
        return _usage({name: 0 for name in COMPONENTS}, 0, 0.0, source, True)
    if not unique:
        return empty_usage(source)
    sums = {name: 0 for name in COMPONENTS}
    present = {name: False for name in COMPONENTS}
    missing = {name: False for name in COMPONENTS}
    total = 0
    total_seen = False
    cost = 0.0
    cost_seen = True
    for info in unique.values():
        tokens = info.get('tokens') if isinstance(info.get('tokens'), dict) else {}
        components = _token_components(tokens)
        for name in COMPONENTS:
            value = components[name]
            if value is None:
                missing[name] = True
            else:
                present[name] = True
                sums[name] += value
        supplied_total = _num(tokens.get('total'))
        if supplied_total is not None:
            total += supplied_total
            total_seen = True
        else:
            known = [value for value in components.values() if value is not None]
            if known:
                total += sum(known)
                total_seen = True
        message_cost = _num(info.get('cost'))
        if message_cost is None:
            cost_seen = False
        else:
            cost += message_cost
    values = {name: (sums[name] if present[name] else None) for name in COMPONENTS}
    total_value = total if total_seen else None
    cost_value = cost if cost_seen else None
    complete = (complete_history and total_value is not None and cost_value is not None and
                all(present[name] and not missing[name] for name in COMPONENTS))
    return _usage(values, total_value, cost_value, source, complete)


def _normalize_usage(value):
    source = value.get('source') if isinstance(value.get('source'), str) and value.get('source') else 'saved'
    return _usage({name: _num(value.get(name)) for name in COMPONENTS},
                  _num(value.get('total')), _num(value.get('cost')), source, value.get('complete') is True)


def _read_json(path):
    try:
        stat = path.stat()
    except OSError:
        return None, None
    try:
        return json.loads(path.read_text()), stat.st_mtime
    except (OSError, ValueError):
        return None, stat.st_mtime


def _load_messages(task_id):
    if not isinstance(task_id, str) or not task_id:
        return None, None
    path = common.STATE / 'artifacts' / task_id / 'messages.json'
    try:
        stat = path.stat()
    except OSError:
        return None, None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _messages_cache:
        return _messages_cache[key]
    try:
        messages = json.loads(path.read_text())
    except (OSError, ValueError):
        messages = None
    if not isinstance(messages, list):
        messages = None
    if len(_messages_cache) >= RETAINED_LIMIT:
        _messages_cache.clear()
    _messages_cache[key] = (messages, stat.st_mtime)
    return messages, stat.st_mtime


def _result_tokens(task):
    """Cheap partial usage from the retained result snapshot (bounded prefix read)."""
    task_id = task.get('id') if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return None
    path = common.STATE / 'artifacts' / task_id / 'result.json'
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _result_cache:
        return _result_cache[key]
    tokens = None
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as stream:
            prefix = stream.read(RESULT_PREFIX_BYTES)
        match = _RESULT_TOKENS.search(prefix)
        if match:
            value = json.loads('{' + match.group(0) + '}')
            tokens = value.get('tokens') if isinstance(value, dict) else None
    except (OSError, ValueError):
        tokens = None
    usage = usage_from_tokens(tokens, 'result') if isinstance(tokens, dict) and tokens else None
    if len(_result_cache) > 1024:
        _result_cache.clear()
    _result_cache[key] = usage
    return usage


def _retained_usage(task):
    stored = task.get('usage')
    if isinstance(stored, dict) and stored:
        return _normalize_usage(stored)
    messages, _ = _load_messages(task.get('id'))
    if messages is not None:
        return usage_from_messages(messages, complete_history=True, source='saved')
    usage = _result_tokens(task)
    if usage is not None:
        return usage
    legacy = task.get('tokens')
    if isinstance(legacy, dict) and legacy:
        return usage_from_tokens(legacy, 'task')
    return empty_usage()


def _event(event_id, kind, status, label, text, at):
    return {'id': event_id, 'type': kind, 'status': status, 'label': label, 'text': text, 'time': at}


def _part_time(times, part, state):
    part_time = part.get('time') if isinstance(part.get('time'), dict) else {}
    state_time = state.get('time') if isinstance(state.get('time'), dict) else {}
    for candidate in (state_time.get('end'), state_time.get('start'),
                      part_time.get('end'), part_time.get('start'),
                      times.get('completed'), times.get('created')):
        at = _seconds(candidate)
        if at is not None:
            return at
    return None


def _tool_text(tool, state):
    inputs = state.get('input') if isinstance(state.get('input'), dict) else {}

    def first(*keys):
        for key in keys:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ''

    if tool == 'bash':
        return first('command')
    if tool in ('read', 'edit', 'write', 'apply_patch'):
        return first('filePath', 'path')
    if tool in ('grep', 'glob'):
        return first('pattern')
    if tool == 'webfetch':
        return first('url')
    if tool == 'todowrite':
        todos = inputs.get('todos')
        return '%d todos' % len(todos) if isinstance(todos, list) else ''
    if tool == 'list':
        return first('path', 'filePath')
    return first('description', 'command', 'filePath', 'path', 'pattern', 'query', 'url')


def _message_events(messages):
    events = []
    if not isinstance(messages, list):
        return events
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        info = message.get('info') if isinstance(message.get('info'), dict) else {}
        parts = message.get('parts') if isinstance(message.get('parts'), list) else []
        role = info.get('role')
        message_id = info.get('id') if isinstance(info.get('id'), str) and info.get('id') else 'position:%d' % index
        times = info.get('time') if isinstance(info.get('time'), dict) else {}
        created = _seconds(times.get('created'))
        completed = _seconds(times.get('completed'))
        raw_error = info.get('error')
        if isinstance(raw_error, dict):
            data = raw_error.get('data') if isinstance(raw_error.get('data'), dict) else {}
            events.append(_event(_stable_id('model-error', message_id, raw_error.get('name')), 'error', 'error',
                                 raw_error.get('name') or 'error',
                                 _trim(data.get('message') or raw_error.get('message') or raw_error.get('name') or
                                       'Model error', RAW_LIMITS['error']),
                                 completed or created))
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind == 'tool':
                tool = part.get('tool') if isinstance(part.get('tool'), str) and part.get('tool') else 'tool'
                state = part.get('state') if isinstance(part.get('state'), dict) else {}
                status = state.get('status') if isinstance(state.get('status'), str) and state.get('status') else 'pending'
                text = _tool_text(tool, state)
                if status == 'error' and isinstance(state.get('error'), str) and state['error'].strip():
                    failure = state['error'][:RAW_LIMITS['tool']]
                    text = (text + ' — ' + failure) if text else failure
                event_id = part.get('id') if isinstance(part.get('id'), str) and part.get('id') else \
                    _stable_id('tool', message_id, part_index, tool, part.get('callID'))
                events.append(_event(event_id, 'tool', status, tool,
                                     _trim(text, RAW_LIMITS['tool']), _part_time(times, part, state)))
            elif kind == 'text':
                text = part.get('text')
                if not isinstance(text, str) or not text.strip():
                    continue
                if role == 'assistant':
                    events.append(_event(part.get('id') or _stable_id('text', message_id, part_index), 'assistant',
                                         'completed' if completed else 'running', 'assistant',
                                         _trim(text, RAW_LIMITS['assistant']), completed or created))
                elif role == 'user':
                    events.append(_event(part.get('id') or _stable_id('text', message_id, part_index), 'user', None,
                                         'user', _trim(text, RAW_LIMITS['user']), created))
    return events


def _pending_events(items, at, prefix='pending'):
    events = []
    if not isinstance(items, list):
        return events
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        label, text, item_id = 'pending', 'Waiting for input', None
        if isinstance(item.get('questions'), list) and item['questions']:
            question = item['questions'][0] if isinstance(item['questions'][0], dict) else {}
            label = 'question'
            text = question.get('question') or question.get('header') or text
        elif item.get('permission') or item.get('title'):
            label = 'permission'
            text = item.get('title') or item.get('permission')
        item_id = item.get('id') if isinstance(item.get('id'), str) and item.get('id') else index
        events.append(_event(_stable_id(prefix, item_id), 'pending', 'pending', label,
                             _trim(text, RAW_LIMITS['pending']), at))
    return events


def _result_events(result, task, at):
    events = []
    for index, item in enumerate(result.get('errors') if isinstance(result.get('errors'), list) else []):
        if not isinstance(item, dict):
            continue
        text = item.get('message') or item.get('code') or 'Error'
        events.append(_event(_stable_id('result-error', item.get('message_id'), item.get('code'), text, index),
                             'error', 'error', item.get('source') or 'error',
                             _trim(text, RAW_LIMITS['error']), _seconds(item.get('occurred_at')) or at))
    events.extend(_pending_events(result.get('pending'), at, prefix='result-pending'))
    report = result.get('worker_report')
    summary = report.get('summary') if isinstance(report, dict) else None
    if isinstance(summary, str) and summary.strip():
        events.append(_event(_stable_id('result-summary', task.get('id'), summary[:200]), 'assistant', 'completed',
                             'assistant', _trim(summary, RAW_LIMITS['assistant']), at))
    return events


def _task_events(task, message_ids=None):
    events = []
    task_id = task.get('id') if isinstance(task.get('id'), str) else 'task'
    if task.get('cancel_requested'):
        events.append(_event(_stable_id('state', task_id, 'cancelling'), 'status', 'cancelling', 'status',
                             'Cancellation requested', _seconds(task.get('updated_at'))))
    if task.get('status') == 'queued':
        reason = task.get('queue_reason') or task.get('reason')
        events.append(_event(_stable_id('state', task_id, 'queued'), 'status', 'queued', 'status',
                             _trim(reason, RAW_LIMITS['status']) if isinstance(reason, str) and reason
                             else 'Waiting for a worker slot', _seconds(task.get('created_at'))))
    seen = message_ids or set()
    for index, item in enumerate(task.get('guidance') or []):
        if not isinstance(item, dict):
            continue
        message_id = item.get('message_id')
        if isinstance(message_id, str) and message_id in seen:
            continue
        events.append(_event(_stable_id('guidance', task_id, item.get('request_id') or index), 'guidance',
                             item.get('status') if isinstance(item.get('status'), str) else None, 'guidance',
                             _trim(item.get('text') if isinstance(item.get('text'), str) else '',
                                   RAW_LIMITS['guidance']), _seconds(item.get('created_at'))))
    return events


def _display_text(kind, value, limit):
    if not isinstance(value, str):
        return ''
    if kind in ('assistant', 'user', 'guidance'):
        text = value.strip()
        while '\n\n\n' in text:
            text = text.replace('\n\n\n', '\n\n')
    else:
        text = ' '.join(value.split())
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rstrip() + '…'
    return text, truncated


def _finalize(events):
    try:
        redacted = common.redact(events)
    except Exception:
        # Redaction must fail closed: never return possibly unredacted text.
        redacted = []
    result = []
    for event in redacted if isinstance(redacted, list) else []:
        if not isinstance(event, dict):
            continue
        kind = event.get('type') if isinstance(event.get('type'), str) else 'status'
        text, truncated = _display_text(kind, event.get('text'), DISPLAY_LIMITS.get(kind, 200))
        item = {'id': event.get('id'), 'type': kind, 'status': event.get('status'),
                'label': event.get('label'), 'text': text, 'time': event.get('time')}
        if truncated:
            item['truncated'] = True
        result.append(item)
    return result


def _build_events(messages, task, extra_events=(), limit=MAX_EVENTS):
    task = task if isinstance(task, dict) else {}
    events = _message_events(messages)
    message_ids = set()
    for message in messages or []:
        info = message.get('info') if isinstance(message, dict) else None
        if isinstance(info, dict) and isinstance(info.get('id'), str):
            message_ids.add(info['id'])
    events.extend(_task_events(task, message_ids))
    events.extend(extra_events)
    unique = {}
    for event in events:
        unique[event['id']] = event
    ordered = sorted(unique.values(), key=lambda event: (event['time'] is None, event['time'] or 0))
    if not ordered:
        status = task.get('status') if isinstance(task.get('status'), str) else 'unknown'
        reason = task.get('reason') or task.get('queue_reason')
        ordered = [_event(_stable_id('state', task.get('id'), status), 'status', status, 'status',
                          _trim(reason, RAW_LIMITS['status']) if isinstance(reason, str) else '',
                          _seconds(task.get('updated_at')) or _now())]
    truncated = len(ordered) > limit
    if truncated:
        ordered = ordered[-limit:]
    return _finalize(ordered), truncated


def events_from_messages(messages, task=None, limit=MAX_EVENTS):
    """Bounded, redacted, chronological events from native message dictionaries."""
    return _build_events(messages, task, limit=limit)


def full_event_text(task, event_id):
    """Return one complete redacted assistant text part on explicit request."""
    if not isinstance(task, dict) or not isinstance(event_id, str) or not event_id:
        raise ValueError('Invalid activity event')
    messages = None
    if _live_candidate(task):
        try:
            messages = common.api('/session/' + task['session_id'] + '/message?limit=' + str(MESSAGE_WINDOW),
                                  _session_directory(task), timeout=max(5, LIVE_TIMEOUT))
            if isinstance(messages, tuple):
                messages = messages[0]
        except Exception:
            messages = None
    if not isinstance(messages, list):
        messages, _ = _load_messages(task.get('id'))
    for message_index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        info = message.get('info') if isinstance(message.get('info'), dict) else {}
        if info.get('role') != 'assistant':
            continue
        message_id = info.get('id') if isinstance(info.get('id'), str) and info.get('id') else \
            'position:%d' % message_index
        for part_index, part in enumerate(message.get('parts') or []):
            if not isinstance(part, dict) or part.get('type') != 'text' or not isinstance(part.get('text'), str):
                continue
            candidate = part.get('id') or _stable_id('text', message_id, part_index)
            if candidate == event_id:
                redacted = common.redact(part['text'])
                if not isinstance(redacted, str):
                    raise ValueError('Activity message unavailable')
                return redacted
    result, _ = _read_json(common.STATE / 'artifacts' / str(task.get('id')) / 'result.json')
    report = result.get('worker_report') if isinstance(result, dict) else None
    summary = report.get('summary') if isinstance(report, dict) else None
    candidate = _stable_id('result-summary', task.get('id'), summary[:200]) if isinstance(summary, str) else None
    if candidate == event_id:
        redacted = common.redact(summary)
        if isinstance(redacted, str):
            return redacted
    raise ValueError('Activity message unavailable')


def activity_from_messages(messages, task=None, source='saved', sampled_at=None, has_more=False,
                           stale=False, error=None, limit=MAX_EVENTS):
    events, truncated = _build_events(messages, task, limit=limit)
    return {'events': events, 'sampled_at': sampled_at, 'source': source,
            'stale': bool(stale), 'error': error, 'has_more': bool(has_more or truncated)}


def _retained_activity(task, stale=False, error=None):
    task_id = task.get('id')
    messages, messages_at = _load_messages(task_id)
    if messages is not None:
        pending, pending_at = _read_json(common.STATE / 'artifacts' / str(task_id) / 'pending.json')
        extra = _pending_events(pending, pending_at or messages_at) if isinstance(pending, list) else []
        events, truncated = _build_events(messages, task, extra_events=extra)
        return {'events': events, 'sampled_at': messages_at, 'source': 'saved',
                'stale': bool(stale), 'error': error, 'has_more': truncated}
    result, result_at = _read_json(common.STATE / 'artifacts' / str(task_id) / 'result.json')
    if isinstance(result, dict):
        events, truncated = _build_events(None, task, extra_events=_result_events(result, task, result_at))
        return {'events': events, 'sampled_at': result_at, 'source': 'result',
                'stale': bool(stale), 'error': error, 'has_more': truncated}
    events, truncated = _build_events(None, task)
    return {'events': events, 'sampled_at': _seconds(task.get('updated_at')) or _now(), 'source': 'task',
            'stale': bool(stale), 'error': error, 'has_more': truncated}


def _error_text(exc):
    message = ' '.join(str(exc).split()) or type(exc).__name__
    return common.redact(message)[:200]


def _live_candidate(task):
    return (isinstance(task.get('session_id'), str) and bool(task.get('session_id')) and
            not task.get('session_deleted') and task.get('status') in common.ACTIVE)


def _session_directory(task):
    """Native session directory: bind/move-session keeps it in session_directory."""
    for key in ('session_directory', 'directory'):
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _task_lock(task_id):
    with _locks_guard:
        if len(_locks) >= CACHE_LIMIT:
            for key in [key for key, lock in _locks.items() if key != task_id and not lock.locked()][:CACHE_LIMIT]:
                _locks.pop(key, None)
        lock = _locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _locks[task_id] = lock
        return lock


def _cached_live(task, fresh_only):
    entry = _cache.get(task.get('id'))
    if not isinstance(entry, dict) or entry.get('session_id') != task.get('session_id') or \
            entry.get('directory') != _session_directory(task):
        return None
    if fresh_only and _now() - entry.get('fetched_at', 0) >= LIVE_TTL:
        return None
    return entry


def _pair(task, usage, activity, stale, error):
    if usage is None:
        usage = _retained_usage(task)
    if activity is None:
        activity = _retained_activity(task, stale=bool(stale), error=error)
    if error and not activity.get('error'):
        activity = dict(activity, stale=True, error=error)
    return {'activity': activity, 'usage': usage}


def _live_pair(task):
    task_id = task.get('id')
    session_id = task.get('session_id')
    entry = _cached_live(task, fresh_only=True)
    if entry is not None:
        return _pair(task, entry.get('usage'), entry.get('activity'), False, None)
    lock = _task_lock(task_id)
    if not lock.acquire(blocking=False):
        entry = _cached_live(task, fresh_only=False)
        if entry is not None:
            return _pair(task, entry.get('usage'), entry.get('activity'), True, BUSY_ERROR)
        return _pair(task, None, None, True, BUSY_ERROR)
    try:
        entry = _cached_live(task, fresh_only=True)
        if entry is not None:
            return _pair(task, entry.get('usage'), entry.get('activity'), False, None)
        directory = _session_directory(task)
        fresh_usage, fresh_activity = None, None
        errors = []
        try:
            fresh_usage = usage_from_session(common.api('/session/' + session_id, directory, timeout=LIVE_TIMEOUT))
        except Exception as exc:
            errors.append('usage: ' + _error_text(exc))
        try:
            messages, cursor = common.api('/session/' + session_id + '/message?limit=' + str(MESSAGE_WINDOW),
                                          directory, timeout=LIVE_TIMEOUT, include_cursor=True)
            fresh_activity = activity_from_messages(messages, task, source='live', sampled_at=_now(),
                                                    has_more=bool(cursor))
        except Exception as exc:
            errors.append('activity: ' + _error_text(exc))
        if fresh_usage is not None and fresh_activity is not None:
            with _cache_guard:
                if len(_cache) >= CACHE_LIMIT and task_id not in _cache and _cache:
                    oldest = min(_cache.items(), key=lambda item: item[1].get('fetched_at', 0))[0]
                    _cache.pop(oldest, None)
                _cache[task_id] = {'session_id': session_id, 'directory': directory,
                                   'fetched_at': _now(), 'usage': fresh_usage, 'activity': fresh_activity}
            return _pair(task, fresh_usage, fresh_activity, False, None)
        # A partially failed refresh never replaces a complete cached sample.
        previous = _cached_live(task, fresh_only=False)
        merged_usage = fresh_usage if fresh_usage is not None else (previous or {}).get('usage')
        merged_activity = fresh_activity if fresh_activity is not None else (previous or {}).get('activity')
        return _pair(task, merged_usage, merged_activity, True, '; '.join(errors) or None)
    finally:
        lock.release()


def snapshot(task):
    """Activity and usage for one task; never raises and never writes task state."""
    if not isinstance(task, dict):
        return {'activity': activity_from_messages([], {}, source='task', sampled_at=_now()),
                'usage': empty_usage()}
    try:
        if _live_candidate(task):
            return _live_pair(task)
        return _pair(task, None, None, False, None)
    except Exception as exc:
        try:
            return _pair(task, None, None, True, _error_text(exc))
        except Exception:
            return {'activity': activity_from_messages([], {}, source='task', sampled_at=_now()),
                    'usage': empty_usage()}


def usage_for_task(task):
    """Cheap per-task usage for the task list: cached live, snapshots, no history scans."""
    try:
        if not isinstance(task, dict):
            return empty_usage()
        entry = _cached_live(task, fresh_only=False)
        live = entry.get('usage') if isinstance(entry, dict) and isinstance(entry.get('usage'), dict) else None
        age = _now() - entry.get('fetched_at', 0) if live is not None else None
        if live is not None and task.get('status') in common.ACTIVE and age <= LIST_LIVE_TTL:
            return dict(live)
        stored = task.get('usage')
        if isinstance(stored, dict) and stored:
            return _normalize_usage(stored)
        if live is not None and age <= LIST_LIVE_TTL:
            return dict(live)
        legacy = task.get('tokens')
        if isinstance(legacy, dict) and legacy:
            return usage_from_tokens(legacy, 'task')
        snapshot_usage = _result_tokens(task)
        if snapshot_usage is not None:
            return snapshot_usage
        if live is not None:
            return dict(live)
        return empty_usage()
    except Exception:
        return empty_usage()
