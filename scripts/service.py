"""Start services from the calling app's authorized macOS context, without launchd TCC drift."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import ACTIVE, STATE, api, config, init, locked, read_json, request_cancel, tasks, write_json


def identity(pid):
    p = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def alive(entry):
    return bool(entry and entry.get('identity') and identity(entry['pid']) == entry['identity'])


def start():
    init()
    with locked('service'):
        from bootstrap import ensure_opencode
        from common import CONFIG
        c = config()
        # Validate the optional Alibaba credential pool before any process is
        # launched. An invalid private setting must fail before the old runtime
        # is stopped or a partial stack is started.
        import bailian_pool
        try:
            pool_settings = bailian_pool.pool_settings(c)
        except ValueError as exc:
            raise RuntimeError('Invalid bailian_pool configuration: ' + str(exc)) from None
        # The native Kimi gate is validated before any process is launched so an
        # invalid private setting cannot leave a partial stack running.
        try:
            import kimi_gate
        except ImportError:
            kimi_gate = None
        gate_settings = None
        if kimi_gate is not None:
            try:
                gate_settings = kimi_gate.settings(c)
            except ValueError as exc:
                raise RuntimeError('Invalid kimi_gate configuration: ' + str(exc)) from None
        # Resolve both before launching anything: the console can call start()
        # from its isolated 3.12 venv during host controls/settings restarts.
        from console_runtime import interpreters
        runtime = interpreters(c, STATE)
        if c.get('memory', {}).get('enabled') is True:
            import memory_manager
            memory_manager.configure()
            if memory_manager.backend() == 'openviking':
                import openviking_service
                openviking_service.start()
        binary = ensure_opencode(c.get('opencode_binary'))
        if binary != c.get('opencode_binary'):
            c['opencode_binary'] = binary
            write_json(CONFIG, c)
        records = read_json(STATE / 'services.json', {})
        maintenance = read_json(STATE / 'maintenance.json', {}) or {}
        paused = maintenance.get('paused_services', []) if isinstance(maintenance, dict) else []
        root = Path(__file__).resolve().parent
        entries = []
        if pool_settings:
            # Start the loopback credential pool before OpenCode so the runtime
            # provider is reachable as soon as the server becomes healthy.
            entries.append(('bailian_pool', 'bailian_pool.py', []))
        if gate_settings:
            # The Kimi gate must listen before OpenCode can issue inference.
            entries.append(('kimi_gate', 'kimi_gate.py', []))
        entries.extend([('server', 'server.py', []), ('pool', 'delegate.py', ['daemon']),
                        ('console', 'console.py', [])])
        for name, script, args in entries:
            # Other upstream callers may auto-start services during a cutover.
            # Honor the operator's explicit pause until checkpoints are resumed.
            if name in paused:
                continue
            if name == 'console' and 'console_url' not in config():
                continue
            if alive(records.get(name)):
                continue
            logpath = STATE / 'logs' / (name + '.log')
            if logpath.exists() and logpath.stat().st_size > 5 * 1024 * 1024:
                logpath.replace(logpath.with_suffix('.previous.log'))
            with logpath.open('ab') as log:
                # A persistent console is not owned by the upstream-harness conversation that
                # happened to start it. CLI submissions retain their caller's owner;
                # manual console submissions use their group/workspace fallback.
                service_env = dict(os.environ)
                service_env.pop('CODEX_THREAD_ID', None)
                python = runtime['console_python' if name == 'console' else 'worker_python']
                proc = subprocess.Popen([python, str(root / script), *args],
                                        cwd=str(STATE), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        start_new_session=True, close_fds=True, env=service_env)
            records[name] = {'pid': proc.pid, 'identity': identity(proc.pid)}
            write_json(STATE / 'services.json', records)
            if name == 'bailian_pool':
                try:
                    password = (STATE / 'server-password').read_text().strip()
                except OSError:
                    password = ''
                ready = bool(password) and bailian_pool.wait_ready(
                    pool_settings['port'], password, timeout=10)
                if ready and hasattr(proc, 'poll') and proc.poll() is not None:
                    ready = False
                if not ready:
                    try:
                        os.kill(proc.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    records.pop(name, None)
                    write_json(STATE / 'services.json', records)
                    raise RuntimeError('Alibaba credential pool did not become ready; '
                                       'inspect private service logs')
            if name == 'kimi_gate':
                try:
                    password = (STATE / 'server-password').read_text().strip()
                except OSError:
                    password = ''
                ready = bool(password) and kimi_gate.wait_ready(
                    gate_settings['port'], password, timeout=10)
                if ready and hasattr(proc, 'poll') and proc.poll() is not None:
                    ready = False
                if not ready:
                    try:
                        os.kill(proc.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    records.pop(name, None)
                    write_json(STATE / 'services.json', records)
                    raise RuntimeError('Kimi gate did not become ready; inspect private service logs')
            if name == 'server':
                for _ in range(40):
                    try:
                        if api('/global/health', timeout=1).get('healthy'):
                            break
                    except Exception:
                        time.sleep(0.25)
                else:
                    raise RuntimeError('OpenCode did not become healthy; inspect private service logs')
                import management
                management.sync_worker_permissions()
        return {'services': records, 'startup': 'on demand from calling app; durable task recovery'}


def stop():
    # Abort and confirm idle before stopping a process that owns writable scopes.
    with locked('service'):
        for t in tasks():
            if t['status'] in ACTIVE and t.get('session_id'):
                from worker import stop as stop_worker
                if not stop_worker(t):
                    raise RuntimeError('Worker abort not confirmed; service and ownership retained')
                request_cancel(t['id'], 'service_stop', source='service')
        records = read_json(STATE / 'services.json', {})
        for name in ('console', 'pool', 'server', 'bailian_pool', 'kimi_gate'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(60):
                    if not alive(entry):
                        break
                    time.sleep(0.25)
                if alive(entry):
                    # Managed OpenCode is still disposing; keep the memory
                    # service alive so plugin commits are not lost.
                    raise RuntimeError(name + ' is still stopping; retry service status later')
        # Order matters: the OpenCode plugin commits and captures during its own
        # disposal, so OpenViking is drained and only then stopped. A failed
        # managed stop above raises before this point, preserving the service.
        if config().get('memory', {}).get('backend') == 'openviking':
            import openviking_service
            if openviking_service.running_record():
                drained = openviking_service.drain(timeout=120)
                if drained.get('settled') is False:
                    raise RuntimeError('OpenViking still has in-flight commits; '
                                       'retry service stop after they settle')
                openviking_service.stop()
        return {'services': 'stopped', 'artifacts': 'retained'}


def reload_runtime(allow_queued=False, maintenance_held=False):
    """Restart idle execution processes while keeping the browser gateway alive.

    Queued work normally blocks the restart. ``allow_queued`` is only accepted
    together with an active ``maintenance_held`` barrier: the caller has already
    stopped new admissions, so queued jobs stay queued and are preserved while
    the restart happens between jobs. Executing or uncertain tasks always block.
    """
    with locked('service'):
        all_tasks = tasks()
        active = [t for t in all_tasks if t['status'] in ACTIVE]
        queued = [t for t in all_tasks if t['status'] == 'queued']
        if active:
            raise ValueError('Tasks are running; apply settings when idle')
        if queued and not allow_queued:
            raise ValueError('Tasks are queued or running; apply settings when idle')
        if allow_queued and (not maintenance_held or not (STATE / 'maintenance.json').exists()):
            raise ValueError('Queued cutover requires an active maintenance barrier')
        import management
        if management._sessions_busy():
            raise ValueError('OpenCode sessions are running; apply settings when idle')
        # Validate before the stop phase: a typo in the optional pool settings
        # must not leave a previously healthy stack shut down.
        import bailian_pool
        try:
            bailian_pool.pool_settings(config())
        except ValueError as exc:
            raise ValueError('Invalid bailian_pool configuration: ' + str(exc)) from None
        try:
            import kimi_gate
            kimi_gate.settings(config())
        except ImportError:
            pass
        except ValueError as exc:
            raise ValueError('Invalid kimi_gate configuration: ' + str(exc)) from None
        records = read_json(STATE / 'services.json', {})
        for name in ('pool', 'server', 'bailian_pool', 'kimi_gate'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(60):
                    if not alive(entry):
                        break
                    time.sleep(0.25)
                if alive(entry):
                    raise RuntimeError(name + ' did not stop')
    result = start()
    c = config()
    c['restart_required'] = False
    from common import CONFIG
    write_json(CONFIG, c)
    return result


def stop_consumers():
    """Pause observers/UI for a compatible update; leave OpenCode execution intact."""
    with locked('service'):
        records = read_json(STATE / 'services.json', {})
        for name in ('pool', 'console'):
            entry = records.get(name)
            if alive(entry):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(150):
                    if not alive(entry): break
                    time.sleep(0.1)
                if alive(entry): raise RuntimeError(name + ' did not stop for live update')
    return {'execution_preserved': True}
