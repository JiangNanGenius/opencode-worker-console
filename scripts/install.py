#!/usr/bin/env python3
"""Generic POSIX installer (macOS and Linux) for the delegate-opencode worker pool.

Windows is explicitly unsupported because the runtime relies on POSIX fcntl locks.
Existing installations keep their config, state, skill and all evidence.
"""
import argparse
import json
import re
import tempfile
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse

from providers import ARK_PROVIDER

ROOT = Path(__file__).resolve().parent.parent
INSTALL = Path(os.environ.get('DELEGATE_INSTALL', '~/.codex/skills/delegate-opencode')).expanduser()
STATE = Path(os.environ.get('DELEGATE_STATE', '~/.local/state/delegate-opencode')).expanduser()
CONFIG = Path(os.environ.get('DELEGATE_CONFIG', '~/.config/opencode/delegate-pool.json')).expanduser()

# Guides ship with the runtime so installed references linking ../README.md stay
# valid; missing optional items are skipped by copy_package.
PACKAGE_ITEMS = ['SKILL.md', 'README.md', 'README.zh-CN.md', 'LICENSE',
                 'SECURITY.md', 'CONTRIBUTING.md',
                 'agents', 'scripts', 'references', 'web']
# Routing defaults (quota.route): fast -> fallback, normal -> senior-code,
# deep -> deep-research. Profile names must stay aligned with those routes.
PROFILE_NAMES = ['fallback', 'senior-code', 'deep-research']
LEGACY_FALLBACK_PROFILE = 'fast-code'
PRESETS = {
    'deepseek-kimi': {
        'fallback': {'model': 'deepseek/deepseek-flash', 'variant': 'max', 'label': 'Fallback · DeepSeek V4.1 Flash'},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'max', 'label': 'Kimi K2.8 Preview'},
        'deep-research': {'model': 'kimi-for-coding/k3', 'variant': 'max', 'label': 'Kimi K3'},
    },
    # Agent Plan ladder: every fixed Ark model and Auto share one AFP allowance,
    # so the automatic policy load-balances the same-capability pairs by
    # provider quota headroom and falls through Auto before direct DeepSeek.
    # ark-deepseek is intentionally not a default profile: it stays available in
    # provider metadata for manual selection only.
    'ark-agent-plan': {
        'fallback': {'model': 'deepseek/deepseek-flash', 'variant': 'max', 'label': 'Fallback · DeepSeek V4.1 Flash'},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'max', 'label': 'Kimi K2.8 Preview'},
        'deep-research': {'model': 'kimi-for-coding/k3', 'variant': 'max', 'label': 'Kimi K3'},
        'ark-evolving': {'model': ARK_PROVIDER + '/doubao-seed-evolving', 'variant': 'max', 'label': 'Ark Doubao Seed Evolving'},
        'ark-k3': {'model': ARK_PROVIDER + '/kimi-k3', 'variant': 'max', 'label': 'Ark Kimi K3'},
        'ark-auto': {'model': ARK_PROVIDER + '/ark-code-latest', 'variant': 'max', 'label': 'Ark Auto'},
    },
}
# The automatic ladder: K3 pair (2:1) → K2.8/Evolving pair (1:1) → Ark Auto →
# direct DeepSeek. Same-capability pairs are one quota-aware pool each, grouped
# by provider so equal headroom preserves the exact 2:1 and 1:1 baselines and
# only materially imbalanced valid quota temporarily shifts effective shares.
# Auto is its own ordered stage, never mixed into a random pool; direct DeepSeek
# is the final fallback. ark-deepseek stays out of automatic routing (manual only).
DEFAULT_ROUTING_POLICY = {
    'fast': [
        [{'profile': 'ark-auto', 'weight': 1}],
        [{'profile': 'fallback', 'weight': 1}],
    ],
    'background': [
        [{'profile': 'senior-code', 'weight': 1}, {'profile': 'ark-evolving', 'weight': 1}],
        [{'profile': 'ark-auto', 'weight': 1}],
        [{'profile': 'fallback', 'weight': 1}],
    ],
    'deep': [
        [{'profile': 'deep-research', 'weight': 2}, {'profile': 'ark-k3', 'weight': 1}],
        [{'profile': 'senior-code', 'weight': 1}, {'profile': 'ark-evolving', 'weight': 1}],
        [{'profile': 'ark-auto', 'weight': 1}],
        [{'profile': 'fallback', 'weight': 1}],
    ],
}
# Adaptive quota balancing is separate from ordinary weighting. This preset opts
# in only for its two subscription pairs; every other install keeps fixed policy
# weights unless the user explicitly enables a ladder in the console.
DEFAULT_ROUTING_DYNAMICS = {
    'background': {'0': {'ladder': [[2, 1], [1, 1], [1, 2]]}},
    'deep': {
        '0': {'ladder': [[3, 1], [2, 1], [1, 1]]},
        '1': {'ladder': [[2, 1], [1, 1], [1, 2]]},
    },
}
DEFAULT_QUOTA_SPILLOVER = {
    'enabled': True,
    'profile': 'fallback',
    'tiers': ['fast', 'background'],
    'max_share_percent': 30,
}
# Concurrency is capped per owning Codex conversation (owner_thread_id), never
# globally or per provider; the scheduler reads max_parallel_per_owner (default 4).
# The low-weekly guard is independent from ordinary weighted routing. Once the
# authoritative Kimi weekly/overall allowance reaches the threshold, normal work
# leaves Kimi and only a bounded number of native-K3 deep jobs may run at once.
# Confirmed quota errors can continue in the same OpenCode session on the next
# same-tier route, preserving context and partial work without replaying the task.
TOP_LEVEL_DEFAULTS = {
    'max_parallel_per_owner': 4,
    'kimi_reserve_percent': 0,
    'kimi_low_weekly_threshold_percent': 5,
    'kimi_low_weekly_k3_limit': 1,
    'auto_reroute_on_quota_exhaustion': True,
    'auto_approve': True,
}
ACTIVE_STATUSES = {'queued', 'starting', 'running', 'stopping', 'uncertain'}
ENV_VARS = ('DELEGATE_INSTALL', 'DELEGATE_STATE', 'DELEGATE_CONFIG', 'DELEGATE_BIN_DIR')


