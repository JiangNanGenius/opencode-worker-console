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

ROOT = Path(__file__).resolve().parent.parent
INSTALL = Path(os.environ.get('DELEGATE_INSTALL', '~/.codex/skills/delegate-opencode')).expanduser()
STATE = Path(os.environ.get('DELEGATE_STATE', '~/.local/state/delegate-opencode')).expanduser()
CONFIG = Path(os.environ.get('DELEGATE_CONFIG', '~/.config/opencode/delegate-pool.json')).expanduser()

PACKAGE_ITEMS = ['SKILL.md', 'agents', 'scripts', 'references', 'web']
# Routing defaults (quota.route): fast -> fast-code, background -> senior-code,
# deep -> deep-research. Profile names must stay aligned with those routes.
PROFILE_NAMES = ['fast-code', 'senior-code', 'deep-research']
KIMI_PROVIDER = 'kimi-for-coding'
PRESETS = {
    'deepseek-kimi': {
        'fast-code': {'model': 'deepseek/deepseek-flash', 'variant': 'high', 'label': 'DeepSeek V4.1 Flash'},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'high', 'label': 'Kimi K2.8 Preview'},
        'deep-research': {'model': 'kimi-for-coding/k3', 'variant': 'high', 'label': 'Kimi K3'},
    }
}
TOP_LEVEL_DEFAULTS = {'max_parallel': 3, 'max_steps': 80, 'kimi_reserve_percent': 20, 'auto_approve': True}
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


def detect_opencode(explicit=None):
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            fail('--opencode must point to an executable file: ' + explicit)
        return str(candidate.resolve())
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


def default_profiles(model=None, preset=None):
    if preset:
        return {name: dict(spec) for name, spec in PRESETS[preset].items()}
    return {name: {'model': model, 'label': model.split('/', 1)[1]} for name in PROFILE_NAMES}


def provider_limit_defaults(profiles):
    providers = {spec['model'].split('/', 1)[0] for spec in profiles.values()}
    return {KIMI_PROVIDER: 1} if KIMI_PROVIDER in providers else {}


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
    if 'profiles' not in c:
        if not args.model and not args.preset:
            fail('Config has no profiles; re-run with --model provider/model or --preset')
        c['profiles'] = default_profiles(args.model, args.preset)
    ensure_urls(c)
    for key, value in TOP_LEVEL_DEFAULTS.items():
        c.setdefault(key, value)
    c.setdefault('opencode_binary', opencode)
    if args.opencode:
        c['opencode_binary'] = opencode
    enabled = [k for k, v in c['profiles'].items() if v.get('enabled') is not False]
    if not enabled:
        fail('At least one enabled profile is required')
    c.setdefault('routing', {tier: name if name in enabled else enabled[0] for tier, name in zip(('fast', 'background', 'deep'), PROFILE_NAMES)})
    c.setdefault('provider_limits', provider_limit_defaults(c['profiles']))
    c.setdefault('max_kimi_parallel', c['provider_limits'].get(KIMI_PROVIDER, 1))
    validate_urls(c)
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
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    check_platform()
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
