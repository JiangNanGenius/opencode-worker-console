#!/usr/bin/python3 -I
"""Narrow privileged helper for console host controls (root allowlist only).

Installed by scripts/install_host_helper.py into a root-owned, non-user-writable
directory (/usr/local/bin) with a passwordless sudoers entry for the console
user. The console (which never runs as root) invokes it through ``sudo -n``
for exactly three action families:

* ``power-mode <lowpowermode|highpowermode> <0|1>`` — macOS supported power modes.
* ``power <sleep|restart|shutdown>`` — disruptive power operations.
* ``--verify`` — prove the installation is intact.

The interpreter is fixed and runs isolated (``-I``): no caller-controlled PATH,
PYTHONPATH or user site-packages are consulted, and the script directory is not
importable. All external commands use absolute system paths. Anything outside
this fixed grammar is refused. The helper accepts no secrets; authentication
for disruptive operations happens in the console against the local account
verifier before dispatch. Stdout is a single JSON object; failures return a
non-zero exit code with an honest ``ok: false`` payload.
"""
import json
import os
import re
import subprocess
import sys

PMSET = '/usr/bin/pmset'
SHUTDOWN = '/sbin/shutdown'
POWER_MODES = ('lowpowermode', 'highpowermode')
POWER_OPS = ('sleep', 'restart', 'shutdown')


def _emit(ok, **fields):
    print(json.dumps({'ok': ok, **fields}))
    sys.stdout.flush()
    return 0 if ok else 1


def _run(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(args, 1, '', str(error))


def _read_mode_value(mode):
    """Per-source numeric value of a pmset setting; defaults are omitted by
    macOS, so absence means 0. Returns (values, absent)."""
    result = _run([PMSET, '-g', 'custom'], timeout=15)
    if result.returncode != 0:
        return None, False
    values = re.findall(r'^\s*' + mode + r'\s+(\d+)\s*$', result.stdout, re.M)
    return values, True


def _power_mode(argv):
    mode, value = argv[1], argv[2]
    if mode not in POWER_MODES or value not in ('0', '1'):
        return _emit(False, error='unsupported power mode request')
    applied = _run([PMSET, '-a', mode, value])
    if applied.returncode != 0:
        return _emit(False, error='pmset failed: ' + (applied.stderr or applied.stdout)[:200])
    values, _ = _read_mode_value(mode)
    if values is None:
        return _emit(False, mode=mode, error='readback unavailable')
    requested = value == '1'
    if values:
        readback_ok = all((v == '1') == requested for v in values)
        observed = values
    else:
        # pmset omits settings at their default; default for both modes is 0.
        readback_ok = not requested
        observed = []
    if not readback_ok:
        return _emit(False, mode=mode, requested=value, observed=observed,
                     error='readback does not match the requested value')
    return _emit(True, mode=mode, enabled=requested, observed=observed)


def _power(argv):
    op = argv[1]
    if op not in POWER_OPS:
        return _emit(False, error='unsupported power operation')
    commands = {'sleep': [PMSET, 'sleepnow'],
                'restart': [SHUTDOWN, '-r', 'now'],
                'shutdown': [SHUTDOWN, '-h', 'now']}
    _emit(True, op=op, dispatched=True)
    result = _run(commands[op])
    return 0 if result.returncode == 0 else 1


def main(argv):
    if os.geteuid() != 0:
        return _emit(False, error='helper must run as root')
    if argv == ['--verify']:
        return _emit(True, helper='opencode-host-helper', interpreter=sys.executable,
                     isolated=bool(sys.flags.isolated))
    if len(argv) == 3 and argv[0] == 'power-mode':
        return _power_mode(argv)
    if len(argv) == 2 and argv[0] == 'power':
        return _power(argv)
    return _emit(False, error='unknown helper request')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