def fail(message):
    raise SystemExit(message)


def check_platform():
    if sys.platform.startswith('win'):
        fail('Windows is unsupported: the worker runtime depends on POSIX fcntl locking')


def same_path(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def configured_opencode():
    """Valid executable path saved in an existing config, if there is one.

    Reusing it keeps upgrades on the user's chosen binary even when it is not
    on PATH, instead of silently downloading another copy.
    """
    try:
        record = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return None
    value = record.get('opencode_binary') if isinstance(record, dict) else None
    if isinstance(value, str) and value:
        candidate = Path(value).expanduser()
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate.resolve())
    return None


def detect_opencode(explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            fail('--opencode must point to an executable file: ' + explicit)
        return str(candidate.resolve())
    saved = configured_opencode()
    if saved:
        return saved
    found = shutil.which('opencode')
    if not found:
        from bootstrap import ensure_opencode
        found = ensure_opencode()
    return found


def validate_model(spec):
    provider, sep, model = spec.partition('/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[^\s:]+', spec):
        fail('--model must be in provider/model form, e.g. anthropic/claude-sonnet-4')


def reserve_ports(count=2):
    sockets = [socket.socket() for _ in range(count)]
    try:
        ports = []
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
            ports.append(sock.getsockname()[1])
    finally:
        for sock in sockets:
            sock.close()
    if len(set(ports)) != count:
        fail('Could not reserve distinct loopback ports')
    return ports


def reserve_distinct_port(existing):
    for _ in range(50):
        port = reserve_ports(1)[0]
        if port not in existing:
            return port
    fail('Could not reserve a distinct loopback port')


def loopback_url(port):
    return 'http://127.0.0.1:' + str(port)


def taken_ports(c):
    ports = set()
    for key in ('server_url', 'console_url'):
        if c.get(key):
            port = urllib.parse.urlsplit(c[key]).port
            if port is not None:
                ports.add(port)
    return ports


def ensure_urls(c):
    missing = [key for key in ('server_url', 'console_url') if not c.get(key)]
    if not missing:
        return
    reserved = iter(reserve_ports(len(missing))) if len(missing) == 2 else None
    for key in missing:
        if reserved is not None:
            c[key] = loopback_url(next(reserved))
        else:
            c[key] = loopback_url(reserve_distinct_port(taken_ports(c)))


def validate_urls(c):
    parsed = {}
    for key in ('server_url', 'console_url'):
        if key not in c:
            continue
        url = urllib.parse.urlsplit(c[key])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.path or url.query or url.fragment or not url.port:
            fail(key + ' must use plain HTTP on a loopback host')
        parsed[key] = url
    if 'server_url' in parsed and 'console_url' in parsed:
        if (parsed['server_url'].hostname, parsed['server_url'].port) == \
           (parsed['console_url'].hostname, parsed['console_url'].port):
            fail('server_url and console_url must be distinct loopback addresses')


def ensure_network_config(c):
    """Default and validate console bind/origins, preserving any explicit values.

    server_url and console_url stay pinned to loopback; only console_bind may be
    widened for explicit LAN use, and public origins must be explicitly listed.
    """
    import console_auth
    c.setdefault('console_bind', '127.0.0.1')
    c.setdefault('console_trusted_proxies', [])
    origins = c.get('console_allowed_origins')
    if origins is None:
        c['console_allowed_origins'] = [c['console_url']]
    elif not isinstance(origins, list) or not origins:
        fail('console_allowed_origins must be a non-empty list of origins')
    try:
        console_auth.validate_bind(c['console_bind'])
        for origin in c['console_allowed_origins']:
            console_auth.validate_origin(origin)
        console_auth.trusted_proxy_networks(c)
    except ValueError as error:
        fail(str(error))


def default_profiles(model=None, preset=None):
    if preset:
        return {name: dict(spec) for name, spec in PRESETS[preset].items()}
    return {name: {'model': model, 'label': model.split('/', 1)[1]} for name in PROFILE_NAMES}


def migrate_legacy_fallback_profile(c):
    """Rename the old misleading fast-code profile without changing its model.

    Routes and policy members reference profile IDs, so the migration updates them
    atomically with the profile map. A pre-existing fallback profile wins to avoid
    overwriting an operator's configuration.
    """
    profiles = c.get('profiles')
    if not isinstance(profiles, dict) or LEGACY_FALLBACK_PROFILE not in profiles or 'fallback' in profiles:
        return False
    profiles['fallback'] = profiles.pop(LEGACY_FALLBACK_PROFILE)
    if profiles['fallback'].get('label') == 'DeepSeek V4.1 Flash':
        profiles['fallback']['label'] = 'Fallback · DeepSeek V4.1 Flash'
    routing = c.get('routing')
    if isinstance(routing, dict):
        for tier, profile in list(routing.items()):
            if profile == LEGACY_FALLBACK_PROFILE:
                routing[tier] = 'fallback'
    policy = c.get('routing_policy')
    if isinstance(policy, dict):
        for stages in policy.values():
            if not isinstance(stages, list):
                continue
            for stage in stages:
                if not isinstance(stage, list):
                    continue
                for member in stage:
                    if isinstance(member, dict) and member.get('profile') == LEGACY_FALLBACK_PROFILE:
                        member['profile'] = 'fallback'
    return True


def refuse_active_tasks():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    for path in sorted((STATE / 'tasks').glob('*.json')):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get('status') in ACTIVE_STATUSES:
            fail('Active workers exist; collect or cancel them before updating the deployment')


def ensure_password():
    password = STATE / 'server-password'
    if not password.exists():
        fd = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(40))
    return password


