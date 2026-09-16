"""Find OpenCode or install the pinned official release into private user storage."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

VERSION = '1.18.30'


def asset_name():
    system = platform.system().lower()
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}.get(platform.machine().lower())
    if system not in ('darwin', 'linux') or not arch:
        raise RuntimeError('Automatic OpenCode install supports macOS/Linux arm64 and x64')
    suffix = '-baseline' if arch == 'x64' else ''
    if system == 'linux' and (Path('/etc/alpine-release').exists() or platform.libc_ver()[0] == 'musl'):
        suffix += '-musl'
    return 'opencode-' + system + '-' + arch + suffix + ('.zip' if system == 'darwin' else '.tar.gz')


def ensure_opencode(preferred=None):
    from common import STATE, locked
    managed = STATE / 'runtime' / ('opencode-' + VERSION)
    candidates = [preferred, shutil.which('opencode'), str(managed), str(Path.home()/'.opencode/bin/opencode')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).absolute())
    with locked('bootstrap'):
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
        npm = shutil.which('npm')
        if npm:
            prefix = STATE / 'runtime' / 'npm'
            subprocess.run([npm, 'install', '--prefix', str(prefix), '--no-audit', '--no-fund',
                            '--registry=https://registry.npmjs.org', 'opencode-ai@' + VERSION],
                           check=True, stdout=subprocess.DEVNULL, timeout=300)
            binary = prefix / 'node_modules/.bin/opencode'
            if binary.is_file() and os.access(binary, os.X_OK):
                return str(binary)
            raise RuntimeError('Official npm installation did not produce an executable')
        name = asset_name()
        req = urllib.request.Request('https://api.github.com/repos/anomalyco/opencode/releases/tags/v'+VERSION,
                                     headers={'Accept':'application/vnd.github+json','User-Agent':'Worker-Desk'})
        with urllib.request.urlopen(req, timeout=30) as response:
            release = json.load(response)
        asset = next((a for a in release['assets'] if a['name'] == name), None)
        expected_url = 'https://github.com/anomalyco/opencode/releases/download/v'+VERSION+'/'+name
        if not asset or asset.get('browser_download_url') != expected_url or not asset.get('digest', '').startswith('sha256:'):
            raise RuntimeError('Official release asset or SHA-256 digest unavailable')
        with urllib.request.urlopen(expected_url, timeout=120) as response:
            archive = response.read(300 * 1024 * 1024 + 1)
        if len(archive) > 300 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != asset['digest'][7:]:
            raise RuntimeError('OpenCode release checksum verification failed')
        if name.endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                member = next(m for m in bundle.infolist() if m.filename == 'opencode')
                binary_data = bundle.read(member)
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
                member = next(m for m in bundle.getmembers() if m.name in ('opencode','./opencode') and m.isfile())
                binary_data = bundle.extractfile(member).read()
        managed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=str(managed.parent))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(binary_data)
            os.chmod(temporary, 0o700)
            os.replace(temporary, managed)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        subprocess.run([str(managed), '--version'], check=True, stdout=subprocess.DEVNULL, timeout=15)
        return str(managed)
