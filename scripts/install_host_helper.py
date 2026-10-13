#!/usr/bin/env python3
"""Install the narrow privileged helper for console host controls.

Installs scripts/host_helper.py into a dedicated root-owned directory
(/opt/opencode-host/bin, mode 0755, root:wheel) with a sudoers drop-in allowing
only this exact binary, without a password, for the invoking user. Run it with
sudo:

    sudo python3 scripts/install_host_helper.py

Verification afterwards (as the console user):

    sudo -n /opt/opencode-host/bin/opencode-host-helper --verify

Rollback (also with sudo):

    sudo rm -f /etc/sudoers.d/opencode-host-helper
    sudo rm -rf /opt/opencode-host

The console never runs as root; until this installer completes and the verify
command succeeds, power-mode changes and sleep/restart/shutdown stay
unavailable and the API reports the helper as unverified. An existing helper
or sudoers file is preserved byte-for-byte when installation fails or is
re-run.
"""
import os
import re
import stat
import tempfile
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent / 'host_helper.py'
INSTALL_DIR = Path('/opt/opencode-host/bin')
TARGET = INSTALL_DIR / 'opencode-host-helper'
SUDOERS = Path('/etc/sudoers.d/opencode-host-helper')
TRUSTED_PYTHON = '/usr/bin/python3'


class InstallFailure(RuntimeError):
    pass


def _check_destination(path):
    # macOS /etc is a system symlink; resolve parents, then verify ownership
    # of the whole canonical chain. Never follow a destination-file symlink.
    if path.is_symlink():
        raise InstallFailure('refusing symlink destination')
    for parent in (path.parent.resolve(), *path.parent.resolve().parents):
        if parent.exists():
            st = parent.stat()
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise InstallFailure('destination parent must be root-owned and not writable by others')
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise InstallFailure('destination must be a regular file')


def _snapshot(path):
    _check_destination(path)
    if not path.exists():
        return None
    st = path.stat()
    return {'bytes': path.read_bytes(), 'mode': st.st_mode & 0o7777,
            'uid': st.st_uid, 'gid': st.st_gid}


def _atomic_write(path, content, mode, uid=0, gid=0):
    _check_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    _check_destination(path)
    fd, name = tempfile.mkstemp(prefix='.host-helper-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
            out.flush()
            os.fchown(out.fileno(), uid, gid)
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _restore(path, snap):
    if snap is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, snap['bytes'], snap['mode'], snap['uid'], snap['gid'])


def _write_root(path, content, mode):
    _atomic_write(path, content, mode)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo: sudo python3 scripts/install_host_helper.py')
    if not SOURCE.is_file():
        raise SystemExit('helper source missing: ' + str(SOURCE))
    if not Path(TRUSTED_PYTHON).is_file():
        raise SystemExit('trusted interpreter missing: ' + TRUSTED_PYTHON)
    first_line = SOURCE.read_text().splitlines()[0]
    if not first_line.startswith('#!' + TRUSTED_PYTHON):
        raise SystemExit('helper shebang must pin ' + TRUSTED_PYTHON + ' with -I')
    target_user = os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name

    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*', target_user):
        raise SystemExit('unsupported account name')
    pwd.getpwnam(target_user)
    previous_helper = _snapshot(TARGET)
    previous_sudoers = _snapshot(SUDOERS)
    try:
        _write_root(TARGET, SOURCE.read_bytes(), 0o755)
        _write_root(SUDOERS, ('# Console host controls: narrow allowlist, no password, exact binary only.\n' +
                              '{} ALL=(root) NOPASSWD: {}\n'.format(target_user, TARGET)).encode(), 0o440)
        check = subprocess.run(['/usr/sbin/visudo', '-c', '-f', str(SUDOERS)], capture_output=True, text=True)
        if check.returncode != 0:
            raise InstallFailure('sudoers drop-in failed validation: ' + check.stderr.strip())
        self_test = subprocess.run([str(TARGET), '--verify'], capture_output=True, text=True, timeout=10)
        if self_test.returncode != 0:
            raise InstallFailure('root self-test failed: ' + (self_test.stderr or self_test.stdout).strip())
        # Verify the target user can elevate this exact binary without a password.
        permission_test = subprocess.run(
            ['/usr/bin/sudo', '-n', '-u', target_user, '/usr/bin/sudo', '-n', str(TARGET), '--verify'],
            capture_output=True, text=True, timeout=15)
        if permission_test.returncode != 0:
            raise InstallFailure('permission test failed for ' + target_user + ': ' +
                                 (permission_test.stderr or permission_test.stdout).strip())
    except (InstallFailure, OSError, subprocess.SubprocessError) as error:
        _restore(TARGET, previous_helper)
        _restore(SUDOERS, previous_sudoers)
        raise SystemExit('installation failed and previous state was restored: ' + str(error))
    print('installed {} for user {}'.format(TARGET, target_user))
    print('root self-test: {}'.format(self_test.stdout.strip()))
    print('permission test as {}: {}'.format(target_user, permission_test.stdout.strip()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
