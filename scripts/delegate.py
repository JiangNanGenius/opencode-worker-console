#!/usr/bin/env python3
"""CLI and durable queue daemon for the general-purpose OpenCode worker pool."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import statistics
import sys
import threading
import time
import uuid
from common import (ACTIVE, CONFIG, STATE, TERMINAL, api, artifact_dir, config, init, locked,
                    public_task, read_json, redact, task, task_path, tasks, update, write_json)
import quota
import diagnostics
from workspace import conflicts, git_root, integrate, relative_scope
from worker import run_task


def owner_key(t):
    """Immutable scheduling owner: the Codex conversation, with legacy fallbacks."""
    return t.get('owner_thread_id') or t.get('group_id') or t.get('source_dir')


def per_owner_cap(c):
    cap = c.get('max_parallel_per_owner', 4)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 16:
        return 4
    return cap


def live_recovery(t):
    """Fresh guidance for queued blockages and billing-failed tasks.

    Live derivation replaces the persisted snapshot; when routing is healthy again the
    result is None so stale recovery never lingers. The snapshot is kept only when live
    derivation itself is unavailable.
    """
    if t.get('status') not in ('queued', 'failed', 'needs_attention'):
        return None
    try:
        return quota.guidance(t, config(), quota.view(read_json(STATE / 'quota.json', {})))
    except Exception:
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
    result.update(
        terminal=terminal,
        continue_waiting=not terminal and not blocked,
        waited_seconds=max(0, round(waited_seconds, 3)),
        next_action=(
            'reselect_profile_and_resubmit' if actionable and alternatives else
            'top_up_or_authorize_manual_retry' if actionable and monthly else
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


def task_status(t):
    """Expose current recovery options without retaining an obsolete snapshot."""
    result = public_task(t)
    result.pop('recovery', None)
    recovery = live_recovery(t) if t.get('id') else None
    if recovery:
        result['recovery'] = recovery
    return result


def submit(spec):
    c = config()
    root = Path(spec['directory']).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('Task directory does not exist')
    if not spec.get('objective', '').strip():
        raise ValueError('A non-empty objective is required')
    if spec.get('profile', 'auto') not in {'auto', *c['profiles']}:
        raise ValueError('Unknown profile')
    if spec.get('profile', 'auto') != 'auto' and c['profiles'][spec['profile']].get('enabled') is False:
        raise ValueError('Profile is disabled')
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
        # Isolation partitions local paths; target-only work has no local scope to isolate.
        workspace = 'isolated' if mode == 'write' and scopes and (spec.get('large') or '.' in scopes) else 'shared'
    if workspace not in ('shared', 'isolated'):
        raise ValueError('Workspace must be auto, shared or isolated')
    if workspace == 'isolated' and git_root(root) != root:
        raise ValueError('For isolated work, specify the Git repository root')
    if spec.get('urgency', 'background') not in ('fast', 'background'):
        raise ValueError('Urgency must be fast or background')
    if spec.get('complexity', 'normal') not in ('normal', 'deep'):
        raise ValueError('Complexity must be normal or deep')
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
        'requested_profile': spec.get('profile', 'auto'), 'mode': mode,
        'urgency': spec.get('urgency', 'background'), 'complexity': spec.get('complexity', 'normal'),
        'workspace': workspace, 'source_dir': str(root), 'scopes': scopes, 'targets': targets,
        'commands': spec.get('commands', []), 'resources': spec.get('resources', []),
        'web': bool(spec.get('web', False)),
        'status': 'queued', 'created_at': time.time(), 'updated_at': time.time(),
        'owner_thread_id': os.environ.get('CODEX_THREAD_ID'),
        'group_id': spec.get('group_id') or os.environ.get('CODEX_THREAD_ID') or str(root),
        'group_title': spec.get('group_title') or root.name,
        'parent_task_id': spec.get('parent_task_id'),
    }
    if t['parent_task_id']:
        parent = task(t['parent_task_id'])
        if parent.get('group_id') != t['group_id']:
            raise ValueError('Parent task must belong to the same group')
    # Block accidental delegation of known stored API keys before persisting prompt text.
    if redact(t) != t:
        raise ValueError('Task contains a credential-like value; remove it before submission')
    with locked():
        if (STATE / 'maintenance.json').exists():
            raise ValueError('Configuration update in progress; retry shortly')
        write_json(task_path(t['id']), t)
    return public_task(t)


def choose_ready(all_tasks, c, q):
    """Select dispatchable queued tasks.

    No global or per-provider concurrency caps: independent Codex conversations run
    freely. Only the per-owner cap, true quota/billing blocks, and scope/resource
    overlap locks constrain dispatch. Profiles are pinned; billing/quota blockages are
    reported, never silently re-routed.
    """
    if (STATE / 'maintenance.json').exists():
        return []
    active = [t for t in all_tasks if t['status'] in ACTIVE]
    choices = []
    waiting = [t for t in all_tasks if t['status'] == 'queued']
    cap = per_owner_cap(c)
    for t in waiting:
        if t.get('cancel_requested'):
            choices.append((t, None, 'cancelled'))
            continue
        if sum(1 for a in active if owner_key(a) == owner_key(t)) >= cap:
            choices.append((t, None, 'owner_at_capacity'))
            continue
        profile, why = quota.route(t, c, q)
        if profile is None:
            choices.append((t, None, why))
            continue
        if any(conflicts(t, a) for a in active):
            choices.append((t, None, 'scope_or_resource_in_use'))
            continue
        selected = dict(t, profile=profile, status='starting')
        active.append(selected)
        choices.append((t, profile, why))
    return choices


def daemon():
    init()
    lock = open(STATE / 'daemon.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Per-job threads: no hidden executor cap across independent owners.
    threads = {}
    cleanup_thread = None
    last_refresh = 0
    last_cleanup = 0
    q = quota.view(read_json(STATE / 'quota.json', {}))
    while not stop.is_set():
        try:
            c = config()
            import cleanup
            cp = cleanup.policy()
            if cp['enabled'] and time.time() - last_cleanup >= cp['interval_seconds'] and \
                    (cleanup_thread is None or not cleanup_thread.is_alive()):
                last_cleanup = time.time()
                cleanup_thread = threading.Thread(target=cleanup.run, kwargs={'apply': True},
                                                  daemon=True, name='cleanup')
                cleanup_thread.start()
            all_tasks = tasks()
            just_finished = any(not th.is_alive() for th in threads.values())
            needs_poll = any(t['status'] in ACTIVE | {'queued'} for t in all_tasks)
            has_queue = any(t['status'] == 'queued' for t in all_tasks)
            if just_finished or (needs_poll and time.time() - last_refresh >= (60 if has_queue else 300)):
                q, last_refresh = quota.refresh(), time.time()
            threads = {k: th for k, th in threads.items() if th.is_alive()}
            with locked():
                for t, profile, reason in choose_ready(tasks(), c, q):
                    if reason == 'cancelled':
                        t.update(status='cancelled', finished_at=time.time())
                    elif profile:
                        t.update(profile=profile, status='starting', route_reason=reason,
                                 started_at=time.time(), queue_reason=None, recovery=None)
                    else:
                        t['queue_reason'] = reason
                        t['recovery'] = quota.guidance(t, c, q)
                    write_json(task_path(t['id']), t)
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('submit')
    s.add_argument('--spec', help='JSON task specification; use - for stdin')
    s.add_argument('--directory', default=os.getcwd())
    s.add_argument('--profile', default='auto')
    s.add_argument('--mode', choices=['read', 'write'], default='read')
    s.add_argument('--urgency', choices=['fast', 'background'], default='background')
    s.add_argument('--complexity', choices=['normal', 'deep'], default='normal')
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
    s = sub.add_parser('wait'); s.add_argument('id'); s.add_argument('--seconds', type=int, default=20)
    s = sub.add_parser('quota'); s.add_argument('--refresh', action='store_true')
    s.add_argument('--retry-provider', metavar='PROVIDER',
                   help='Explicit local authorization to allow new attempts on a billing-blocked provider '
                        'until a further billing error re-blocks it; manual retry authorization, not proof '
                        'of recovery')
    s = sub.add_parser('integrate'); s.add_argument('id'); s.add_argument('--apply', action='store_true')
    for name in ('doctor', 'daemon', 'stats'):
        sub.add_parser(name)
    s = sub.add_parser('service'); s.add_argument('action', choices=['start', 'stop'])
    s = sub.add_parser('console'); s.add_argument('--open', action='store_true')
    s = sub.add_parser('steer'); s.add_argument('id'); s.add_argument('text'); s.add_argument('--request-id')
    s = sub.add_parser('sessions'); s.add_argument('--search', default=''); s.add_argument('--directory'); s.add_argument('--archived', action='store_true')
    s = sub.add_parser('session'); s.add_argument('action', choices=['rename','archive','restore','fork','delete','bind']); s.add_argument('id'); s.add_argument('--title'); s.add_argument('--directory'); s.add_argument('--yes', action='store_true')
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
    init()
    if args.cmd == 'credential':
        import credentials
        return credentials.command(args)
    if args.cmd == 'daemon':
        return daemon()
    if args.cmd in ('submit', 'wait', 'cancel', 'console', 'steer', 'sessions', 'session', 'cleanup') or (args.cmd == 'transcript' and not args.saved):
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
    elif args.cmd == 'cleanup':
        import cleanup
        result = cleanup.run(apply=args.apply, force=args.force)
    elif args.cmd == 'submit':
        if args.spec:
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
        else:
            spec = vars(args).copy()
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
        t = task(args.id)
        result = public_task(t if t['status'] in TERMINAL else update(args.id, cancel_requested=True))
    elif args.cmd == 'wait':
        began = time.time()
        end = time.time() + max(0, min(args.seconds, 60))
        while task(args.id)['status'] not in TERMINAL and time.time() < end:
            time.sleep(1)
        result = wait_result(task(args.id), time.time() - began)
    elif args.cmd == 'quota':
        result = quota.retry_provider(args.retry_provider) if args.retry_provider \
            else quota.refresh(force=args.refresh)
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
        result = service.start() if args.action == 'start' else service.stop()
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
