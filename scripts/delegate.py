#!/usr/bin/env python3
"""CLI and durable queue daemon for the general-purpose OpenCode worker pool."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from common import (ACTIVE, CONFIG, STATE, TERMINAL, api, artifact_dir, config, init, locked,
                    coordinator_task, read_json, redact, request_cancel, task, task_path, tasks, update,
                    write_json)
import quota
import diagnostics
import routing
from workspace import conflicts, git_root, integrate, relative_scope
from worker import run_task, recovered_capacity_exclusions, capacity_resume_candidate

WAIT_DEFAULT_SECONDS = {'fast': 300, 'normal': 1800, 'deep': 3600, 'ultra': 3600}
TIERS = ('fast', 'normal', 'deep', 'ultra')
WAIT_MIN_SECONDS = 60
WAIT_POLL_SECONDS = 0.5


def wait_pause(seconds):
    """One injectable task-wait pause without monkey-patching process-wide time.sleep."""
    time.sleep(seconds)


def owner_key(t):
    """Immutable scheduling owner: the upstream-harness conversation, with legacy fallbacks."""
    return t.get('owner_thread_id') or t.get('group_id') or t.get('source_dir')


def per_owner_cap(c):
    cap = c.get('max_parallel_per_owner', 4)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 16:
        return 4
    return cap


def live_recovery(t):
    """Fresh guidance for queued blockages and billing-failed tasks.

    Live derivation replaces the persisted snapshot; when routing is healthy again the
    result is None so stale recovery never lingers. Automatic routing projects only a
    generic whole-route exhaustion after every configured route across every tier is
    proven unavailable; partial/unproven tier stops return no recovery object at all.
    Explicit pins keep the detailed
    operator recovery. The persisted snapshot is never exposed for an automatic task,
    because it may contain the individual provider/quota stop that transparent routing
    deliberately keeps operator-only.
    """
    if t.get('status') not in ('queued', 'failed', 'needs_attention'):
        return None
    automatic = (t.get('requested_profile') or 'auto') == 'auto'
    try:
        return quota.coordinator_recovery(t, config(),
                                          quota.view(read_json(STATE / 'quota.json', {})))
    except Exception:
        if automatic:
            return None
        return t.get('recovery') if isinstance(t.get('recovery'), dict) else None


def wait_result(t, waited_seconds):
    """Make a bounded wait unambiguous to an agent coordinator."""
    result = task_status(t)
    terminal = t['status'] in TERMINAL
    recovery = result.get('recovery')
    # A blocked queued task or a terminal billing failure needs coordinator
    # re-selection, not another blind wait or a generic error inspection.
    blocked = t['status'] == 'queued' and isinstance(recovery, dict)
    billing_terminal = t['status'] in ('failed', 'needs_attention') and isinstance(recovery, dict)
    actionable = blocked or billing_terminal
    alternatives = recovery.get('alternatives') if isinstance(recovery, dict) else None
    monthly = isinstance(recovery, dict) and recovery.get('billing_reason') == 'monthly_usage_limit'
    window = isinstance(recovery, dict) and recovery.get('usage_window_exhausted') is True
    all_routes_exhausted = isinstance(recovery, dict) and \
        recovery.get('automatic_routes_exhausted') is True
    result.update(
        terminal=terminal,
        continue_waiting=not terminal and not blocked,
        waited_seconds=max(0, round(waited_seconds, 3)),
        next_action=(
            'wait_for_capacity_recovery' if actionable and all_routes_exhausted else
            'resubmit_same_tier_auto' if actionable and alternatives else
            'release_manual_retry_then_resubmit' if actionable and monthly and
            isinstance(recovery, dict) and recovery.get('manual_release_required') else
            'top_up_or_authorize_manual_retry' if actionable and monthly else
            'retry_or_reselect_after_usage_window' if actionable and window else
            'top_up_provider_account_then_resubmit' if billing_terminal else
            'top_up_provider_account_then_wait' if blocked else
            'call_wait_again' if not terminal else
            'collect_and_review' if t['status'] == 'completed' else
            'collect_and_inspect_errors' if t['status'] != 'cancelled' else
            'stop'
        ),
    )
    if recovery:
        result['recovery'] = recovery
    if actionable:
        result['attention'] = True
    return result


def wait_window_seconds(t, requested=None):
    """Choose a useful observation window without turning it into a worker deadline."""
    if requested is not None:
        return max(WAIT_MIN_SECONDS, requested)
    return WAIT_DEFAULT_SECONDS.get(t.get('tier'), WAIT_DEFAULT_SECONDS['normal'])


def wait_ready(t):
    """Return promptly for terminal work or an actionable queued blockage."""
    if t.get('status') in TERMINAL:
        return True
    return t.get('status') == 'queued' and isinstance(live_recovery(t), dict)


def wait_for_task(task_id, requested_seconds=None):
    began = time.time()
    current = task(task_id)
    window = wait_window_seconds(current, requested_seconds)
    end = began + window
    while not wait_ready(current):
        remaining = end - time.time()
        if remaining <= 0:
            break
        wait_pause(min(WAIT_POLL_SECONDS, remaining))
        current = task(task_id)
    current = task(task_id)  # Include a state change racing the observation-window boundary.
    result = wait_result(current, time.time() - began)
    result['wait_window_seconds'] = window
    return result


def task_status(t):
    """Expose current recovery options without retaining an obsolete snapshot."""
    result = coordinator_task(t)
    result.pop('recovery', None)
    recovery = live_recovery(t) if t.get('id') else None
    if recovery:
        result['recovery'] = recovery
    return result


def routing_assignment(t, profile, reason):
    """Persist one bridge-owned routing decision and disclose true fallback use."""
    fields = {
        'profile': profile,
        'status': 'starting',
        'route_reason': reason,
        'started_at': time.time(),
        'queue_reason': None,
        'recovery': None,
    }
    stage = next((part for part in str(reason).split(':') if part.startswith('stage')), '')
    try:
        stage_index = int(stage[5:])
    except (TypeError, ValueError):
        stage_index = -1
    if ((t.get('requested_profile') or 'auto') == 'auto' and profile == 'fallback'
            and stage_index > 0):
        fields.update(
            fallback_used=True,
            routing_notice=(
                'Preferred routing stages were unavailable or exhausted; '
                'the bridge dispatched the configured fallback model.'
            ),
        )
    if 'ultra_fallback_deep' in str(reason):
        # Truthful disclosure: the task keeps its Ultra tier and identity, while
        # the actual running model is the Deep primary until Ultra recovers.
        fields.update(
            fallback_used=True,
            routing_notice=(
                'Ultra capacity was confirmed exhausted; the bridge started this '
                'Ultra-tier task on the Deep primary model. It returns to Ultra at '
                'a safe idle checkpoint once Ultra capacity recovers.'
            ),
        )
    return fields


def submit(spec):
    c = config()
    root = Path(spec['directory']).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('Task directory does not exist')
    if not spec.get('objective', '').strip():
        raise ValueError('A non-empty objective is required')
    notify_on_complete = spec.get('notify_on_complete', False)
    if not isinstance(notify_on_complete, bool):
        raise ValueError('notify_on_complete must be boolean')
    requested_profile = spec.get('profile', 'auto')
    if requested_profile not in {'auto', *c['profiles']}:
        raise ValueError('Unknown profile')
    if requested_profile != 'auto' and c['profiles'][requested_profile].get('enabled') is False:
        raise ValueError('Profile is disabled')
    profile_reason = spec.get('profile_reason')
    if profile_reason is not None and (not isinstance(profile_reason, str) or not profile_reason.strip()):
        raise ValueError('Profile reason must be a non-empty string')
    if isinstance(profile_reason, str):
        profile_reason = profile_reason.strip()
        if len(profile_reason) > 500:
            raise ValueError('Profile reason must be at most 500 characters')
    if requested_profile == 'auto' and profile_reason:
        raise ValueError('Profile reason applies only to an explicit profile')
    if c.get('routing_policy') and requested_profile != 'auto' and not profile_reason:
        raise ValueError('Explicit profile requires profile_reason when routing policy is enabled; '
                         'use profile=auto for subscription-first routing')
    tier = spec.get('tier')
    if tier is not None and tier not in TIERS:
        raise ValueError('Tier must be fast, normal, deep or ultra')
    legacy_urgency = spec.get('urgency')
    legacy_complexity = spec.get('complexity')
    if legacy_urgency is not None and legacy_urgency not in ('fast', 'background'):
        raise ValueError('Urgency must be fast or background')
    if legacy_complexity is not None and legacy_complexity not in ('normal', 'deep'):
        raise ValueError('Complexity must be normal or deep')
    legacy_tier = ('deep' if legacy_complexity == 'deep' else
                   'fast' if legacy_urgency == 'fast' else 'normal')
    if tier is None:
        tier = legacy_tier
    elif (legacy_urgency is not None or legacy_complexity is not None) and tier != legacy_tier:
        raise ValueError('Tier conflicts with legacy urgency/complexity fields')
    floor = spec.get('capability_floor')
    floor_reason = spec.get('capability_reason')
    if floor is not None:
        if floor not in TIERS or TIERS.index(floor) > TIERS.index(tier):
            raise ValueError('Capability floor must be a tier no higher than the requested tier')
        if not isinstance(floor_reason, str) or not 1 <= len(floor_reason.strip()) <= 500:
            raise ValueError('Capability floor requires a capability_reason of 1-500 characters')
    elif floor_reason is not None:
        raise ValueError('Capability reason requires capability_floor')
    urgency = 'fast' if tier == 'fast' else 'background'
    # Ultra is the reserved high-capability tier; it inherits the deep reasoning
    # mirror so legacy consumers never read it as ordinary work.
    complexity = 'deep' if tier in ('deep', 'ultra') else 'normal'
    mode = spec.get('mode', 'read')
    if mode not in ('read', 'write'):
        raise ValueError('Mode must be read or write')
    scopes = [relative_scope(s, root) for s in spec.get('scopes', [])]
    targets = spec.get('targets', [])
    if not isinstance(targets, list) or any(not isinstance(x, str) or not x.strip() for x in targets):
        raise ValueError('Targets must be an array of non-empty strings')
    targets = [x.strip() for x in targets]
    if mode == 'write' and not scopes and not targets:
        raise ValueError('Write tasks require at least one explicit local scope or operational target')
    workspace = spec.get('workspace', 'auto')
    if workspace == 'auto':
        # The installed development checkout is the normal execution surface.
        # Scope/resource conflict checks serialize overlapping writers, so repository
        # breadth alone must not create a disk-heavy worktree. Isolation is explicit.
        workspace = 'shared'
    if workspace not in ('shared', 'isolated'):
        raise ValueError('Workspace must be auto, shared or isolated')
    if workspace == 'isolated' and git_root(root) != root:
        raise ValueError('For isolated work, specify the Git repository root')
    for command in spec.get('commands', []):
        if not isinstance(command, str) or not command.strip():
            raise ValueError('Allowed commands must be non-empty strings')
    # Restricted mode treats commands as an exact native allowlist, so permission
    # wildcards are rejected there. Auto Approve runs tools without prompts and
    # documents commands as suggested checks, where ordinary shell globs are legal.
    if not c.get('auto_approve', True) and any(x in command for command in spec.get('commands', [])
                                               for x in '*?[]'):
        raise ValueError('Restricted-mode allowed commands must be literal commands without wildcards')
    t = {
        'id': 'job-' + uuid.uuid4().hex[:16], 'title': spec.get('title') or spec['objective'][:80],
        'objective': spec['objective'], 'acceptance': spec.get('acceptance', []),
        'requested_profile': requested_profile, 'mode': mode,
        'tier': tier, 'urgency': urgency, 'complexity': complexity,
        'capability_floor': floor, 'capability_reason': floor_reason.strip() if floor_reason else None,
        'workspace': workspace, 'source_dir': str(root), 'scopes': scopes, 'targets': targets,
        'commands': spec.get('commands', []), 'resources': spec.get('resources', []),
        'web': bool(spec.get('web', False)),
        'notify_on_complete': notify_on_complete,
        'status': 'queued', 'created_at': time.time(), 'updated_at': time.time(),
        'owner_thread_id': os.environ.get('CODEX_THREAD_ID'),
        'group_id': spec.get('group_id') or os.environ.get('CODEX_THREAD_ID') or str(root),
        'group_title': spec.get('group_title') or root.name,
        'parent_task_id': spec.get('parent_task_id'),
    }
    if profile_reason:
        t['profile_reason'] = profile_reason
    if t['parent_task_id']:
        parent = task(t['parent_task_id'])
        if parent.get('group_id') != t['group_id']:
            raise ValueError('Parent task must belong to the same group')
        if not t.get('capability_floor') and parent.get('capability_floor'):
            inherited = parent['capability_floor']
            if inherited in TIERS and TIERS.index(inherited) > TIERS.index(tier):
                raise ValueError('Continuation tier cannot fall below the parent capability floor')
            t.update(capability_floor=inherited, capability_reason=parent.get('capability_reason'))
        parent_recovery = parent.get('recovery') if isinstance(parent.get('recovery'), dict) else {}
        blocked_provider = parent_recovery.get('blocked_provider')
        if requested_profile == 'auto' and isinstance(blocked_provider, str) and blocked_provider:
            inherited = dict(parent, excluded_providers=[blocked_provider])
            recovered = recovered_capacity_exclusions(
                inherited, c, quota.view(read_json(STATE / 'quota.json', {})))
            t['excluded_providers'] = recovered['excluded_providers']
            t['_capacity_exclusions'] = parent.get('_capacity_exclusions') or {}
    # Block accidental delegation of known stored API keys before persisting prompt text.
    if redact(t) != t:
        raise ValueError('Task contains a credential-like value; remove it before submission')
    with locked():
        if (STATE / 'maintenance.json').exists():
            raise ValueError('Configuration update in progress; retry shortly')
        write_json(task_path(t['id']), t)
    return coordinator_task(t)


def _capacity_admission_fingerprint(t):
    """Only stable admission fields; observer timestamp updates do not invalidate it."""
    return json.dumps({key: t.get(key) for key in (
        'status', '_capacity_wait', '_capacity_exclusions', 'excluded_providers', 'excluded_profiles',
        'profile', 'requested_profile', 'tier', 'capability_floor', 'tier_change',
        'message_id', 'cancel_requested')}, sort_keys=True)


def preview_capacity_admissions(all_tasks, c, q):
    """Pool credential/generation checks happen BEFORE the scheduler state lock."""
    views = {}
    for t in all_tasks:
        waiting = (t.get('_capacity_wait') or {}).get('phase') == 'waiting'
        if t.get('status') == 'queued' or t.get('status') in ACTIVE and waiting:
            if waiting:
                candidate, profile, _ = capacity_resume_candidate(t, c, q)
            else:
                candidate, profile = recovered_capacity_exclusions(t, c, q), None
            views[t['id']] = {'fingerprint': _capacity_admission_fingerprint(t),
                              'candidate': candidate, 'resume_profile': profile}
    return views


def choose_ready(all_tasks, c, q, admissions=None, recovery_views=None):
    """Select dispatchable queued tasks.

    No global or per-provider concurrency caps: independent upstream-harness conversations run
    freely. Only the per-owner cap, true quota/billing blocks, and scope/resource
    overlap locks constrain dispatch. Profiles are pinned; billing/quota blockages are
    reported, never silently re-routed.

    Weighted routing_policy credits are advanced only after a task passes every check,
    so owner-cap, scope/resource and quota blocks never consume a turn. Explicit
    requested_profile tasks bypass the policy entirely and are never charged even when
    that profile also appears in a stage. ``admissions`` is the daemon's durable batch;
    the daemon calls this under the shared state lock and saves the batch after the
    admitted tasks are durable. When omitted, this is a read-only preview that neither
    advances nor writes counters.
    """
    if (STATE / 'maintenance.json').exists():
        return []
    if recovery_views is None:
        # Standalone previews/tests have no enclosing scheduler state lock.
        recovery_views = preview_capacity_admissions(all_tasks, c, q)
    def current_view(t):
        view = recovery_views.get(t['id']) or {}
        return view if view.get('fingerprint') == _capacity_admission_fingerprint(t) else {}
    active = [t for t in all_tasks if t['status'] in ACTIVE]
    executing = []
    for t in active:
        if (t.get('_capacity_wait') or {}).get('phase') != 'waiting':
            executing.append(t)
        elif current_view(t).get('resume_profile'):
            # Ready original work reserves the next execution slot before new
            # queued work can fill it. Still-blocked waiters reserve no slot.
            executing.append(dict(t, profile=current_view(t)['resume_profile']))
    choices = []
    waiting = sorted((t for t in all_tasks if t['status'] == 'queued'), key=lambda t: (
        not bool(t.get('session_id') or (t.get('_retier') or {}).get('queued_checkpoint')),
        t.get('created_at', 0)))
    cap = per_owner_cap(c)
    for t in waiting:
        if t.get('cancel_requested'):
            choices.append((t, None, 'cancelled'))
            continue
        if sum(1 for a in executing if owner_key(a) == owner_key(t)) >= cap:
            choices.append((t, None, 'owner_at_capacity'))
            continue
        candidate = current_view(t).get('candidate')
        if candidate:
            # Merge only the computed exclusion decision. Never overwrite newer
            # authority/progress/steering fields with an outside-lock snapshot.
            t = dict(t, excluded_providers=candidate.get('excluded_providers', []))
        explicit = (t.get('requested_profile') or 'auto') != 'auto'
        profile, why = quota.route(t, c, q, admissions)
        if profile is None:
            if admissions is not None:
                admissions.discard()
            choices.append((t, None, why))
            continue
        # The weekly reserve blocks ordinary Kimi models inside admission itself
        # (explicit requests included). The admitted concurrency of the reserved
        # Ultra model is capped uniformly by the ACTUAL selected model, so an
        # explicit profile or alias can no longer bypass the low-weekly limit.
        selected_model = (c.get('profiles', {}).get(profile) or {}).get('model')
        if selected_model == routing.KIMI_RESERVED_MODEL:
            low, remaining = quota.kimi_ultra_low_weekly(q, c)
            if low:
                running = sum(
                    1 for item in executing
                    if (c.get('profiles', {}).get(item.get('profile')) or {}).get('model')
                    == routing.KIMI_RESERVED_MODEL)
                if running >= quota.kimi_ultra_limit(c):
                    if admissions is not None:
                        admissions.discard()
                    choices.append((t, None, 'ultra_weekly_concurrency_limited:' +
                                    str(round(remaining, 3)) + 'pct'))
                    continue
        if any(conflicts(t, a) for a in active):
            if admissions is not None:
                admissions.discard()
            choices.append((t, None, 'scope_or_resource_in_use'))
            continue
        # Only a real admission consumes its weighted share; the batch sees prior
        # committed choices so one scheduling pass distributes correctly.
        if admissions is not None:
            if explicit:
                admissions.discard()
            else:
                admissions.commit()
        selected = dict(t, profile=profile, status='starting')
        active.append(selected)
        executing.append(selected)
        choices.append((t, profile, why))
    return choices


def daemon():
    init()
    maintenance = read_json(STATE / 'maintenance.json', {}) or {}
    if 'pool' in maintenance.get('paused_services', []):
        return  # Direct CLI starts must honor the same barrier as service.start.
    lock = open(STATE / 'daemon.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Per-job threads: no hidden executor cap across independent owners.
    threads = {}
    cleanup_thread = None
    memory_thread = None
    holiday_thread = None
    update_launch_checked = 0
    last_holiday_refresh = 0
    last_refresh = 0
    last_cleanup = 0
    last_memory_maintenance = 0
    q = quota.view(read_json(STATE / 'quota.json', {}))
    while not stop.is_set():
        try:
            maintenance = read_json(STATE / 'maintenance.json', {}) or {}
            if 'pool' in maintenance.get('paused_services', []):
                stop.set()
                break  # Stop observing; preserve native sessions and checkpoints.
            c = config()
            if time.time() - update_launch_checked >= 60:
                update_launch_checked = time.time()
                try:
                    import opencode_update
                    opencode_update.launch_if_due()
                except Exception:
                    pass
            import holiday_calendar
            holiday_cfg = holiday_calendar.settings(c.get('deepseek_holiday_calendar'))
            holiday_interval = holiday_cfg['refresh_hours'] * 3600
            if holiday_cfg['enabled'] and \
                    time.time() - last_holiday_refresh >= max(3600, holiday_interval) and \
                    (holiday_thread is None or not holiday_thread.is_alive()):
                last_holiday_refresh = time.time()
                holiday_thread = threading.Thread(target=holiday_calendar.refresh,
                                                  daemon=True, name='holiday-calendar')
                holiday_thread.start()
            import cleanup
            cp = cleanup.policy()
            if cp['enabled'] and time.time() - last_cleanup >= cp['interval_seconds'] and \
                    (cleanup_thread is None or not cleanup_thread.is_alive()):
                last_cleanup = time.time()
                cleanup_thread = threading.Thread(target=cleanup.periodic,
                                                  daemon=True, name='cleanup')
                cleanup_thread.start()
            memory_cfg = c.get('memory') if isinstance(c.get('memory'), dict) else {}
            rolling = memory_cfg.get('rolling') if isinstance(memory_cfg.get('rolling'), dict) else {}
            memory_interval = int(rolling.get('check_interval_hours') or 24) * 3600
            if memory_cfg.get('enabled') is True and rolling.get('enabled', True) is True and \
                    time.time() - last_memory_maintenance >= max(3600, memory_interval) and \
                    (memory_thread is None or not memory_thread.is_alive()):
                last_memory_maintenance = time.time()
                memory_thread = threading.Thread(
                    target=lambda: __import__('memory_manager').maintain(),
                    daemon=True, name='memory-maintenance')
                memory_thread.start()
            all_tasks = tasks()
            # Publish the compact Ultra demand snapshot from this already-read
            # listing. The reserve helper and the native gate never rescan task
            # history; a failed or absent snapshot stays conservatively unknown
            # and therefore keeps the base weekly reserve.
            try:
                import kimi_reserve
                if kimi_reserve.uses_kimi(c):
                    kimi_reserve.publish_task_demand(c, all_tasks,
                                                     kimi_reserve.current_identity())
            except Exception:
                pass
            just_finished = any(not th.is_alive() for th in threads.values())
            needs_poll = any(t['status'] in ACTIVE | {'queued'} for t in all_tasks)
            has_queue = any(t['status'] == 'queued' for t in all_tasks)
            capacity_waiting = any(t.get('_capacity_wait') for t in all_tasks)
            if just_finished or (needs_poll and time.time() - last_refresh >=
                                 (60 if has_queue or capacity_waiting else 300)):
                q, last_refresh = quota.refresh(), time.time()
                try:
                    import notifications
                    notifications.observe_quota(c, q)
                except Exception:
                    pass  # Notifications are best-effort and never stop scheduling.
            threads = {k: th for k, th in threads.items() if th.is_alive()}
            recovery_views = preview_capacity_admissions(all_tasks, c, q)
            with locked():
                policy = routing.configured(c)
                admissions = routing.Admissions.load(policy) if policy else None
                for t, profile, reason in choose_ready(tasks(), c, q, admissions, recovery_views):
                    if reason == 'cancelled':
                        t.update(status='cancelled', finished_at=time.time())
                    elif profile:
                        t.update(routing_assignment(t, profile, reason))
                    else:
                        t['queue_reason'] = reason
                        t['recovery'] = quota.guidance(t, c, q)
                    write_json(task_path(t['id']), t)
                if admissions is not None:
                    # Persist consumed credits strictly after the admitted tasks are
                    # durable. A crash in between leaves at most this batch's credits
                    # undercounted for tasks already starting; they are never admitted
                    # again, so nothing is double-charged and the bounded credits
                    # self-correct instead of skewing indefinitely.
                    admissions.save()
            for t in tasks():
                if t['status'] in ACTIVE and t['id'] not in threads:
                    th = threading.Thread(target=run_task, args=(t['id'], stop),
                                          daemon=True, name='task-' + t['id'])
                    th.start()
                    threads[t['id']] = th
            write_json(STATE / 'heartbeat.json', {'pid': os.getpid(), 'time': time.time(),
                       'active': list(threads), 'version': 1})
        except Exception as e:
            write_json(STATE / 'daemon-error.json', {'time': time.time(), 'error': diagnostics.exception(e, 'schedule')})
        stop.wait(2)
    # Daemon threads exit on the shared stop event; one shared deadline bounds settling.
    deadline = time.time() + 10
    for th in threads.values():
        th.join(timeout=max(0, deadline - time.time()))
    lock.close()


def detect_wizard_lang():
    for var in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        value = os.environ.get(var, '')
        if value:
            return 'zh-CN' if value.lower().startswith('zh') else 'en'
    return 'en'


def setup_wizard_command(lang=None):
    """Open the interactive setup/review wizard for the installed runtime.

    With a TTY this runs the co-located installer wizard, which only mutates
    anything after an explicit review confirmation. Without a TTY it documents
    the exact installed install.py --wizard command (or the noninteractive
    installer flags) so a coordinator can surface it to the user.
    """
    lang = lang or detect_wizard_lang()
    installer = Path(__file__).resolve().parent / 'install.py'
    if sys.stdin.isatty() and sys.stdout.isatty() and installer.is_file():
        code = subprocess.call([sys.executable, str(installer), '--wizard', '--lang', lang])
        return {'wizard': 'completed' if code == 0 else 'cancelled' if code == 2 else 'failed',
                'lang': lang, 'exit_code': code}
    return {'setup': 'interactive terminal required; no changes were made', 'lang': lang,
            'command': 'python3 {0} --wizard --lang {1}'.format(shlex.quote(str(installer)), lang),
            'noninteractive': 'python3 {0} --model provider/model [--no-start] or '
                              '--preset deepseek-kimi'.format(shlex.quote(str(installer)))}


def doctor():
    result = {'config_path': str(CONFIG), 'state_path': str(STATE), 'profiles': config()['profiles']}
    try:
        result['server'] = api('/global/health')
        agents = api('/agent')
        result['registered_profiles'] = [a['name'] for a in agents if a['name'] in config()['profiles']]
    except Exception as e:
        result['server_error'] = diagnostics.exception(e, 'health')
    heartbeat = read_json(STATE / 'heartbeat.json', {})
    result['daemon'] = {'healthy': time.time() - heartbeat.get('time', 0) < 45,
                        'active': heartbeat.get('active', [])}
    result['last_daemon_error'] = read_json(STATE / 'daemon-error.json')
    if config().get('console_url'):
        result['console_url'] = config()['console_url'] + '/console'
    return result


def stats():
    groups = {}
    for t in tasks():
        if t.get('finished_at') and t.get('profile'):
            g = groups.setdefault(t['profile'], {'finished': 0, 'completed': 0, 'durations': [], 'queue_delays': []})
            g['finished'] += 1
            g['completed'] += t['status'] == 'completed'
            if t.get('elapsed_seconds') is not None:
                g['durations'].append(t['elapsed_seconds'])
            if t.get('started_at'):
                g['queue_delays'].append(t['started_at'] - t['created_at'])
    for g in groups.values():
        g['median_seconds'] = statistics.median(g.pop('durations')) if g['durations'] else None
        g['median_queue_seconds'] = statistics.median(g.pop('queue_delays')) if g['queue_delays'] else None
        g['note'] = 'Mixed task durations; compare like-for-like before changing routing'
    return groups


def load_submit_spec(args):
    """Load a JSON submit specification from a file, stdin or a shell-safe URI argument."""
    if args.spec_urlencoded is not None:
        source = '--spec-urlencoded'
        try:
            raw = urllib.parse.unquote_to_bytes(args.spec_urlencoded).decode('utf-8')
        except UnicodeDecodeError as e:
            raise ValueError('--spec-urlencoded must contain percent-encoded UTF-8 JSON') from e
    elif args.spec == '-':
        source = '--spec - stdin'
        raw = sys.stdin.read()
        if not raw.strip():
            raise ValueError(
                '--spec - received empty stdin; functions.exec_command does not send an '
                'in-memory value as stdin. Use --spec /absolute/private/task.json, use '
                '--spec-urlencoded with percent-encoded JSON, or open a TTY session and '
                'write the JSON to its stdin.'
            )
    else:
        source = '--spec file'
        raw = Path(args.spec).read_text()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f'Invalid JSON from {source} at line {e.lineno}, column {e.colno}: {e.msg}'
        ) from None
    if not isinstance(value, dict):
        raise ValueError('Task specification must be one JSON object')
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('submit')
    spec_group = s.add_mutually_exclusive_group()
    spec_group.add_argument('--spec', help='JSON task specification file; use - only when stdin is supplied')
    spec_group.add_argument('--spec-urlencoded',
                            help='Percent-encoded UTF-8 JSON task specification')
    s.add_argument('--directory', default=os.getcwd())
    s.add_argument('--profile', default='auto')
    s.add_argument('--profile-reason', help='Why this task must bypass automatic routing')
    s.add_argument('--tier', choices=['fast', 'normal', 'deep', 'ultra'],
                   help='Worker capability tier; the bridge selects the model. Ultra is the '
                        'reserved native Kimi K3.1 Pro tier for the most extreme-complexity work. '
                        'On confirmed capacity exhaustion a default Ultra task may step down one '
                        'level to the Deep primary (Max) in the same session and returns at a safe '
                        'idle checkpoint; it never goes below Deep. Explicit model pins and '
                        'capability_floor=ultra never step down and wait instead.')
    s.add_argument('--capability-floor', choices=['fast', 'normal', 'deep', 'ultra'])
    s.add_argument('--capability-reason', help='Why this task cannot use lower capability routes')
    s.add_argument('--mode', choices=['read', 'write'], default='read')
    s.add_argument('--urgency', choices=['fast', 'background'], help=argparse.SUPPRESS)
    s.add_argument('--complexity', choices=['normal', 'deep'], help=argparse.SUPPRESS)
    s.add_argument('--workspace', choices=['auto', 'shared', 'isolated'], default='auto')
    s.add_argument('--large', action='store_true')
    s.add_argument('--scope', action='append', default=[])
    s.add_argument('--command', action='append', default=[])
    s.add_argument('--resource', action='append', default=[],
                   help='Shared lock name (repeatable); use stable names such as ssh:host:service')
    s.add_argument('--target', action='append', default=[],
                   help='Operational target (repeatable), e.g. ssh:example.com:nginx')
    s.add_argument('--acceptance', action='append', default=[])
    # Accept old callers without reintroducing an execution deadline.
    s.add_argument('--timeout-seconds', type=int, help=argparse.SUPPRESS)
    s.add_argument('--web', action='store_true')
    s.add_argument('--notify-on-complete', action='store_true',
                   help='Send one Bark notification only if this task completes successfully')
    s.add_argument('--title')
    s.add_argument('--group-id')
    s.add_argument('--group-title')
    s.add_argument('--parent-task-id')
    s.add_argument('objective', nargs='?')
    s = sub.add_parser('status'); s.add_argument('id', nargs='?')
    s = sub.add_parser('collect'); s.add_argument('id'); s.add_argument('--full', action='store_true')
    s = sub.add_parser('transcript', help='Read session messages and tool calls only when needed')
    s.add_argument('id', help='Worker task ID or native OpenCode session ID')
    s.add_argument('--limit', type=int, default=20, help='Latest message count, 1-100; default 20')
    s.add_argument('--before', help='Read older messages using next_before from the previous page')
    s.add_argument('--full', action='store_true', help='Explicitly read the entire available conversation')
    s.add_argument('--saved', action='store_true', help='Read retained worker evidence instead of live OpenCode')
    s.add_argument('--output', help='Export to a new private JSON file and return only its location')
    s = sub.add_parser('cancel'); s.add_argument('id')
    s.add_argument('--reason', required=True,
                   choices=['user_requested', 'superseded', 'wrong_scope', 'duplicate',
                            'side_effect_risk'],
                   help='Concrete cancellation cause; elapsed time or lack of a diff is not a cause')
    s = sub.add_parser('wait'); s.add_argument('id')
    s.add_argument('--seconds', type=int,
                   help='Observation window; defaults by tier to fast=300, normal=1800, deep=3600, '
                        'ultra=3600; minimum 60')
    s = sub.add_parser('kimi', help='Official Kimi account quota: status, device login and explicit binding')
    ksub = s.add_subparsers(dest='action', required=True)
    for action in ('status', 'start', 'login-status', 'usage'):
        ksub.add_parser(action)
    kl = ksub.add_parser('login'); kl.add_argument('--region', choices=['mainland-cn', 'global'], default='mainland-cn')
    kb = ksub.add_parser('bind', help='Only after the user confirms this account owns the inference key')
    kb.add_argument('--confirm', action='store_true',
                    help='Explicit confirmation that the logged-in account is the inference-key account')
    s = sub.add_parser('quota'); s.add_argument('--refresh', action='store_true')
    s.add_argument('--compact', action='store_true', help='Return the stable tier-classification contract without routing telemetry')
    s.add_argument('--tier-guidance', action='store_true',
                   help='Include detailed operator-only capacity diagnostics')
    s.add_argument('--retry-provider', metavar='PROVIDER',
                   help='Explicit local authorization to allow new attempts on a billing-blocked provider '
                        'until a further billing error re-blocks it; manual retry authorization, not proof '
                        'of recovery')
    s.add_argument('--import-snapshot', metavar='FILE',
                   help='Import one manual console reading for a provider without a balance API. JSON object: '
                        'provider, remaining, limit, resets_at, captured_at (optional), source (optional), '
                        'fresh_for_minutes (optional)')
    s = sub.add_parser('usage-report', help='Observed per-model tokens and estimated Credits by window')
    s.add_argument('--windows', default='1,24,48', help='Comma-separated rolling window hours (default 1,24,48)')
    s.add_argument('--output', help='Write the report to a private JSON file and return only its location')
    s = sub.add_parser('integrate'); s.add_argument('id'); s.add_argument('--apply', action='store_true')
    for name in ('doctor', 'daemon', 'stats'):
        sub.add_parser(name)
    s = sub.add_parser('service'); s.add_argument('action', choices=['start', 'stop'])
    s = sub.add_parser('opencode', help='Inspect or update the managed OpenCode runtime')
    s.add_argument('action', choices=['version', 'check', 'update'])
    s.add_argument('--yes', action='store_true')
    s = sub.add_parser('memory', help='Manage persistent OpenCode project memory')
    msub = s.add_subparsers(dest='action', required=True)
    for action in ('status', 'projects', 'profile', 'stats', 'maintain', 'migration'):
        msub.add_parser(action)
    ml = msub.add_parser('list'); ml.add_argument('--directory'); ml.add_argument('--page', type=int, default=1); ml.add_argument('--limit', type=int, default=50)
    ms = msub.add_parser('search'); ms.add_argument('query'); ms.add_argument('--directory'); ms.add_argument('--page', type=int, default=1); ms.add_argument('--limit', type=int, default=20)
    ma = msub.add_parser('add'); ma.add_argument('content'); ma.add_argument('--directory', required=True); ma.add_argument('--tag', action='append', default=[]); ma.add_argument('--type')
    mu = msub.add_parser('update'); mu.add_argument('id'); mu.add_argument('content')
    md = msub.add_parser('delete'); md.add_argument('ids', nargs='+'); md.add_argument('--yes', action='store_true')
    mp = msub.add_parser('pin'); mp.add_argument('id'); mp.add_argument('--off', action='store_true')
    mm = msub.add_parser('migrate', help='Back up and import legacy opencode-mem records into OpenViking')
    mm.add_argument('--plan', action='store_true'); mm.add_argument('--backup', action='store_true')
    mm.add_argument('--apply', action='store_true'); mm.add_argument('--verify', action='store_true')
    mm.add_argument('--batch', type=int, default=50); mm.add_argument('--limit', type=int)
    mm.add_argument('--offline', action='store_true', help='Read the stopped legacy SQLite store for the final capture delta')
    mc = msub.add_parser('cutover', help='Switch capture and management between memory backends safely')
    mc.add_argument('--apply', action='store_true'); mc.add_argument('--rollback', action='store_true')
    mc.add_argument('--skip-delta', action='store_true')
    mc.add_argument('--offline', action='store_true', help='Read the stopped legacy SQLite store')
    s = sub.add_parser('progress', help='Report or inspect a long-running operation')
    psub = s.add_subparsers(dest='action', required=True)
    pr = psub.add_parser('report'); pr.add_argument('id'); pr.add_argument('--phase', required=True); pr.add_argument('--message', default=''); pr.add_argument('--current', type=float); pr.add_argument('--total', type=float); pr.add_argument('--percent', type=float); pr.add_argument('--eta-seconds', type=float); pr.add_argument('--status', choices=['running','queued','completed','failed','cancelled'], default='running'); pr.add_argument('--github-run')
    ps = psub.add_parser('status'); ps.add_argument('id')
    s = sub.add_parser('setup', help='Interactive bilingual setup wizard for this installation (terminal only)')
    s.add_argument('--lang', choices=['en', 'zh-CN'], help='Wizard language (default: auto-detect from locale)')
    s = sub.add_parser('console'); s.add_argument('--open', action='store_true')
    s = sub.add_parser('notify', help='Send one operator-requested Bark message to configured clients')
    s.add_argument('--title', required=True); s.add_argument('--body', required=True)
    s.add_argument('--level', choices=['passive', 'active', 'timeSensitive'], default='active')
    s = sub.add_parser('steer'); s.add_argument('id'); s.add_argument('text'); s.add_argument('--request-id')
    s = sub.add_parser('sessions'); s.add_argument('--search', default=''); s.add_argument('--directory'); s.add_argument('--archived', action='store_true')
    s = sub.add_parser('session'); s.add_argument('action', choices=['rename','archive','restore','fork','delete','bind']); s.add_argument('id'); s.add_argument('--title'); s.add_argument('--directory'); s.add_argument('--yes', action='store_true')
    s = sub.add_parser('task', help='Delete bridge tasks or retier one running task')
    tsub = s.add_subparsers(dest='action', required=True)
    td = tsub.add_parser('delete'); td.add_argument('ids', nargs='+'); td.add_argument('--yes', action='store_true')
    tc = tsub.add_parser('clear-completed'); tc.add_argument('--yes', action='store_true')
    tf = tsub.add_parser('clear-finished'); tf.add_argument('--yes', action='store_true')
    tr = tsub.add_parser('retier', help='Change the capability tier of the same task at a safe native boundary')
    tr.add_argument('id')
    tr.add_argument('--tier', required=True, choices=['fast', 'normal', 'deep', 'ultra'])
    tr.add_argument('--reason', required=True,
                    help='Why the tier changes now: Normal is the default for ordinary executable '
                         'work; Fast needs clear direction, constraints and acceptance plus a '
                         'reasonable expectation that faster model work materially advances a '
                         'concrete near-term milestone (an actually ready blocked consumer or an '
                         'explicit urgent delivery/deadline; a mere sequential dependency or '
                         'resource ownership is not enough); Deep needs high uncertainty; Ultra is '
                         'reserved for extreme-complexity work where a wrong architecture or a '
                         'missed constraint dominates, or after an ordinary task made no '
                         'substantive progress in two consecutive rounds at a checkpoint. Ultra '
                         'keeps its tier; on confirmed capacity exhaustion it may continue one '
                         'level down on the Deep primary and returns to Ultra at a safe idle '
                         'checkpoint, never below Deep. An explicit reason is required')
    tr.add_argument('--handoff-file', required=True,
                    help='Absolute path to a private owner-only JSON checkpoint: '
                         'done, remaining, decisions, evidence, do_not_repeat (text or arrays). '
                         'remaining must explain the concrete remaining work')
    tr.add_argument('--request-id', required=True, dest='request_id',
                    help='Stable request ID; the same ID with the same contents never replays the continuation')
    s = sub.add_parser('cleanup'); s.add_argument('--apply', action='store_true'); s.add_argument('--force', action='store_true')
    s = sub.add_parser('credential', help='Metadata-only local credential references and a redacting runner')
    csub = s.add_subparsers(dest='action', required=True)
    cr = csub.add_parser('register'); cr.add_argument('name')
    cgroup = cr.add_mutually_exclusive_group(required=True)
    cgroup.add_argument('--file'); cgroup.add_argument('--env')
    csub.add_parser('list')
    cm = csub.add_parser('remove'); cm.add_argument('name')
    crun = csub.add_parser('run')
    crun.add_argument('--use', action='append', default=[])
    crun.add_argument('--timeout', type=float, default=60)
    crun.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.cmd == 'setup':
        # Handled before init(): a non-TTY setup must not create state dirs.
        result = setup_wizard_command(args.lang)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get('exit_code', 0)
    init()
    if args.cmd == 'credential':
        import credentials
        return credentials.command(args)
    if args.cmd == 'daemon':
        return daemon()
    if args.cmd in ('submit', 'wait', 'cancel', 'console', 'steer', 'sessions', 'session', 'task', 'cleanup') or (args.cmd == 'transcript' and not args.saved):
        from service import start
        start()
    if args.cmd == 'steer':
        import steering
        result = steering.send(args.id, args.text, args.request_id)
    elif args.cmd == 'sessions':
        import management
        result = management.sessions({'search': args.search, 'directory': args.directory, 'archived': args.archived})
    elif args.cmd == 'session':
        import management
        body = {'action': args.action, 'title': args.title, 'directory': args.directory}
        if args.action == 'delete':
            if not args.yes:
                raise ValueError('Use --yes only for a user-authorized permanent deletion')
            raw = management._fetch_session(management._validate_session_id(args.id))
            body.update(confirm_session_id=args.id, confirm_title=raw['title'])
        result = management.update_session(args.id, body)
    elif args.cmd == 'task':
        if args.action == 'retier':
            import retier
            # The handoff file is read privately here; only the validated
            # normalized checkpoint crosses into the backend, and no response
            # or log line echoes its path or contents.
            result = retier.send(args.id, args.tier, args.reason,
                                 retier.read_handoff_file(args.handoff_file), args.request_id)
        else:
            if not args.yes:
                raise ValueError('Use --yes only for a user-authorized permanent task deletion')
            import management
            action = {'delete': 'delete', 'clear-completed': 'clear_completed',
                      'clear-finished': 'clear_finished'}[args.action]
            body = {
                'action': action,
                # One confirmed task deletion owns the complete bridge lifecycle:
                # compact usage is retained, while the linked native session,
                # evidence and any managed terminal worktrees are removed together.
                'close_uncertain': True,
                'discard_cancelled_worktrees': True,
                'discard_unintegrated_worktrees': True,
            }
            if args.action == 'delete':
                body['ids'] = args.ids
            result = management.delete_tasks(body)
    elif args.cmd == 'cleanup':
        import cleanup
        result = cleanup.run(apply=args.apply, force=args.force)
    elif args.cmd == 'notify':
        import notifications
        result = notifications.send(args.title, args.body, level=args.level)
    elif args.cmd == 'submit':
        if args.spec is not None or args.spec_urlencoded is not None:
            spec = load_submit_spec(args)
        else:
            spec = {key: value for key, value in vars(args).items() if value is not None}
            spec.update(scopes=args.scope, commands=args.command, resources=args.resource,
                        targets=args.target)
        result = submit(spec)
    elif args.cmd == 'status':
        if args.id:
            result = task_status(task(args.id))
        else:
            result = [task_status(t) for t in tasks()]
    elif args.cmd == 'transcript':
        import transcript
        result = transcript.read(args.id, args.limit, args.before, args.full, args.saved)
        if args.output:
            result = transcript.export(result, args.output)
    elif args.cmd == 'collect':
        result = diagnostics.collect(args.id, args.full)
    elif args.cmd == 'cancel':
        result = coordinator_task(request_cancel(args.id, args.reason, source='cli'))
    elif args.cmd == 'wait':
        result = wait_for_task(args.id, args.seconds)
    elif args.cmd == 'kimi':
        import management
        result = management.kimi_account_command(
            args.action, region=getattr(args, 'region', None),
            confirmed=getattr(args, 'confirm', False))
    elif args.cmd == 'quota':
        if args.retry_provider:
            result = quota.retry_provider(args.retry_provider)
        elif args.import_snapshot:
            payload = json.loads(Path(args.import_snapshot).read_text())
            if not isinstance(payload, dict):
                raise ValueError('Snapshot file must contain one JSON object')
            provider = payload.pop('provider', None)
            result = quota.import_snapshot(provider, payload)
        else:
            snapshot = quota.refresh(force=args.refresh)
            guidance = quota.tier_guidance(config(), snapshot) if args.tier_guidance or args.compact else None
            result = quota.compact_guidance(guidance) if args.compact else (
                {'providers': snapshot, 'tier_guidance': guidance} if args.tier_guidance else snapshot)
    elif args.cmd == 'usage-report':
        import analytics
        windows = []
        for part in str(args.windows).split(','):
            part = part.strip()
            if not part:
                continue
            try:
                windows.append(float(part))
            except ValueError:
                raise ValueError('--windows must be comma-separated numbers of hours')
        report = analytics.usage_report(window_hours=tuple(windows) or (1, 24, 48))
        if args.output:
            target = Path(args.output).expanduser()
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            os.chmod(str(target), 0o600)
            result = {'report': str(target), 'windows': [w['hours'] for w in report['windows']]}
        else:
            result = report
    elif args.cmd == 'integrate':
        with locked():
            t = task(args.id)
            integration = dict(t, workspace='shared')
            if any(a['status'] in ACTIVE and conflicts(integration, a) for a in tasks() if a['id'] != t['id']):
                raise ValueError('Integration conflicts with an active task')
            result = integrate(t, args.apply)
            if args.apply:
                t.update(integrated_at=time.time())
                write_json(task_path(t['id']), t)
    elif args.cmd == 'doctor':
        result = doctor()
    elif args.cmd == 'service':
        import service
        if args.action == 'start':
            result = service.start()
        elif args.action == 'restart':
            # Idle-safe restart: refuses while tasks or sessions are active, so a
            # backend or plugin change never interrupts running work.
            result = service.reload_runtime()
        else:
            result = service.stop()
    elif args.cmd == 'opencode':
        import opencode_update
        if args.action == 'version':
            result = opencode_update.status()
        elif args.action == 'check':
            result = opencode_update.run(check_only=True, force_check=True)
        else:
            if not args.yes:
                raise ValueError('Use --yes to install and activate an available OpenCode update')
            result = opencode_update.run(check_only=False, force_check=True)
    elif args.cmd == 'memory':
        import memory_manager
        if args.action == 'status': result = memory_manager.status()
        elif args.action == 'projects': result = memory_manager.projects()
        elif args.action == 'profile': result = memory_manager.profile()
        elif args.action == 'stats': result = memory_manager.stats()
        elif args.action == 'maintain': result = memory_manager.maintain(force=True)
        elif args.action == 'list': result = memory_manager.list_memories(args.directory, args.page, args.limit)
        elif args.action == 'search': result = memory_manager.search(args.query, args.directory, args.page, args.limit)
        elif args.action == 'add': result = memory_manager.add(args.content, args.directory, args.tag, args.type)
        elif args.action == 'update': result = memory_manager.update(args.id, args.content)
        elif args.action == 'pin': result = memory_manager.pin(args.id, not args.off)
        elif args.action == 'migration':
            import openviking_migrate
            result = openviking_migrate.status()
        elif args.action == 'cutover':
            import openviking_migrate
            result = openviking_migrate.cutover(apply=args.apply, rollback=args.rollback,
                                                skip_delta=args.skip_delta, offline=args.offline)
        elif args.action == 'migrate':
            if not (args.plan or args.apply or args.verify or args.backup):
                raise ValueError('Choose --plan, --backup, --apply and/or --verify')
            import openviking_migrate
            import openviking_service
            result = {}
            if args.backup or (args.apply and not openviking_migrate.latest_backup()):
                result['backup'] = openviking_migrate.backup()
            if args.apply:
                if not openviking_service.probe()['ready']:
                    raise ValueError('OpenViking is not ready; start the service before importing')
                result['run'] = openviking_migrate.run(limit=args.limit, batch=args.batch, offline=args.offline)
            elif args.plan:
                plan = openviking_migrate.plan(offline=args.offline)
                plan.pop('items', None)
                result['plan'] = plan
            if args.verify:
                result['verify'] = openviking_migrate.verify(offline=args.offline)
        else:
            if not args.yes: raise ValueError('Use --yes for permanent memory deletion')
            result = memory_manager.delete(args.ids[0]) if len(args.ids) == 1 else memory_manager.bulk_delete(args.ids)
    elif args.cmd == 'progress':
        import progress
        result = progress.snapshot(args.id) if args.action == 'status' else progress.report(
            args.id, args.phase, args.message, args.current, args.total, args.percent,
            args.eta_seconds, args.status, args.github_run)
    elif args.cmd == 'console':
        url = config()['console_url'] + '/console'
        if args.open:
            import webbrowser
            webbrowser.open(url)
        result = {'url': url}
    else:
        result = stats()
    print(json.dumps(redact(result), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        code = main()
        sys.exit(code if isinstance(code, int) else 0)
    except Exception as e:
        print(json.dumps(diagnostics.exception(e, 'cli'), ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