def load_or_build_config(args, opencode):
    fresh = not CONFIG.exists()
    if fresh:
        if not args.model and not args.preset:
            fail('Fresh install requires --model provider/model or --preset ' + ', '.join(sorted(PRESETS)))
        server_port, console_port = reserve_ports(2)
        c = {'version': 1, 'server_url': loopback_url(server_port),
             'console_url': loopback_url(console_port), 'opencode_binary': opencode,
             'profiles': default_profiles(args.model, args.preset)}
    else:
        c = json.loads(CONFIG.read_text())
        migrate_legacy_fallback_profile(c)
    if 'profiles' not in c:
        if not args.model and not args.preset:
            fail('Config has no profiles; re-run with --model provider/model or --preset')
        c['profiles'] = default_profiles(args.model, args.preset)
    ensure_urls(c)
    for key, value in TOP_LEVEL_DEFAULTS.items():
        c.setdefault(key, value)
    # Per-task iteration caps were removed; drop any legacy max_steps so an
    # old 80 cannot reappear in the configuration or console.
    c.pop('max_steps', None)
    c.setdefault('opencode_binary', opencode)
    if args.opencode:
        c['opencode_binary'] = opencode
    enabled = [k for k, v in c['profiles'].items() if v.get('enabled') is not False]
    if not enabled:
        fail('At least one enabled profile is required')
    c.setdefault('routing', {tier: name if name in enabled else enabled[0] for tier, name in zip(('fast', 'background', 'deep'), PROFILE_NAMES)})
    if fresh and args.preset == 'ark-agent-plan':
        c['routing_policy'] = DEFAULT_ROUTING_POLICY
        c['routing_dynamics'] = DEFAULT_ROUTING_DYNAMICS
        c['quota_spillover'] = DEFAULT_QUOTA_SPILLOVER
    # Legacy global/provider cap fields (max_parallel, max_kimi_parallel,
    # provider_limits) are never written for new installs and are left inert
    # on disk for existing configs; max_parallel_per_owner is the only cap.
    validate_urls(c)
    ensure_network_config(c)
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=str(CONFIG.parent), prefix='.pool-config-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(c, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, CONFIG)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return c, fresh


def backup_existing():
    if INSTALL.exists() and not same_path(ROOT, INSTALL):
        backup = STATE / 'releases' / time.strftime('%Y%m%d-%H%M%S')
        shutil.copytree(INSTALL, backup, ignore=shutil.ignore_patterns('__pycache__'))


