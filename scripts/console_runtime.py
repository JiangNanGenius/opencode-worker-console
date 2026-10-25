"""Explicit, isolated Python 3.12 runtime for console WebAuthn dependencies.

Preparation is opt-in and never downloads Python. The worker interpreter is
recorded separately so a restart requested by the 3.12 console keeps workers
on their original interpreter. Environments live outside the installed skill,
because virtual environments cannot safely be copied into release backups.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


LOCK_FILE = Path(__file__).resolve().parent.parent / 'console-auth-requirements.lock'
DEFAULT_PYTHON = '/opt/homebrew/bin/python3.12'
DEFAULT_UV = str(Path.home() / '.local' / 'bin' / 'uv')


def passkey_enabled(c):
    value = c.get('console_passkey', {})
    return isinstance(value, dict) and value.get('enabled') is True


def lock_digest(lock_file=None):
    return hashlib.sha256(Path(lock_file or LOCK_FILE).read_bytes()).hexdigest()


def _read(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _executable(value):
    # Do not resolve symlinks: a venv's python must retain its venv path.
    path = Path(value).expanduser().absolute()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise RuntimeError('Python/runtime executable unavailable: ' + str(path))
    return str(path)


def _version(python):
    result = subprocess.run([_executable(python), '-I', '-c',
                             'import json,sys; print(json.dumps(list(sys.version_info[:2])))'],
                            check=True, capture_output=True, text=True, timeout=20)
    return tuple(json.loads(result.stdout))


def smoke(python, lock_file=None):
    """Check the real interpreter and every locked installed distribution."""
    locked = dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',
                             Path(lock_file or LOCK_FILE).read_text(), re.MULTILINE))
    if locked.get('webauthn') != '3.0.1':
        raise RuntimeError('Unexpected console dependency lock')
    code = ('import importlib.metadata as m,json,sys; '
            'import webauthn,cbor2,cryptography,OpenSSL; '
            'assert sys.version_info[:2] == (3,12), "Console requires Python 3.12"; '
            'expected=json.loads(sys.argv[1]); '
            'actual={name:m.version(name) for name in expected}; '
            'assert actual == expected, "Console dependencies differ from lock"; '
            'print(json.dumps(actual))')
    result = subprocess.run([_executable(python), '-I', '-c', code, json.dumps(locked)],
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def interpreters(c, state):
    """Select pinned interpreters; an enabled but unprepared console fails closed."""
    state = Path(state)
    registry = _read(state / 'runtime' / 'interpreters.json')
    worker = registry.get('worker_python') or sys.executable
    worker = _executable(worker)
    if not passkey_enabled(c):
        return {'worker_python': worker, 'console_python': worker}
    digest = lock_digest()
    expected = state.absolute() / 'runtime' / 'console-auth' / digest / 'venv' / 'bin' / 'python'
    if (registry.get('lock_sha256') != digest or
            registry.get('console_python') != str(expected) or
            not registry.get('worker_python')):
        raise RuntimeError('Passkey console runtime missing or stale; run console_runtime.py prepare')
    try:
        smoke(expected)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        raise RuntimeError('Passkey console runtime failed verification; run console_runtime.py prepare') from exc
    if Path(worker).absolute() == expected:
        raise RuntimeError('Worker interpreter must be separate from the console environment')
    return {'worker_python': worker, 'console_python': str(expected)}


def prepare_runtime(state, worker_python=None, python=None, uv=None):
    """Build, verify, then atomically publish a content-addressed environment.

    A failed dependency install never changes the active interpreter registry.
    Existing ready environments are reused only after a fresh import/version
    smoke check. No service is stopped or started by this function.
    """
    state = Path(state).expanduser().absolute()
    root = state / 'runtime'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'console-auth.lock').open('a') as handle:
        os.chmod(str(root / 'console-auth.lock'), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        registry_path = root / 'interpreters.json'
        previous = _read(registry_path)
        worker = _executable(worker_python or previous.get('worker_python') or sys.executable)
        # An accidental invocation from the managed console must not pin its
        # disposable venv as the worker interpreter.
        if root / 'console-auth' in Path(worker).parents:
            raise RuntimeError('Specify the original worker Python, not the console environment')
        base = _executable(python or previous.get('base_python') or DEFAULT_PYTHON)
        if _version(base) != (3, 12):
            raise RuntimeError('Console base interpreter must be Python 3.12')
        if _version(worker) < (3, 9):
            raise RuntimeError('Worker interpreter must be Python 3.9 or newer')
        digest = lock_digest()
        target = root / 'console-auth' / digest
        console_python = target / 'venv' / 'bin' / 'python'
        ready = target / 'ready.json'
        if ready.exists():
            versions = smoke(console_python)
        else:
            # This path is never published until ready.json and registry are
            # written. Create at its final location to preserve venv shebangs.
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, mode=0o700)
            env = dict(os.environ, UV_PYTHON_DOWNLOADS='never', UV_NO_MANAGED_PYTHON='true')
            tool = _executable(uv or DEFAULT_UV)
            try:
                subprocess.run([tool, 'venv', '--python', base, str(target / 'venv')],
                               check=True, env=env, timeout=120)
                subprocess.run([tool, 'pip', 'sync', '--python', str(console_python),
                                '--require-hashes', '--only-binary', ':all:', str(LOCK_FILE)],
                               check=True, env=env, timeout=300)
                versions = smoke(console_python)
                _write(ready, {'lock_sha256': digest, 'versions': versions})
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
        result = {'worker_python': worker, 'console_python': str(console_python), 'base_python': base,
                  'lock_sha256': digest, 'versions': versions}
        _write(registry_path, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'check'])
    parser.add_argument('--state', default=os.environ.get('DELEGATE_STATE', '~/.local/state/delegate-opencode'))
    parser.add_argument('--worker-python', help='Original worker/CLI Python; never the console venv')
    parser.add_argument('--python', help='Existing Python 3.12 binary; no Python download')
    parser.add_argument('--uv', help='Existing uv binary')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        result = prepare_runtime(args.state, args.worker_python, args.python, args.uv)
    else:
        result = interpreters({'console_passkey': {'enabled': True}}, Path(args.state).expanduser())
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
