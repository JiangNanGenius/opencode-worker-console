"""Semantic same-task tier changes applied at safe native continuation boundaries.

A tier change keeps the task ID, native session, workspace, scopes, every prior
message and the cumulative usage. It never aborts the current native turn, tool,
build or deploy: the new routing runs as one explicit Done/Remaining/Decisions/
Evidence/Do-not-repeat handoff continuation that OpenCode queues after the
current turn (prompt_async), the same safe-boundary machinery the capacity
reroute already uses. Routing stays profile=auto; this is orthogonal to
budget-driven model fallback, and there is no automatic difficulty classifier.

Public state is deliberately compact: ``tier_change``/``tier_history`` expose
request metadata only (never the handoff, never a profile/model choice, never a
private file path). The normalized handoff and the routed target profile live in
the private ``_retier`` field for dispatch and deterministic replay; they stay
out of ordinary task views and diagnostic logs.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import stat as stat_module
import time
import common
import diagnostics
import quota

TIERS = ('fast', 'normal', 'deep', 'ultra')
HANDOFF_FIELDS = ('done', 'remaining', 'decisions', 'evidence', 'do_not_repeat')
MAX_REASON = 500
MAX_REQUEST_ID = 100
# Same durable text budget as coordinator guidance.
MAX_HANDOFF_CHARS = 24000
# One handoff file must also fit the ordinary console API body bound.
MAX_HANDOFF_BYTES = 65536
COMPACT_KEYS = ('request_id', 'from_tier', 'to_tier', 'status', 'reason',
                'created_at', 'applied_at', 'message_id')
UNRESOLVED = ('pending', 'uncertain')
_REQUEST_ID_SHAPE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$')


def _compact(change):
    return {key: change[key] for key in COMPACT_KEYS if change.get(key) is not None}


def _tier_sync(tier):
    """Keep the legacy urgency/complexity mirrors consistent with the tier."""
    return {'tier': tier, 'urgency': 'fast' if tier == 'fast' else 'background',
            'complexity': 'deep' if tier in ('deep', 'ultra') else 'normal'}


def normalize_handoff(value):
    """Normalize text-or-array fields to arrays of non-empty stripped strings.

    ``remaining`` must explain concrete remaining work. Every string passes the
    existing redaction boundary, so raw credentials are never accepted.
    """
    if not isinstance(value, dict):
        raise ValueError('Handoff must be one JSON object')
    unknown = [key for key in value if key not in HANDOFF_FIELDS]
    if unknown:
        # Never echo the caller's key names: an unexpected key could itself
        # carry a credential-like value.
        raise ValueError('Handoff contains unsupported fields')
    out = {}
    for field in HANDOFF_FIELDS:
        raw = value.get(field)
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            raise ValueError('Handoff ' + field + ' must be text or an array of text')
        out[field] = [x.strip() for x in raw if x.strip()]
    if not out['remaining']:
        raise ValueError('Handoff remaining must explain the concrete remaining work')
    if sum(len(x) for items in out.values() for x in items) > MAX_HANDOFF_CHARS:
        raise ValueError('Handoff must contain at most ' + str(MAX_HANDOFF_CHARS) + ' characters')
    for items in out.values():
        for item in items:
            if common.redact(item) != item:
                raise ValueError('Handoff contains a credential-like value')
    return out


def read_handoff_file(path):
    """Read one private local handoff JSON file without exposing its path or contents.

    Failures are deliberately generic: the file path, parse positions and file
    contents are the caller's private view and must never reach ordinary
    diagnostics or logs.
    """
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise ValueError('Handoff file must be an absolute private path')
    target = Path(path)
    try:
        if target.is_symlink() or not target.is_file():
            raise ValueError('handoff_unusable')
        # The approved boundary is an owner-only private file: a regular file
        # owned by the calling user, with no group or world access bits.
        # Anything else can be observed or swapped by another reader and is
        # refused. (The stat result must never shadow the stat module here.)
        info = target.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077 or \
                not stat_module.S_ISREG(info.st_mode):
            raise ValueError('handoff_unsafe')
        if info.st_size > MAX_HANDOFF_BYTES:
            raise ValueError('handoff_too_large')
        value = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Handoff file must be a readable owner-only (no group/world '
                         'access) regular JSON file within ' + str(MAX_HANDOFF_BYTES) +
                         ' bytes; no path or contents are shown') from None
    if not isinstance(value, dict):
        raise ValueError('Handoff file must contain one JSON object')
    return value


def _validated(tier, reason, request_id):
    if tier not in TIERS:
        raise ValueError('Tier must be fast, normal, deep or ultra')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= MAX_REASON:
        raise ValueError('Reason must be 1-' + str(MAX_REASON) + ' characters')
    reason = reason.strip()
    if common.redact(reason) != reason:
        raise ValueError('Reason contains a credential-like value')
    if not isinstance(request_id, str) or not _REQUEST_ID_SHAPE.match(request_id.strip()):
        raise ValueError('A stable request ID of 1-100 characters matching '
                         '[A-Za-z0-9][A-Za-z0-9._:-]* is required')
    request_id = request_id.strip()
    # Reject credential-looking request IDs before anything is persisted.
    if common.redact(request_id) != request_id:
        raise ValueError('Request ID contains a credential-like value')
    return reason, request_id


def _content_sha(tier, reason, handoff):
    return hashlib.sha256(json.dumps([tier, reason, handoff], sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _remember(t, request_id, content):
    """Keep one replay fingerprint per request ID the task history retains.

    Fingerprints live and die with their ``tier_history`` entries: pruning is
    driven by the retained history, never by an arbitrary count that would
    silently stop a still-auditable request from being replay-safe.
    """
    retained = {(t.get('tier_change') or {}).get('request_id')}
    retained.update(item.get('request_id') for item in t.get('tier_history') or [])
    retained.add(request_id)
    stored = {key: value for key, value in (t.get('_retier_hashes') or {}).items()
              if key in retained}
    stored[request_id] = content
    return stored


def _replay(t, request_id, content):
    """Return the stored result for a deterministic replay, never a new send."""
    hashes = t.get('_retier_hashes') or {}
    change = t.get('tier_change') or {}
    history = t.get('tier_history') or []
    known = change.get('request_id') == request_id or \
        any(item.get('request_id') == request_id for item in history)
    if not known:
        return None
    # A tracked mismatch (or a pruned hash) never replays ambiguously: the
    # stable request ID was already used for different contents.
    if hashes.get(request_id) != content:
        raise ValueError('Request ID was already used for a different retier request')
    if change.get('request_id') == request_id:
        return _compact(change)
    return _compact(next(item for item in history if item.get('request_id') == request_id))


def _route_for(t, tier, c, q):
    """Preview the routed profile for the prospective tier; previews consume nothing."""
    view = dict(t, requested_profile='auto', **_tier_sync(tier))
    profile, why = quota.route(view, c, q)
    return profile, why, view


def _handoff_text(from_tier, to_tier, reason, handoff, first_dispatch=False):
    def section(field, prompt):
        lines = handoff[field]
        body = '\n'.join('- ' + x for x in lines) if lines else '(none provided)'
        return field.replace('_', ' ').capitalize() + (' — ' + prompt if prompt else '') + ':\n' + body
    preamble = (
        'Coordinator tier change for the current task: ' + str(from_tier) + ' -> ' + str(to_tier) + '.\n'
        'Reason: ' + reason + '\n\n') if not first_dispatch else (
        'Pre-dispatch tier change for this task: ' + str(from_tier) + ' -> ' + str(to_tier) + '.\n'
        'Reason: ' + reason + '\n\n'
        'The tier change is already applied to this first dispatch. The checkpoint below\n'
        'is authoritative for what remains: follow Remaining, preserve the Decisions and\n'
        'never execute any Do-not-repeat item. The task specification above remains the\n'
        'governing objective, scope and authority.\n\n')
    continuation = ('' if first_dispatch else
        'This is a semantic retiering continuation inside the same task and the same\n'
        'OpenCode session. Your prior turn was not aborted; this runs at the next safe\n'
        'native continuation boundary. The task ID, native session, workspace, scopes,\n'
        'targets, permissions, full conversation and cumulative usage are unchanged; this\n'
        'is not a new task and does not expand authorization. Honor the original authority\n'
        'and scope on this and every later continuation. Do not replay whole completed\n'
        'work, re-run finished side effects or repeat published messages.\n\n')
    return preamble + continuation + (
        'Handoff checkpoint (verify against the live workspace; do not re-do the Done items):\n'
        + section('done', 'already completed') + '\n\n'
        + section('remaining', 'the concrete work to continue now') + '\n\n'
        + section('decisions', 'settled choices to preserve') + '\n\n'
        + section('evidence', 'known evidence so far') + '\n\n'
        + section('do_not_repeat', 'actions that must not run again') + '\n\n'
        'Existing scope, permissions and final JSON report requirements still apply.')


def dispatch_note(t):
    """Checkpoint text that the FIRST dispatch of a pre-dispatch retier must follow.

    The handoff stays private task state (``_retier``); only the dispatched
    prompt carries it. It is consumed with the dispatch intent, never replayed.
    """
    private = t.get('_retier') or {}
    change = t.get('tier_change') or {}
    if not private.get('queued_checkpoint') or not private.get('handoff') or \
            change.get('status') != 'applied':
        return ''
    return '\n\n' + _handoff_text(change.get('from_tier'), change.get('to_tier'),
                                  private.get('reason', ''), private['handoff'],
                                  first_dispatch=True)


def _apply_queued(t, tier, reason, handoff, request_id, content):
    """Apply a tier change to a queued task before any dispatch happens.

    Routing is previewed outside the lock (previews consume nothing), then the
    durable write revalidates under the same state serialization the pool uses
    for admission, so a queued->starting race can never clear the profile of an
    already-starting task. Caller holds the per-task control lock; the nested
    order here is control-then-state, identical to common.update and worker
    finish, so it cannot deadlock against pool admission.
    """
    c = common.config()
    q = quota.view(common.read_json(common.STATE / 'quota.json', {}))
    profile, route_reason, _view = _route_for(t, tier, c, q)
    if not profile:
        raise ValueError('No route is available for tier ' + tier + ': ' + str(route_reason))
    now = time.time()
    applied = _compact({'request_id': request_id, 'from_tier': t.get('tier'),
                        'to_tier': tier, 'status': 'applied', 'reason': reason,
                        'created_at': now, 'applied_at': now})
    with common.locked():
        current = common.task(t['id'])
        if current.get('status') != 'queued' or current.get('cancel_requested') or \
                (current.get('tier_change') or {}).get('status') in UNRESOLVED or \
                current.get('tier') == tier:
            raise ValueError('Task began dispatching; submit the retier request once it is running')
        fields = dict(_tier_sync(tier))
        fields.update(tier_change=applied,
                      tier_history=(current.get('tier_history') or []) + [applied],
                      requested_tier=None, queue_reason=None, recovery=None,
                      _retier={'request_id': request_id, 'queued_checkpoint': True,
                               'handoff': handoff, 'reason': reason},
                      _retier_hashes=_remember(current, request_id, content))
        if current.get('profile'):
            # A stale profile selection was computed for the old tier: dispatch
            # recomputes it fresh under the new tier.
            fields.update(profile=None, route_reason=None)
        if tier == 'ultra' and current.get('session_id'):
            # Entering Ultra must not continue the previous model's session: the
            # reserved model gets a clean session built by first dispatch, and the
            # old session stays as retained history. The queued checkpoint still
            # carries the Done/Remaining/Do-not-repeat handoff.
            fields.update(session_id=None, message_id=None, dispatch_attempted_at=None,
                          dispatch_acknowledged=None,
                          _retier={**fields['_retier'],
                                   'superseded_session_id': current['session_id']})
        current.update(fields, updated_at=time.time())
        common.write_json(common.task_path(current['id']), current)
    return applied


def _reject(t, change, error):
    """A definitively rejected prompt leaves the original tier/dispatch intact.

    The rejection is retained in ``tier_history`` as well as in ``tier_change``:
    a later different request must not make this request ID forgettable, so an
    identical replay of the rejected request still returns the original result
    without another send.
    """
    history = list(t.get('route_history') or [])
    for hop in history:
        if isinstance(hop, dict) and hop.get('message_id') == change.get('message_id') and \
                hop.get('switch_state') == 'queued':
            hop.update(accepted=False, switch_state='rejected', error='http_' + str(error.status))
    rejected = _compact(dict(change, status='rejected'))
    common.update(t['id'], tier_change=rejected, requested_tier=None,
                  tier_history=(t.get('tier_history') or []) + [rejected],
                  route_history=history, _retier=None,
                  errors=(t.get('errors') or []) + [diagnostics.exception(
                      error, 'tier_change_dispatch', dispatched=False)])
    return rejected


def _send_running(t, tier, reason, handoff, request_id, content):
    c = common.config()
    if not t.get('session_id') or not t.get('directory'):
        raise ValueError('Task has no native session yet; retier it while queued')
    q = quota.view(common.read_json(common.STATE / 'quota.json', {}))
    profile, route_reason, _view = _route_for(t, tier, c, q)
    if not profile:
        # No route: reject explicitly and keep the original execution running.
        raise ValueError('No route is available for tier ' + tier + ': ' + str(route_reason))
    cfg = c['profiles'][profile]
    provider, model = cfg['model'].split('/', 1)
    now = time.time()
    if tier == 'ultra' and profile != t.get('profile'):
        # Cross-model entry into Ultra: queue a clean model session instead of
        # continuing the previous model's history (and its chain of thought).
        # It is opened at the next confirmed-idle boundary by
        # materialize_new_session(), so no current turn/tool/build is aborted.
        change = {'request_id': request_id, 'from_tier': t.get('tier'), 'to_tier': tier,
                  'status': 'pending', 'reason': reason, 'created_at': now}
        history = list(t.get('route_history') or [])
        history.append({'from_profile': t.get('profile'), 'from_provider':
                        str((c.get('profiles', {}).get(t.get('profile')) or {}).get('model', '')).split('/', 1)[0],
                        'to_profile': profile, 'to_provider': provider,
                        'trigger': 'tier_change', 'route_reason': route_reason, 'at': now,
                        'queued_boundary_switch': True, 'new_model_session': True,
                        'from_session_id': t.get('session_id'), 'switch_state': 'queued'})
        common.update(t['id'], tier_change=_compact(change), requested_tier=tier,
                      route_history=history,
                      _retier={'request_id': request_id, 'to_profile': profile,
                               'handoff': handoff, 'reason': reason,
                               'queued_new_session': True},
                      _retier_hashes=_remember(t, request_id, content))
        return _compact(change)
    message_id = common.message_id()
    change = {'request_id': request_id, 'from_tier': t.get('tier'), 'to_tier': tier,
              'status': 'pending', 'reason': reason, 'created_at': now, 'message_id': message_id}
    history = list(t.get('route_history') or [])
    if profile != t.get('profile'):
        # The UI-visible model change waits for observed assistant evidence; this
        # queued switch is activated by the existing boundary observer, never here.
        history.append({'from_profile': t.get('profile'), 'from_provider':
                        str((c.get('profiles', {}).get(t.get('profile')) or {}).get('model', '')).split('/', 1)[0],
                        'to_profile': profile, 'to_provider': provider,
                        'trigger': 'tier_change', 'route_reason': route_reason, 'at': now,
                        'queued_boundary_switch': True, 'message_id': message_id,
                        'switch_state': 'queued'})
    # Capture the billing provenance for the target provider at this private
    # dispatch (metadata-only credential identity), so the observed boundary
    # later rebinds with exactly what this continuation cost, never the stale
    # original segment.
    t = common.update(t['id'], tier_change=_compact(change), requested_tier=tier,
                      route_history=history, _retier={'request_id': request_id,
                                                      'to_profile': profile,
                                                      'handoff': handoff, 'reason': reason,
                                                      'billing': {'provider': provider,
                                                                  'identity': common.credential_identity(provider),
                                                                  'at': now}},
                      _retier_hashes=_remember(t, request_id, content))
    body = {'messageID': message_id, 'agent': profile,
            'model': {'providerID': provider, 'modelID': model},
            'parts': [{'type': 'text', 'text': _handoff_text(change['from_tier'], tier,
                                                              reason, handoff)}]}
    if cfg.get('variant'):
        body['variant'] = cfg['variant']
    try:
        common.api('/session/' + t['session_id'] + '/prompt_async', t['directory'], 'POST', body)
    except common.HttpFailure as error:
        if error.status and 400 <= error.status < 500:
            return _reject(t, change, error)
        # An ambiguous acknowledgement is retained and observed, never retried blindly.
        uncertain = _compact(dict(change, status='uncertain'))
        common.update(t['id'], tier_change=uncertain,
                      errors=(t.get('errors') or []) + [diagnostics.exception(
                          error, 'tier_change_dispatch', dispatched=True)])
        return uncertain
    return _compact(change)


def _discard_session(directory, session_id):
    """Best-effort removal of an empty session created for a rejected switch."""
    try:
        common.api('/session/' + session_id, directory, 'DELETE')
    except Exception:
        pass


def _settle_unavailable(t, change, error=None):
    """Reject a queued Ultra session switch that provably did not run."""
    history = [dict(hop) if isinstance(hop, dict) else hop
               for hop in (t.get('route_history') or [])]
    for hop in history:
        if isinstance(hop, dict) and hop.get('new_model_session') and \
                hop.get('switch_state') == 'queued':
            hop.update(accepted=False, switch_state='rejected', error='session_unavailable')
    rejected = _compact(dict(change, status='rejected'))
    details = (diagnostics.exception(error, 'ultra_session_open', dispatched=False) if error else
               diagnostics.error('bridge', 'ultra_session_unavailable',
                                 'The clean Ultra model session could not be opened; the '
                                 'original model and dispatch remain intact.',
                                 action='retry_after_resolving_session_or_model_error'))
    return common.update(t['id'], tier_change=rejected, requested_tier=None, _retier=None,
                         route_history=history,
                         errors=(t.get('errors') or []) + [details])


def materialize_new_session(t, native_status):
    """Open the clean Ultra model session at the first confirmed-idle boundary.

    Entering Ultra must not carry the previous model's session history or chain
    of thought. The reserved model receives one explicit Done/Remaining/Decisions/
    Evidence/Do-not-repeat checkpoint, while the task ID, authorization, scopes,
    targets, workspace and cumulative usage stay unchanged. The previous session
    is retained as history and is never aborted: materialization only runs after
    the native session is confirmed idle, so a current turn, tool, build or
    deploy is never interrupted.
    """
    change = t.get('tier_change') or {}
    private = t.get('_retier') if isinstance(t.get('_retier'), dict) else {}
    if change.get('status') != 'pending' or private.get('queued_new_session') is not True:
        return t
    if native_status.get('type') != 'idle' or t.get('status') != 'running':
        return t
    with common.locked('control-' + t['id']):
        t = common.task(t['id'])
        change = t.get('tier_change') or {}
        private = t.get('_retier') if isinstance(t.get('_retier'), dict) else {}
        if change.get('status') != 'pending' or private.get('queued_new_session') is not True or \
                t.get('status') != 'running' or t.get('cancel_requested'):
            return t
        c = common.config()
        profile = private.get('to_profile')
        cfg = (c.get('profiles') or {}).get(profile) if isinstance(profile, str) else None
        directory = t.get('directory')
        if not isinstance(cfg, dict) or not cfg.get('model') or not isinstance(directory, str):
            return _settle_unavailable(t, change)
        provider, model = cfg['model'].split('/', 1)
        try:
            import worker
            permission = worker.permissions(dict(t, auto_approve=c.get('auto_approve', True)))
        except Exception:
            return _settle_unavailable(t, change)
        try:
            session = common.api('/session', directory, 'POST', {
                'title': 'delegate:' + t['id'] + ' ultra ' + str(t.get('title') or '')[:120],
                'agent': profile,
                'model': {'id': model, 'providerID': provider,
                          **({'variant': cfg['variant']} if cfg.get('variant') else {})},
                'permission': permission})
        except common.HttpFailure as error:
            return _settle_unavailable(t, change, error)
        new_id = session.get('id') if isinstance(session, dict) else None
        if not isinstance(new_id, str) or not new_id:
            return _settle_unavailable(t, change)
        message_id = common.message_id()
        now = time.time()
        applied = dict(change, message_id=message_id)
        history = [dict(hop) if isinstance(hop, dict) else hop
                   for hop in (t.get('route_history') or [])]
        for hop in history:
            if isinstance(hop, dict) and hop.get('new_model_session') and \
                    hop.get('switch_state') == 'queued':
                hop.update(message_id=message_id, new_session_id=new_id)
        body = {'messageID': message_id, 'agent': profile,
                'model': {'providerID': provider, 'modelID': model},
                'parts': [{'type': 'text', 'text': _handoff_text(
                    change.get('from_tier'), change.get('to_tier'),
                    private.get('reason', ''), private.get('handoff') or {})}]}
        if cfg.get('variant'):
            body['variant'] = cfg['variant']
        try:
            common.api('/session/' + new_id + '/prompt_async', directory, 'POST', body)
        except common.HttpFailure as error:
            _discard_session(directory, new_id)
            return _settle_unavailable(t, change, error)
        identity = common.credential_identity(provider)
        return common.update(t['id'], session_id=new_id, message_id=message_id,
                             dispatch_attempted_at=now, dispatch_acknowledged=False,
                             tier_change=_compact(applied), route_history=history,
                             _retier={'request_id': private.get('request_id'),
                                      'to_profile': profile,
                                      'handoff': private.get('handoff'),
                                      'reason': private.get('reason', ''),
                                      'billing': {'provider': provider,
                                                  'identity': identity, 'at': now}},
                             _billing_dispatch={'provider': provider,
                                                'identity': identity, 'at': now})


def send(task_id, tier, reason, handoff, request_id):
    """Request one same-task tier change; the bridge routes, never the caller.

    Requires a stable request ID for deterministic replay safety. Queued tasks
    apply before dispatch; running tasks preserve the current tier until an
    assistant response actually acknowledges the new continuation.
    """
    reason, request_id = _validated(tier, reason, request_id)
    handoff = normalize_handoff(handoff)
    content = _content_sha(tier, reason, handoff)
    with common.locked('control-' + task_id):
        t = common.task(task_id)
        replay = _replay(t, request_id, content)
        if replay is not None:
            return replay
        change = t.get('tier_change') or {}
        if change.get('status') in UNRESOLVED:
            raise ValueError('A tier change is already ' + str(change.get('status')) +
                             '; at most one unresolved tier change per task')
        status = t.get('status')
        if status in common.TERMINAL:
            raise ValueError('Task is ' + str(status) +
                             '; a tier change only applies to a live task')
        if t.get('cancel_requested'):
            raise ValueError('Task cancellation is in flight; no tier change is accepted')
        if (t.get('requested_profile') or 'auto') != 'auto':
            raise ValueError('Task pins a named profile; same-task retiering requires '
                             'routing profile=auto (conflicts are rejected, neither side relaxed)')
        floor = t.get('capability_floor')
        if floor in TIERS and TIERS.index(floor) > TIERS.index(tier):
            raise ValueError('Tier ' + tier + ' is below the task capability floor ' +
                             floor + '; the floor is not relaxed')
        if tier == t.get('tier'):
            raise ValueError('Task already runs at tier ' + str(tier))
        if any(isinstance(hop, dict) and hop.get('queued_boundary_switch') and
               hop.get('switch_state') == 'queued' for hop in t.get('route_history') or []):
            raise ValueError('A routing switch is already queued; retry after it activates')
        if status == 'queued':
            return _apply_queued(t, tier, reason, handoff, request_id, content)
        if status != 'running':
            raise ValueError('Task is ' + str(status) +
                             '; a tier change applies when the task is queued or running')
        return _send_running(t, tier, reason, handoff, request_id, content)


def _ack_message(change, messages):
    """The assistant response that acknowledges the new continuation (parentID bound)."""
    message_id = change.get('message_id')
    if not message_id:
        return None
    return next((m.get('info', {}) for m in messages
                 if isinstance(m, dict) and m.get('info', {}).get('role') == 'assistant'
                 and m.get('info', {}).get('parentID') == message_id), None)


def _ack_time(info):
    time_map = info.get('time') if isinstance(info.get('time'), dict) else {}
    raw = time_map.get('created')
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw / 1000 if raw >= 1e11 else float(raw)
    return time.time()


def observe(t, messages):
    """Apply a pending/uncertain tier change once the new continuation is acknowledged.

    Acknowledgement is an assistant response correlated to the tier message ID,
    even when the routed model did not change. Nothing here replays a prompt.
    Only this observed evidence may rebind the active dispatch segment and
    activate the new routing; HTTP acceptance alone never does.
    """
    change = t.get('tier_change') or {}
    if change.get('status') not in UNRESOLVED or t.get('status') not in common.ACTIVE:
        return t
    ack = _ack_message(change, messages)
    if ack is None:
        return t
    private = t.get('_retier') or {}
    now = _ack_time(ack)
    applied = _compact(dict(change, status='applied', applied_at=now))
    fields = dict(_tier_sync(applied['to_tier']))
    fields.update(tier_change=applied, tier_history=(t.get('tier_history') or []) + [applied],
                  requested_tier=None, _retier=None)
    profile = private.get('to_profile')
    if profile and profile != t.get('profile'):
        # Activate the new routing only now: the acknowledgement is assistant
        # evidence, not the earlier HTTP acceptance.
        fields['profile'] = profile
    if change.get('message_id'):
        # Bind the active segment to the continuation that actually ran: its
        # message ID, the time that dispatch was attempted and the credential
        # identity captured for the target provider at its private dispatch.
        # This applies even when the routed model did not change, so billing
        # evidence and later recovery stop crediting the stale pre-steer
        # segment.
        fields['message_id'] = change['message_id']
        fields['dispatch_attempted_at'] = change.get('created_at') or now
        billing = private.get('billing') if isinstance(private.get('billing'), dict) else {}
        if billing.get('provider'):
            fields['_billing_dispatch'] = dict(billing)
    return common.update(t['id'], **fields)


def _settle_lost(t, change, why):
    """Settle an unobserved continuation when the boundary definitively did not run."""
    history = list(t.get('route_history') or [])
    for hop in history:
        if isinstance(hop, dict) and hop.get('message_id') == change.get('message_id') and \
                hop.get('switch_state') == 'queued':
            hop.update(accepted=False, switch_state='rejected', error=why)
    rejected = _compact(dict(change, status='rejected'))
    common.update(t['id'], tier_change=rejected, requested_tier=None,
                  tier_history=(t.get('tier_history') or []) + [rejected],
                  route_history=history, _retier=None,
                  errors=(t.get('errors') or []) + [diagnostics.error(
                      'bridge', why, 'The accepted tier continuation was not observed before the '
                      'session boundary stopped; the original tier and dispatch remain intact.',
                      action='retier_again_if_still_useful', phase='tier_change_observe')])
    return common.task(t['id'])


def resolve_before_stop_recovery(t, messages, aborted=False):
    """Settle an unresolved tier change against confirmed-idle evidence.

    Used by the capacity recovery lifecycle when the native session is already
    idle (and, for the usage-window path, definitively aborted by the bridge):
    either the continuation was acknowledged (apply it), or it provably did
    not run (settle it as rejected; the original tier and dispatch stay
    intact). This keeps an accepted continuation from being stranded behind
    the completion gate and never terminally loses a task whose tier change
    was still unresolved. A continuation that is delivered but may still run
    is retained for observation and blocks this recovery pass instead.
    Nothing here ever replays the continuation prompt.
    """
    change = t.get('tier_change') or {}
    if change.get('status') not in UNRESOLVED:
        return t
    t = observe(t, messages)
    change = (t.get('tier_change') or {})
    if change.get('status') not in UNRESOLVED:
        return t
    ids = {m.get('info', {}).get('id') for m in messages if isinstance(m, dict)}
    if change.get('message_id') not in ids:
        return _settle_lost(t, change, 'tier_continuation_unobserved_at_stop')
    if aborted:
        # The bridge confirmed the abort: a delivered-but-unprocessed
        # continuation cannot run anymore. Settle it deterministically.
        return _settle_lost(t, change, 'tier_continuation_aborted_before_ack')
    return t


def settled(task, messages):
    """True when completion may proceed: no tier continuation is owed acknowledgement."""
    change = task.get('tier_change') or {}
    if change.get('status') not in UNRESOLVED:
        return True
    return _ack_message(change, messages) is not None