def stop_installed_services():
    if (INSTALL / 'scripts' / 'service.py').exists():
        subprocess.run([sys.executable, str(INSTALL / 'scripts' / 'delegate.py'), 'service', 'stop'], check=True)


def copy_package():
    if same_path(ROOT, INSTALL):
        return
    INSTALL.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_ITEMS:
        src, dst = ROOT / name, INSTALL / name
        if same_path(src, dst):
            continue
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
        elif src.is_file():
            shutil.copy2(src, dst)


def write_wrapper():
    bindir = Path(os.environ['DELEGATE_BIN_DIR']).expanduser() if os.environ.get('DELEGATE_BIN_DIR') else Path.home() / '.local' / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / 'delegate-opencode'
    lines = ['#!/bin/sh']
    for var in ENV_VARS:
        value = os.environ.get(var)
        if value:
            lines.append('export ' + var + '=' + shlex.quote(value))
    lines.append('exec ' + shlex.quote(sys.executable) + ' ' +
                 shlex.quote(str(INSTALL / 'scripts' / 'delegate.py')) + ' "$@"')
    wrapper.write_text('\n'.join(lines) + '\n')
    wrapper.chmod(0o755)
    return wrapper


def start_services():
    subprocess.run([sys.executable, str(INSTALL / 'scripts' / 'delegate.py'), 'service', 'start'], check=True)


def stop_command():
    if not (INSTALL / 'scripts' / 'delegate.py').exists():
        fail('No deployment found at ' + str(INSTALL))
    result = subprocess.run([sys.executable, str(INSTALL / 'scripts' / 'delegate.py'), 'service', 'stop'],
                            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output = result.stdout or ''
    sys.stdout.write(output if isinstance(output, str) else json.dumps(output))
    if output and not output.endswith('\n'):
        sys.stdout.write('\n')


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stop', action='store_true', help='Stop services, retaining config, skill and all evidence')
    p.add_argument('--live', action='store_true', help='Upgrade observers/UI while preserving running OpenCode sessions; existing config only')
    p.add_argument('--no-start', action='store_true', help='Install or update without starting services')
    p.add_argument('--model', help='provider/model used for all three profiles on a fresh install')
    p.add_argument('--preset', choices=sorted(PRESETS), help='Known profile bundle for a fresh install')
    p.add_argument('--opencode', help='Path to the opencode binary (default: search PATH)')
    p.add_argument('--wizard', action='store_true',
                   help='Run the interactive bilingual setup wizard (requires --lang en|zh-CN)')
    p.add_argument('--lang', choices=('en', 'zh-CN'),
                   help='Wizard language; only valid together with --wizard')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    check_platform()
    if args.wizard:
        if args.stop or args.live or args.model or args.preset or args.opencode:
            fail('--wizard cannot be combined with --stop, --live, --model, --preset or '
                 '--opencode; make those choices inside the wizard instead')
        if not args.lang:
            fail('--wizard requires an explicit language: --lang en or --lang zh-CN')
        import setup_wizard
        try:
            setup_wizard.run(args.lang, no_start=args.no_start)
        except setup_wizard.Cancelled as cancelled:
            # Distinct code (2) so callers can tell user cancellation/EOF from an
            # installer failure (1) or success (0).
            print(str(cancelled) or 'Setup cancelled; no changes were made.', file=sys.stderr)
            raise SystemExit(2)
        return
    if args.lang:
        fail('--lang is only valid together with --wizard')
    if args.model and args.preset:
        fail('--model and --preset are mutually exclusive')
    if args.model:
        validate_model(args.model)
    if args.stop:
        stop_command()
        return
    opencode = detect_opencode(args.opencode)
    if args.live and (not CONFIG.exists() or args.model or args.preset or args.opencode or args.no_start):
        fail('--live requires an existing configuration and cannot change model/binary or defer startup')
    if not args.live:
        refuse_active_tasks()
    else:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    (STATE / 'logs').mkdir(exist_ok=True, mode=0o700)
    c, fresh = load_or_build_config(args, opencode)
    ensure_password()
    # Fresh installations get the documented admin/admin console login once.
    # Existing credentials are never reset by an install or upgrade.
    import console_auth
    console_auth.ensure_default_account(STATE)
    backup_existing()
    if args.live:
        import service
        service.stop_consumers()
    else:
        stop_installed_services()
    copy_package()
    wrapper = write_wrapper()
    started = not args.no_start
    if started:
        start_services()
    print(json.dumps({'installed_skill': str(INSTALL), 'command': str(wrapper), 'config': str(CONFIG),
                      'state': str(STATE), 'server_url': c['server_url'],
                      'console_url': c.get('console_url'), 'fresh': fresh,
                      'startup': 'live update; existing OpenCode process retained' if args.live else 'started' if started else 'deferred (--no-start)'}, indent=2))


if __name__ == '__main__':
    main()
