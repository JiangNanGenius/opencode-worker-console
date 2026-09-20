"""Model configuration and OpenCode session management backend.

Exposes a small JSON-ready surface for the local console. Every upstream call
uses common.api, which is pinned to the authenticated loopback OpenCode server,
so no caller-supplied URL is ever contacted. Provider options, environment
variables, credentials and other provider/agent internals are never returned.
Concurrency is capped per owning Codex conversation (owner_thread_id) via
max_parallel_per_owner (default 4); there is no global or provider-wide cap.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import re
import time

import common

_PROFILE_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*/\S+$')
_SESSION_ID = re.compile(r'^ses[A-Za-z0-9_-]{1,124}$')
_ACTIONS = ('rename', 'archive', 'restore', 'fork', 'abort', 'delete', 'bind')
_ROUTING_KEYS = ('fast', 'background', 'deep')
_ARCHIVED_TRUE = {'1', 'true', 'yes', 'on', 'archived'}
_ARCHIVED_FALSE = {'0', 'false', 'no', 'off', 'active', 'unarchived'}
SESSION_LIMIT = 500
MAX_PARALLEL_PER_OWNER_DEFAULT = 4


def _per_owner_limit(value):
    """Owner slot cap; legacy global fields on disk are never interpreted."""
    if isinstance(value, bool) or not isinstance(value, int):
        return MAX_PARALLEL_PER_OWNER_DEFAULT
    return value if 1 <= value <= 16 else MAX_PARALLEL_PER_OWNER_DEFAULT


def _text(value, default=''):
    return value if isinstance(value, str) else default


def _status_type(value):
    if isinstance(value, dict):
        kind = value.get('type')
        return kind if isinstance(kind, str) else 'idle'
    return value if isinstance(value, str) else 'idle'


def _task_index():
    index = {}
    for t in common.tasks():
        sid = t.get('session_id')
        if isinstance(sid, str) and sid:
            index[sid] = t.get('id')
    return index


def _clean_session(raw, statuses=None, task_index=None):
    raw = raw if isinstance(raw, dict) else {}
    statuses = statuses if isinstance(statuses, dict) else {}
    index = task_index if isinstance(task_index, dict) else {}
    sid = raw.get('id')
    status = {'type': _status_type(statuses.get(sid))}
    time_value = raw.get('time')
    return {
        'id': _text(sid),
        'title': _text(raw.get('title')),
        'directory': _text(raw.get('directory')),
        'parentID': raw.get('parentID') if isinstance(raw.get('parentID'), str) else None,
        'projectID': raw.get('projectID') if isinstance(raw.get('projectID'), str) else None,
        'time': time_value if isinstance(time_value, dict) else {},
        'status': status,
        'task_id': index.get(sid) if isinstance(sid, str) else None,
    }


def _model_variants(model):
    variants = model.get('variants')
    if isinstance(variants, dict):
        return [str(name) for name in variants.keys()]
    if isinstance(variants, list):
        names = []
        for item in variants:
            name = item.get('name') if isinstance(item, dict) else item
            if isinstance(name, str) and name:
                names.append(name)
        return names
    return []


def _models(value):
    if isinstance(value, dict):
        items = []
        for key, model in value.items():
            if isinstance(model, dict):
                item = dict(model)
                item.setdefault('id', key)
                items.append(item)
        return items
    if isinstance(value, list):
        return [m for m in value if isinstance(m, dict)]
    return []


def catalog():
    """Return provider/model metadata with the provider internals stripped out."""
    body = common.api('/provider')
    providers = body.get('all') if isinstance(body, dict) else body
    if not isinstance(providers, list):
        providers = []
    connected = body.get('connected') if isinstance(body, dict) else None
    if isinstance(connected, dict):
        connected_ids = {key for key, value in connected.items() if value}
    elif isinstance(connected, list):
        connected_ids = {item for item in connected if isinstance(item, str)}
    else:
        connected_ids = set()
    result = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        pid = provider.get('id')
        if not isinstance(pid, str) or not pid:
            continue
        models = []
        for model in _models(provider.get('models')):
            mid = model.get('id')
            if not isinstance(mid, str) or not mid:
                continue
            limit = model.get('limit') if isinstance(model.get('limit'), dict) else {}
            context = limit.get('context')
            if isinstance(context, bool) or not isinstance(context, (int, float)):
                context = None
            models.append({'id': mid, 'name': _text(model.get('name'), mid),
                           'limit': {'context': context}, 'variants': _model_variants(model)})
        result.append({'id': pid, 'name': _text(provider.get('name'), pid),
                       'connected': pid in connected_ids, 'models': models})
    return {'providers': result}


def settings():
    """Return only the editable pool configuration, never paths/URLs/secrets."""
    c = common.config()
    profiles = {}
    for name, profile in (c.get('profiles') or {}).items():
        if not isinstance(profile, dict):
            continue
        profiles[name] = {'model': profile.get('model'), 'label': profile.get('label'),
                          'variant': profile.get('variant'), 'enabled': profile.get('enabled', True)}
    out = {'profiles': profiles, 'max_parallel_per_owner': _per_owner_limit(c.get('max_parallel_per_owner')),
           'kimi_reserve_percent': c.get('kimi_reserve_percent'),
           'revision': c.get('revision', 0), 'auto_approve': c.get('auto_approve', True)}
    import cleanup
    import quota
    # Always export a complete, validated schedule: generic defaults when unset and a
    # fail-closed normalization when a legacy record is malformed. The personal day 19 is
    # never a default; it only appears when explicitly stored.
    out['kimi_monthly_reset'] = quota.normalize_monthly_schedule(c.get('kimi_monthly_reset'))
    import economics
    out['economics'] = economics.normalize(c.get('economics'))
    if isinstance(c.get('routing'), dict):
        out['routing'] = c['routing']
    # Expose the effective dispatch policy, not the raw record: a degraded reference is
    # dropped the same way dispatch drops it, so settings and runtime agree. A
    # structurally malformed record routes as if unset and is never echoed as active.
    import routing
    policy = routing.runtime_policy(c.get('routing_policy'), c.get('profiles'))
    if policy:
        out['routing_policy'] = policy
    out['cleanup'] = cleanup.policy()
    return out


def _int_setting(body, key, low, high):
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(key + ' must be an integer between ' + str(low) + ' and ' + str(high))
    return value


def _clean_profile(name, value):
    if not isinstance(name, str) or not _PROFILE_ID.match(name):
        raise ValueError('Invalid profile id')
    if not isinstance(value, dict):
        raise ValueError('Profile ' + name + ' must be an object')
    model = value.get('model')
    if not isinstance(model, str):
        raise ValueError('Profile ' + name + ' requires a model')
    model = model.strip()
    if '://' in model or not _MODEL.match(model):
        raise ValueError('Profile ' + name + ' model must be provider/model without URLs')
    label = value.get('label')
    if label is None:
        label = name
    if not isinstance(label, str):
        raise ValueError('Profile ' + name + ' label must be text')
    variant = value.get('variant')
    if variant is not None and not isinstance(variant, str):
        raise ValueError('Profile ' + name + ' variant must be text')
    enabled = value.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('Profile ' + name + ' enabled must be boolean')
    profile = {'model': model, 'label': label, 'enabled': enabled}
    if variant is not None:
        profile['variant'] = variant
    return profile


def _validate_settings(body):
    if not isinstance(body, dict):
        raise ValueError('Settings must be an object')
    for key in ('profiles', 'max_parallel_per_owner', 'kimi_reserve_percent'):
        if key not in body:
            raise ValueError('Missing required setting: ' + key)
    raw_profiles = body.get('profiles')
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError('profiles must be a non-empty object')
    if len(raw_profiles) > 32:
        raise ValueError('At most 32 profiles are supported')
    profiles = {name: _clean_profile(name, value) for name, value in raw_profiles.items()}
    result = {'profiles': profiles, 'max_parallel_per_owner': _int_setting(body, 'max_parallel_per_owner', 1, 16),
              'kimi_reserve_percent': _int_setting(body, 'kimi_reserve_percent', 0, 100)}
    if 'auto_approve' in body:
        if not isinstance(body['auto_approve'], bool):
            raise ValueError('auto_approve must be boolean')
        result['auto_approve'] = body['auto_approve']
    if 'kimi_monthly_reset' in body:
        import quota
        # Strict validation with zoneinfo: unknown zones, malformed times and out-of-range
        # days are rejected. Omitting the key preserves the stored schedule for old clients.
        result['kimi_monthly_reset'] = quota.validate_monthly_schedule(body['kimi_monthly_reset'])
    if 'economics' in body:
        import economics
        result['economics'] = economics.validate(body['economics'])
    routing = body.get('routing')
    if routing is not None:
        if not isinstance(routing, dict) or set(routing.keys()) != set(_ROUTING_KEYS):
            raise ValueError('routing must define fast, background and deep')
        for key in _ROUTING_KEYS:
            name = routing[key]
            if not isinstance(name, str) or name not in profiles or not profiles[name]['enabled']:
                raise ValueError('routing.' + key + ' must reference an enabled profile')
        result['routing'] = {key: routing[key] for key in _ROUTING_KEYS}
    # Optional opt-in ordered-fallback/weighted policy. An omitted key preserves the
    # stored policy for old clients; null or an empty object (how the console clears an
    # optional object field) disables it.
    if 'routing_policy' in body:
        value = body['routing_policy']
        if value is None or value == {}:
            result['routing_policy'] = None
        else:
            import routing as routing_policy
            result['routing_policy'] = routing_policy.validate_policy(value, profiles)
    # Legacy per-task iteration caps in the body are ignored, never validated
    # and never re-persisted; workers have no default step or time cap.
    # Legacy global/provider cap fields sent by old clients are ignored, never
    # validated as owner limits, and never re-persisted as active settings.
    if 'cleanup' in body:
        cp = body['cleanup']
        if not isinstance(cp, dict) or not isinstance(cp.get('enabled'), bool):
            raise ValueError('Invalid cleanup policy')
        cleaned = {'enabled': cp['enabled']}
        for key, low, high in [('min_free_gb', 1, 1000), ('target_free_gb', 1, 2000), ('min_age_days', 1, 3650), ('keep_recent', 1, 10000), ('interval_seconds', 60, 86400)]:
            cleaned[key] = _int_setting(cp, key, low, high)
        if cleaned['target_free_gb'] < cleaned['min_free_gb']:
            raise ValueError('Cleanup target must be at least the trigger threshold')
        result['cleanup'] = cleaned
    if 'routing' not in result:
        defaults = {'fast': 'fast-code', 'background': 'senior-code', 'deep': 'deep-research'}
        if not all(p in profiles and profiles[p]['enabled'] for p in defaults.values()):
            raise ValueError('routing is required for custom profiles')
        result['routing'] = defaults
    return result


def _pool_busy():
    busy = common.ACTIVE | {'queued'}
    return any(t.get('status') in busy for t in common.tasks())


def _all_statuses(raw=None):
    if raw is None:
        raw = common.api('/experimental/session?limit=500&archived=true')
    directories = sorted({s.get('directory') for s in raw if isinstance(s, dict) and s.get('directory')}) if isinstance(raw, list) else []
    result = common.api('/session/status')
    if not isinstance(result, dict):
        raise ValueError('Session status unavailable')
    with ThreadPoolExecutor(max_workers=4) as pool:
        for status in pool.map(lambda d: common.api('/session/status', directory=d), directories):
            if not isinstance(status, dict):
                raise ValueError('Project session status unavailable')
            result.update(status)
    return result


def _sessions_busy():
    try:
        statuses = _all_statuses()
    except common.HttpFailure:
        return True
    if isinstance(statuses, dict):
        return any(_status_type(value) != 'idle' for value in statuses.values())
    if isinstance(statuses, list):
        return any(_status_type(value) != 'idle' for value in statuses)
    return bool(statuses)


def sync_worker_permissions():
    """Apply the runtime policy to idle, pool-owned sessions, including older installs."""
    import worker
    auto = common.config().get('auto_approve', True)
    result = {'updated': [], 'skipped': [], 'errors': []}
    statuses = {}
    for t in common.tasks():
        sid = t.get('session_id')
        if not sid or t.get('session_deleted') or t.get('auto_approve') is auto:
            continue
        if t.get('status') in common.ACTIVE:
            result['skipped'].append(sid)
            continue
        directory = t.get('session_directory') or t.get('directory')
        try:
            if directory not in statuses:
                statuses[directory] = common.api('/session/status', directory)
            if not isinstance(statuses[directory], dict):
                raise ValueError('Session status unavailable')
            if _status_type(statuses[directory].get(sid, 'idle')) != 'idle':
                result['skipped'].append(sid)
                continue
            policy = worker.permissions(dict(t, directory=directory, auto_approve=auto))
            common.api('/session/' + sid, directory, 'PATCH', {'permission': policy})
            common.update(t['id'], auto_approve=auto)
            result['updated'].append(sid)
        except Exception as error:
            result['errors'].append({'session_id': sid, 'error': common.redact(str(error))})
    common.write_json(common.STATE / 'permission-sync.json', result)
    return result


def save_settings(body):
    """Validate and atomically persist the editable settings.

    Other configuration is preserved. A new revision and restart_required flag
    are written; the caller is responsible for restarting services.
    """
    candidate = _validate_settings(body)
    explicit_policy = 'routing_policy' in body
    policy_value = candidate.pop('routing_policy', None)
    with common.locked():
        existing = common.read_json(common.CONFIG)
        if not isinstance(existing, dict) or not existing:
            raise ValueError('Pool is not installed')
        if 'revision' in body and body['revision'] != existing.get('revision', 0):
            raise ValueError('Settings changed elsewhere; reload before saving')
        if _pool_busy():
            raise ValueError('Refusing to change settings while pool tasks are queued or active')
        if _sessions_busy():
            raise ValueError('Refusing to change settings while OpenCode sessions are busy')
        revision = existing.get('revision', 0)
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 0
        # Drop any legacy per-task iteration cap so an old max_steps cannot reappear.
        existing.pop('max_steps', None)
        if explicit_policy:
            # An omitted routing_policy preserves the stored opt-in policy for old
            # clients; null or an empty object disables it.
            if policy_value is None:
                existing.pop('routing_policy', None)
            else:
                existing['routing_policy'] = policy_value
        elif existing.get('routing_policy') not in (None, {}):
            # A preserved policy must still validate against the submitted profiles:
            # otherwise a settings save would silently retain a broken reference.
            import routing as routing_policy
            existing['routing_policy'] = routing_policy.validate_policy(
                existing['routing_policy'], candidate['profiles'])
        existing.update(candidate)
        existing['revision'] = revision + 1
        existing['restart_required'] = True
        common.write_json(common.CONFIG, existing)
    result = settings()
    result['restart_required'] = True
    return result


def _archived_filter(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _ARCHIVED_TRUE:
            return True
        if lowered in _ARCHIVED_FALSE:
            return False
    raise ValueError('Invalid archived filter')


def _is_archived(raw):
    time_value = raw.get('time') if isinstance(raw, dict) else None
    if not isinstance(time_value, dict):
        return False
    archived = time_value.get('archived')
    return isinstance(archived, (int, float)) and not isinstance(archived, bool) and archived > 0


def sessions(query=None):
    """List OpenCode sessions filtered and linked to pool tasks."""
    query = query if query is not None else {}
    if not isinstance(query, dict):
        raise ValueError('Session query must be an object')
    raw = common.api('/experimental/session?limit=' + str(SESSION_LIMIT) + '&archived=true')
    if isinstance(raw, dict):
        raw = raw.get('sessions') if isinstance(raw.get('sessions'), list) else raw.get('data')
    if not isinstance(raw, list):
        raw = []
    raw = [item for item in raw if isinstance(item, dict)]
    statuses = _all_statuses(raw)
    index = _task_index()
    search = query.get('search')
    if search is not None and not isinstance(search, str):
        raise ValueError('search must be text')
    needle = (search or '').strip().lower()
    directory = query.get('directory')
    if directory is not None and not isinstance(directory, str):
        raise ValueError('directory must be text')
    wanted_directory = os.path.normpath(directory) if directory else None
    wanted_archived = _archived_filter(query.get('archived')) if 'archived' in query else None
    result = []
    for item in raw:
        sid = item.get('id')
        item_directory = item.get('directory') if isinstance(item.get('directory'), str) else ''
        if wanted_archived is not None and _is_archived(item) != wanted_archived:
            continue
        if wanted_directory is not None:
            found = os.path.normpath(item_directory) if item_directory else ''
            if found != wanted_directory and not found.startswith(wanted_directory + os.sep):
                continue
        if needle:
            haystack = ' '.join([_text(item.get('title')), _text(sid), item_directory]).lower()
            if needle not in haystack:
                continue
        result.append(_clean_session(item, statuses, index))
    projects = sorted({session['directory'] for session in result if session['directory']})
    return {'sessions': result, 'truncated': len(raw) >= SESSION_LIMIT, 'projects': projects}


def _validate_session_id(sid):
    if not isinstance(sid, str) or not _SESSION_ID.match(sid):
        raise ValueError('Invalid session ID')
    return sid


def _active_pool_session(sid):
    for t in common.tasks():
        if t.get('session_id') == sid and t.get('status') in common.ACTIVE:
            return t
    return None


def _fetch_session(sid):
    raw = common.api('/session/' + sid)
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
        raise ValueError('Unknown session')
    return raw


def update_session(sid, body):
    """Rename/archive/restore/fork/abort a session through the native API."""
    sid = _validate_session_id(sid)
    if not isinstance(body, dict):
        raise ValueError('Session update must be an object')
    action = body.get('action')
    if action not in _ACTIONS:
        raise ValueError('Unsupported session action')
    raw = _fetch_session(sid)
    # The caller-supplied path is never trusted; the real session owns the directory.
    directory = raw.get('directory') if isinstance(raw.get('directory'), str) else None
    index = _task_index()
    if action in ('archive', 'fork', 'abort', 'delete', 'bind') and _active_pool_session(sid):
        raise ValueError('Session belongs to an active pool task; use pool cancellation')
    statuses = common.api('/session/status', directory=directory)
    busy = _status_type(statuses.get(sid) if isinstance(statuses, dict) else None) != 'idle'
    if action in ('archive', 'fork', 'delete', 'bind') and busy:
        raise ValueError('Session is busy')
    if action == 'bind':
        destination = _directory(body.get('directory'))
        try:
            common.api('/experimental/control-plane/move-session', method='POST', data={'sessionID': sid, 'destination': {'directory': destination}, 'moveChanges': False})
        except common.HttpFailure as error:
            if error.status == 400:
                raise ValueError('OpenCode rejected workspace migration: select a worktree in the same Git project; create a new session for a different project') from None
            raise
        moved = _fetch_session(sid)
        if os.path.realpath(moved.get('directory', '')) != os.path.realpath(destination):
            raise ValueError('OpenCode did not confirm the new workspace')
        if index.get(sid):
            common.update(index[sid], session_directory=destination)
        return _clean_session(moved, statuses, index)
    if action == 'delete':
        if body.get('confirm_session_id') != sid or body.get('confirm_title') != raw.get('title'):
            raise ValueError('Type the exact session title to confirm permanent deletion')
        # Native deletion can remove children; protect every descendant before deleting.
        pending, seen = [sid], set()
        all_statuses = _all_statuses()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if len(seen) > 500:
                raise ValueError('Too many child sessions; manage children first')
            if _active_pool_session(current) or _status_type(all_statuses.get(current)) != 'idle':
                raise ValueError('A session or child is active; stop it before deletion')
            children = common.api('/session/' + current + '/children', directory=directory)
            if not isinstance(children, list):
                raise ValueError('Cannot verify child sessions')
            pending.extend(_validate_session_id(child['id']) for child in children)
        result = common.api('/session/' + sid, directory, 'DELETE')
        if result is not True:
            raise ValueError('OpenCode did not confirm deletion')
        for item in common.tasks():
            if item.get('session_id') in seen:
                common.update(item['id'], session_deleted=True)
        return {'id': sid, 'deleted': True}
    if action == 'rename':
        title = body.get('title')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('rename requires a non-empty title')
        title = title.strip()
        response = common.api('/session/' + sid, directory, 'PATCH', {'title': title})
        task_id = index.get(sid)
        if task_id:
            # Only mirror the title after the upstream rename succeeded.
            common.update(task_id, title=title)
    elif action == 'archive':
        response = common.api('/session/' + sid, directory, 'PATCH',
                              {'time': {'archived': int(time.time() * 1000)}})
    elif action == 'restore':
        response = common.api('/session/' + sid, directory, 'PATCH', {'time': {'archived': 0}})
    elif action == 'fork':
        # An empty body forks the session without sending any prompt.
        response = common.api('/session/' + sid + '/fork', directory, 'POST', {})
    else:
        response = common.api('/session/' + sid + '/abort', directory, 'POST')
    if isinstance(response, dict) and isinstance(response.get('id'), str):
        return _clean_session(response, statuses, index)
    return _clean_session(_fetch_session(sid), statuses, index)


def create_session(body):
    """Create a native session in an existing absolute directory, without a prompt."""
    if not isinstance(body, dict):
        raise ValueError('Session creation must be an object')
    directory = body.get('directory')
    title = body.get('title')
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError('directory is required')
    directory = os.path.expanduser(directory.strip())
    if not os.path.isabs(directory):
        raise ValueError('directory must be an absolute path')
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise ValueError('directory must be an existing directory')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('title is required')
    response = common.api('/session', directory, 'POST', {'title': title.strip()})
    if not isinstance(response, dict) or not isinstance(response.get('id'), str):
        raise ValueError('Session creation failed')
    statuses = common.api('/session/status', directory=directory)
    return _clean_session(response, statuses, _task_index())


def _directory(value):
    if not isinstance(value, str) or not os.path.isabs(os.path.expanduser(value)):
        raise ValueError('Workspace must be an absolute directory')
    path = os.path.realpath(os.path.expanduser(value))
    if not os.path.isdir(path):
        raise ValueError('Workspace directory does not exist')
    return path


def workspaces():
    return common.read_json(common.STATE / 'workspaces.json', [])


def save_workspace(body):
    directory = _directory(body.get('directory'))
    name = body.get('name') or os.path.basename(directory)
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('Workspace name must contain 1 to 100 characters')
    with common.locked('workspaces'):
        items = [w for w in workspaces() if w['directory'] != directory]
        item = {'name': name.strip(), 'directory': directory}
        items.append(item)
        common.write_json(common.STATE / 'workspaces.json', items)
    return item
