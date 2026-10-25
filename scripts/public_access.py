"""Managed FRP/Caddy public ingress. Configs contain references, never secrets.

The two proxies only reach the passkey-only loopback listener. They never
forward to the LAN/password listener or directly to OpenCode.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from common import STATE, config, write_json, read_json
import keychain_secrets

ROOT = STATE / 'public-access'
DOMAIN = 'desk.example.com'
FRP_VERSION = '0.71.1'
CADDY_VERSION = '2.11.7'
PUBLIC_PORT = 51529
HTTP_PORT = 54080
HTTPS_PORT = 54443
LABEL = 'com.workerdesk.public.'


def directories():
    for path in (ROOT, ROOT / 'bin', ROOT / 'logs', ROOT / 'caddy-data'):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def settings():
    return read_json(ROOT / 'settings.json', {}) or {}


def _validate(value):
    domain = value.get('domain', DOMAIN)
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', domain) or '..' in domain or '.' not in domain:
        raise ValueError('A concrete DNS domain is required')
    server = value.get('server', '')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', server):
        raise ValueError('Invalid FRP server')
    port = int(value.get('server_port', 0))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid FRP control port')
    return dict(domain=domain, server=server, server_port=port)


def render(value):
    value = _validate(value)
    domain, server = value['domain'], value['server']
    # Config templating reads the token from the child's environment. The
    # environment is populated from Keychain only when that child is started.
    frp = ('serverAddr = ' + json.dumps(server) + '\nserverPort = ' + str(value['server_port']) + '\n'
           'loginFailExit = true\nauth.method = "token"\n'
           'auth.token = "{{ .Envs.WORKERDESK_FRP_TOKEN }}"\n'
           'transport.tls.enable = true\n'
           # Keep explicit application heartbeats for existing older frps.
           # Recent frpc defaults disable them when mux is enabled; an older
           # server can then withdraw every proxy at its 90-second timeout.
           'transport.heartbeatInterval = 30\ntransport.heartbeatTimeout = 90\n'
           'log.level = "info"\n')
    for suffix, kind, port in [('http', 'http', HTTP_PORT), ('https', 'https', HTTPS_PORT)]:
        frp += ('\n[[proxies]]\nname = ' + json.dumps('workerdesk-' + domain + '-' + suffix) + '\n'
                'type = ' + json.dumps(kind) + '\nlocalIP = "127.0.0.1"\nlocalPort = ' + str(port) + '\n'
                'customDomains = [' + json.dumps(domain) + ']\n')
        if kind == 'https':
            frp += 'transport.proxyProtocolVersion = "v2"\n'
    caddy = '''{
    admin off
    default_bind 127.0.0.1
    http_port %d
    https_port %d
    auto_https disable_redirects
    servers 127.0.0.1:%d {
        listener_wrappers {
            proxy_protocol {
                timeout 2s
                allow 127.0.0.1/32
                fallback_policy reject
            }
            tls
        }
    }
    storage file_system {
        root %s
    }
}
http://%s {
    redir https://%s{uri} 308
}
https://%s {
    tls {
        issuer acme {
            disable_tlsalpn_challenge
        }
    }
    reverse_proxy 127.0.0.1:%d {
        flush_interval -1
        header_up X-Forwarded-For {remote_host}
    }
}
''' % (HTTP_PORT, HTTPS_PORT, HTTPS_PORT, json.dumps(str(ROOT / 'caddy-data')), domain, domain, domain, PUBLIC_PORT)
    return frp, caddy


def configure(server, server_port, domain=DOMAIN):
    directories()
    value = _validate(dict(server=server, server_port=server_port, domain=domain))
    frp, caddy = render(value)
    for name, body in [('frpc.toml', frp), ('Caddyfile', caddy)]:
        path = ROOT / name
        if path.exists():
            shutil.copy2(path, ROOT / (name + '.previous'))
        path.write_text(body)
        path.chmod(0o600)
    write_json(ROOT / 'settings.json', value)
    (ROOT / 'settings.json').chmod(0o600)
    return {'domain': domain, 'public_origin': 'https://' + domain, 'configured': True,
            'upstream': '127.0.0.1:' + str(PUBLIC_PORT)}


def _fetch(url):
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read()


def install_binaries():
    if sys.platform != 'darwin' or os.uname().machine != 'arm64':
        raise RuntimeError('This deployment helper currently supports Apple Silicon macOS')
    directories()
    receipts = read_json(ROOT / 'binaries.json', {}) or {}
    for binary, repo, version, archive, sums in (
        ('frpc', 'fatedier/frp', FRP_VERSION, 'frp_%s_darwin_arm64.tar.gz' % FRP_VERSION, 'frp_sha256_checksums.txt'),
        ('caddy', 'caddyserver/caddy', CADDY_VERSION, 'caddy_%s_mac_arm64.tar.gz' % CADDY_VERSION,
         'caddy_%s_checksums.txt' % CADDY_VERSION),
    ):
        dest = ROOT / 'bin' / binary
        old = receipts.get(binary, {})
        if dest.is_file() and old.get('version') == version and hashlib.sha256(dest.read_bytes()).hexdigest() == old.get('binary_sha256'):
            continue
        base = 'https://github.com/%s/releases/download/v%s/' % (repo, version)
        checksums = _fetch(base + sums).decode()
        expected = None
        for line in checksums.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == archive:
                expected = parts[0].lower()
        if not expected or not re.fullmatch('(?:[0-9a-f]{64}|[0-9a-f]{128})', expected):
            raise RuntimeError('Official checksum unavailable for ' + binary)
        raw = _fetch(base + archive)
        algorithm = 'sha256' if len(expected) == 64 else 'sha512'
        if hashlib.new(algorithm, raw).hexdigest() != expected:
            raise RuntimeError('Archive checksum mismatch for ' + binary)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
            members = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == binary]
            if len(members) != 1:
                raise RuntimeError('Ambiguous binary archive')
            data = tar.extractfile(members[0]).read()
        staging = dest.with_suffix('.new')
        staging.write_bytes(data)
        staging.chmod(0o700)
        os.replace(staging, dest)
        receipts[binary] = dict(version=version, archive_digest=expected, archive_algorithm=algorithm,
                                binary_sha256=hashlib.sha256(data).hexdigest())
    write_json(ROOT / 'binaries.json', receipts)
    return {'binaries': {k: v['version'] for k, v in receipts.items()}, 'verified': True}


def _environment():
    env = {k: v for k, v in os.environ.items() if k in ('HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR', 'PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'
    env['XDG_DATA_HOME'] = str(ROOT / 'caddy-data')
    env['XDG_CONFIG_HOME'] = str(ROOT / 'caddy-config')
    return env


def run(component):
    value = _validate(settings())
    _gateway_guard(value)
    env = _environment()
    if component == 'frpc':
        env['WORKERDESK_FRP_TOKEN'] = keychain_secrets.get('com.workerdesk.frp', value['domain']).decode()
        args = [str(ROOT / 'bin/frpc'), '-c', str(ROOT / 'frpc.toml')]
    else:
        args = [str(ROOT / 'bin/caddy'), 'run', '--config', str(ROOT / 'Caddyfile'), '--adapter', 'caddyfile']
    os.execve(args[0], args, env)


def _gateway_guard(value):
    gateway = config()
    passkey = gateway.get('console_passkey', {})
    if not isinstance(passkey, dict) or passkey.get('enabled') is not True or \
            passkey.get('public_origin', '').rstrip('/') != 'https://' + value['domain'] or \
            passkey.get('rp_id') != value['domain'] or gateway.get('console_public_port', PUBLIC_PORT) != PUBLIC_PORT:
        raise RuntimeError('Configure the matching Passkey-only gateway before starting public ingress')


def validate():
    value = _validate(settings())
    _gateway_guard(value)
    token = keychain_secrets.get('com.workerdesk.frp', value['domain']).decode()
    commands = [[str(ROOT / 'bin/frpc'), 'verify', '-c', str(ROOT / 'frpc.toml')],
                [str(ROOT / 'bin/caddy'), 'validate', '--config', str(ROOT / 'Caddyfile'), '--adapter', 'caddyfile']]
    for command in commands:
        env = _environment()
        if Path(command[0]).name == 'frpc':
            env['WORKERDESK_FRP_TOKEN'] = token
        result = subprocess.run(command, env=env, capture_output=True, timeout=30)
        if result.returncode:
            # Never forward raw validator output: a future parser could quote
            # the fully expanded FRP token in a diagnostic.
            raise RuntimeError(Path(command[0]).name + ' configuration validation failed')
    return {'valid': True, 'domain': value['domain']}


def launch(action):
    if sys.platform != 'darwin':
        raise RuntimeError('macOS LaunchAgents are required')
    directories()
    if action == 'start':
        validate()
    folder = Path.home() / 'Library/LaunchAgents'
    folder.mkdir(parents=True, exist_ok=True)
    uid = str(os.getuid())
    def unload(component, remove=False):
        label = LABEL + component
        loaded = subprocess.run(['launchctl', 'list', label], capture_output=True).returncode == 0
        if loaded:
            result = subprocess.run(['launchctl', 'bootout', 'gui/' + uid + '/' + label], capture_output=True)
            if result.returncode:
                raise RuntimeError('Could not unload ' + component + ' LaunchAgent')
        if remove:
            path = folder / (label + '.plist')
            if path.exists():
                path.unlink()

    if action == 'stop':
        # Withdraw the public tunnel first; do not silently re-open it at login.
        for component in ('frpc', 'caddy'):
            unload(component, remove=True)
        return status()

    previous = {}
    for component in ('frpc', 'caddy'):
        path = folder / (LABEL + component + '.plist')
        previous[component] = path.read_bytes() if path.exists() else None
        unload(component)
    started = []
    try:
        for component in ('caddy', 'frpc'):
            label = LABEL + component
            path = folder / (label + '.plist')
            job = dict(Label=label, ProgramArguments=[sys.executable, str(Path(__file__).resolve()), 'run', component],
                       RunAtLoad=True, KeepAlive=True, ThrottleInterval=60,
                       WorkingDirectory=str(ROOT), Umask=0o077,
                       StandardOutPath=str(ROOT / 'logs' / (component + '.log')),
                       StandardErrorPath=str(ROOT / 'logs' / (component + '.log')))
            path.write_bytes(plistlib.dumps(job))
            path.chmod(0o600)
            result = subprocess.run(['launchctl', 'bootstrap', 'gui/' + uid, str(path)], capture_output=True)
            if result.returncode:
                raise RuntimeError('Could not load ' + component + ' LaunchAgent')
            started.append(component)
    except Exception:
        for component in reversed(started):
            unload(component, remove=True)
        for component in ('frpc', 'caddy'):
            path = folder / (LABEL + component + '.plist')
            if path.exists():
                path.unlink()
            # Preserve prior launch configuration for explicit recovery, but
            # do not reload it against potentially changed proxy settings.
            if previous[component] is not None:
                recovery = ROOT / (component + '.launchagent.previous.plist')
                recovery.write_bytes(previous[component])
                recovery.chmod(0o600)
        raise
    return status()


def status():
    result = {'domain': settings().get('domain'), 'public_origin': 'https://' + settings().get('domain', DOMAIN), 'components': {}}
    for component in ('caddy', 'frpc'):
        r = subprocess.run(['launchctl', 'list', LABEL + component], capture_output=True, text=True)
        pid = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
        result['components'][component] = {'loaded': r.returncode == 0, 'running': bool(pid), 'pid': int(pid.group(1)) if pid else None}
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    setup = sub.add_parser('configure')
    setup.add_argument('--server', required=True)
    setup.add_argument('--server-port', type=int, default=5443)
    setup.add_argument('--domain', default=DOMAIN)
    sub.add_parser('install')
    sub.add_parser('validate')
    sub.add_parser('start')
    sub.add_parser('stop')
    sub.add_parser('status')
    child = sub.add_parser('run')
    child.add_argument('component', choices=['frpc', 'caddy'])
    args = p.parse_args()
    if args.action == 'run':
        run(args.component)
        return
    if args.action == 'configure':
        result = configure(args.server, args.server_port, args.domain)
    elif args.action == 'install':
        result = install_binaries()
    elif args.action == 'validate':
        result = validate()
    elif args.action in ('start', 'stop'):
        result = launch(args.action)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        # Error strings here are generated locally; never include a command's
        # captured output or credential exception internals.
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        raise SystemExit(1)
