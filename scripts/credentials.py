"""Metadata-only local credential references and a redacting child runner.

This module never stores or returns a credential value through its registry. The
CLI can register a reference to an owner-only private file or to an environment
variable name, list safe metadata, remove metadata (never the source) and run a
command with references resolved in-process and injected only through the child
environment.

`credential run` captures stdout/stderr in bounded memory and redacts injected
values plus common reversible encodings before anything is returned. On timeout
or output-cap termination it kills the child process group and suppresses the
captured buffers entirely so a value cut before exact-value redaction cannot
leak. This reduces accidental exposure in normal authorized workflows; it is not
an OS sandbox. A child, or the same-user agent, can deliberately bypass it, and
non-captured side effects or external logs are not covered.
"""
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import time

import common

MAX_CREDENTIAL_BYTES = 65536
MAX_OUTPUT_BYTES = 262144
READ_CHUNK = 65536
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 3600
EXIT_TIMEOUT = 124
EXIT_OUTPUT_LIMIT = 125
REDACTED = '<redacted>'

_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
_ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
_CONTENT_NOTICE = ('Child output was captured and redacted before display. '
                   'Not an OS sandbox: deliberate bypass, non-captured side effects and external logs are not covered.')


class CredentialError(Exception):
    """Safe, value-free credential-reference failure.

    Messages never embed source data, OS error text or reconstructed values.
    """


def _registry_path():
    return common.STATE / 'credentials' / 'registry.json'


def _validate_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise CredentialError('Invalid credential name')
    return name


def _validate_env_name(name):
    if not isinstance(name, str) or not _ENV_RE.match(name):
        raise CredentialError('Invalid environment variable name')
    return name


def _file_metadata(path):
    """Validate an owner-only regular file without reading its contents."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise CredentialError('Credential file must be an absolute path')
    raw = Path(path)
    try:
        info = os.lstat(str(raw))
    except OSError:
        raise CredentialError('Credential file is unavailable') from None
    if stat.S_ISLNK(info.st_mode):
        raise CredentialError('Credential file must not be a symbolic link')
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError('Credential file must be a regular file')
    if info.st_uid != os.geteuid():
        raise CredentialError('Credential file must be owned by the current user')
    if info.st_mode & 0o077:
        raise CredentialError('Credential file must be owner-only (0600 or stricter)')
    if info.st_size <= 0:
        raise CredentialError('Credential file is empty')
    if info.st_size > MAX_CREDENTIAL_BYTES:
        raise CredentialError('Credential file is too large')
    return raw, info


def _read_file_value(path):
    """Read a validated reference through O_NOFOLLOW and re-check the opened inode."""
    raw, _ = _file_metadata(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(raw), flags)
    except OSError:
        raise CredentialError('Credential file is unavailable') from None
    try:
        info = os.fstat(fd)
        # Re-check the opened inode, not just the path: size and owner-only mode
        # must still hold at read time.
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or (info.st_mode & 0o077):
            raise CredentialError('Credential file is unavailable')
        if info.st_size > MAX_CREDENTIAL_BYTES:
            raise CredentialError('Credential file is too large')
        data = os.read(fd, MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        raise CredentialError('Credential file is unavailable') from None
    finally:
        os.close(fd)
    if len(data) > MAX_CREDENTIAL_BYTES:
        raise CredentialError('Credential file is too large')
    if b'\x00' in data:
        raise CredentialError('Credential file is not usable')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise CredentialError('Credential file is not usable') from None
    # Remove at most one trailing line ending; significant spaces are preserved.
    if text.endswith('\r\n'):
        text = text[:-2]
    elif text.endswith('\n'):
        text = text[:-1]
    if not text:
        raise CredentialError('Credential file is empty')
    return text


def _git_tracked(path):
    """Best-effort check that a reference file is not inside Git-tracked contents.

    Walks only the file's own ancestors looking for a `.git` entry, then asks Git
    about that one path. A missing Git or repository is not an error here.
    """
    raw = Path(path)
    parent = raw.parent
    for _ in range(64):
        if (parent / '.git').exists():
            try:
                result = subprocess.run(
                    ['git', '-C', str(parent), 'ls-files', '--error-unmatch', '--', str(raw)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                return result.returncode == 0
            except OSError:
                return False
        if parent.parent == parent:
            break
        parent = parent.parent
    return False


def _load_registry():
    data = common.read_json(_registry_path(), {})
    creds = data.get('credentials') if isinstance(data, dict) else None
    return creds if isinstance(creds, dict) else {}


def _save_registry(creds):
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    common.write_json(path, {'version': 1, 'credentials': creds})


def _file_available(path):
    try:
        _file_metadata(path)
        return True
    except CredentialError:
        return False


def _public_entry(name, entry):
    source = entry.get('source')
    out = {'name': name, 'source': source, 'registered_at': entry.get('registered_at')}
    if source == 'file':
        out['file'] = entry.get('path')
        out['available'] = _file_available(entry.get('path'))
    elif source == 'env':
        out['env'] = entry.get('env')
        out['available'] = bool(os.environ.get(entry.get('env'), ''))
    else:
        out['available'] = False
    return out


def register(name, file=None, env=None):
    """Store reference metadata only; never the value."""
    _validate_name(name)
    if (file is None) == (env is None):
        raise CredentialError('Provide exactly one of --file or --env')
    with common.locked('credentials'):
        creds = _load_registry()
        if name in creds:
            raise CredentialError('Credential name already registered')
        entry = {'registered_at': time.time()}
        if file is not None:
            raw, _ = _file_metadata(file)
            if _git_tracked(raw):
                raise CredentialError('Credential file must stay outside Git-tracked contents')
            entry.update(source='file', path=str(raw))
        else:
            entry.update(source='env', env=_validate_env_name(env))
        creds[name] = entry
        _save_registry(creds)
    return _public_entry(name, entry)


def listing():
    creds = _load_registry()
    return {'credentials': [_public_entry(name, entry) for name, entry in sorted(creds.items())]}


def remove(name):
    """Remove reference metadata only; the source file or env var is untouched."""
    _validate_name(name)
    with common.locked('credentials'):
        creds = _load_registry()
        entry = creds.pop(name, None)
        if entry is None:
            raise CredentialError('Unknown credential name')
        _save_registry(creds)
    return {'removed': name, 'source': entry.get('source')}


def redaction_values():
    """Resolve registered values for shared redaction; never raise or expose errors.

    Used by common.redact so submit/steer guards and collect/transcript output
    include locally registered references. Missing or invalid sources are skipped
    so unrelated redaction and normal tasks keep working.
    """
    values = []
    try:
        creds = _load_registry()
    except Exception:
        return values
    for entry in creds.values():
        if not isinstance(entry, dict):
            continue
        try:
            if entry.get('source') == 'file':
                values.append(_read_file_value(entry.get('path')))
            elif entry.get('source') == 'env':
                value = os.environ.get(entry.get('env'), '')
                if value:
                    values.append(value)
        except Exception:
            continue
    return values


def _resolve_uses(uses):
    if not uses:
        raise CredentialError('At least one --use ENV_NAME=NAME is required')
    creds = _load_registry()
    resolved = []
    seen = set()
    for item in uses:
        if not isinstance(item, str) or '=' not in item:
            raise CredentialError('Each --use must have the form ENV_NAME=NAME')
        env_name, _, ref_name = item.partition('=')
        _validate_env_name(env_name)
        _validate_name(ref_name)
        if env_name in seen:
            raise CredentialError('Duplicate environment variable in --use')
        seen.add(env_name)
        entry = creds.get(ref_name)
        if not isinstance(entry, dict):
            raise CredentialError('Unknown credential reference')
        if entry.get('source') == 'file':
            value = _read_file_value(entry.get('path'))
        elif entry.get('source') == 'env':
            value = os.environ.get(entry.get('env'), '')
            if not value:
                raise CredentialError('Credential reference is unavailable in this process')
        else:
            raise CredentialError('Credential reference is unavailable')
        resolved.append((env_name, value))
    return resolved


def _resolve_reference(name):
    """Resolve one reference for a trusted in-process adapter.

    Callers must never serialize, log, or return the value. Public CLI and console
    surfaces continue to expose metadata only.
    """
    _validate_name(name)
    entry = _load_registry().get(name)
    if not isinstance(entry, dict):
        raise CredentialError('Unknown credential reference')
    if entry.get('source') == 'file':
        return _read_file_value(entry.get('path'))
    if entry.get('source') == 'env':
        value = os.environ.get(entry.get('env'), '')
        if not value:
            raise CredentialError('Credential environment variable is unavailable')
        return value
    raise CredentialError('Credential reference is unavailable')


def _representations(values):
    reps = set()
    for value in values:
        reps.update(common.secret_representations(value))
    return sorted((r for r in reps if r), key=len, reverse=True)


def _make_redactor(values):
    reps = _representations(values)

    def clean(text):
        for rep in reps:
            text = text.replace(rep, REDACTED)
        return text

    return clean


def _argv_contains_secret(argv, values):
    reps = _representations(values)
    for arg in argv:
        if not isinstance(arg, str):
            continue
        for rep in reps:
            if rep and rep in arg:
                return True
    return False


def _kill_group(pgid):
    """Kill the child's process group. The pgid is captured at spawn because
    ``os.getpgid`` can fail once the direct child has been reaped."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _sink():
    return {'buf': bytearray(), 'produced': 0, 'exceeded': False}


def _decode(buf):
    return bytes(buf).decode('utf-8', errors='replace')


def run(uses, timeout, argv):
    """Resolve references in-process, inject via env, capture and redact output.

    Returns ``(cli_exit_code, sanitized_result_dict)``. The child's own exit
    status is preserved; timeout and output-limit use distinct nonzero codes.
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise CredentialError('A non-empty command argument list is required')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or \
            not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise CredentialError('Timeout must be between %s and %s seconds' % (MIN_TIMEOUT, MAX_TIMEOUT))
    resolved = _resolve_uses(uses)
    values = [value for _, value in resolved]
    if any(not value for value in values):
        raise CredentialError('Credential reference is unavailable in this process')
    if _argv_contains_secret(argv, values):
        raise CredentialError('Command arguments contain a credential value or a recognized encoding')
    env = dict(os.environ)
    for env_name, value in resolved:
        env[env_name] = value
    clean = _make_redactor(values)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, env=env, shell=False,
                                start_new_session=True, close_fds=True)
    except OSError:
        # Never echo argv/env or the OS error text, which could carry a value.
        raise CredentialError('Unable to start the command') from None
    # start_new_session makes the child its own process-group leader. Capture the
    # pgid now: os.getpgid can fail once the direct child has been reaped, while
    # descendants that inherited stdout/stderr may still be alive.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    sink_out, sink_err = _sink(), _sink()
    streams = {}
    for stream, sink in ((proc.stdout, sink_out), (proc.stderr, sink_err)):
        try:
            fd = stream.fileno()
            os.set_blocking(fd, False)
            streams[fd] = (stream, sink)
        except (OSError, ValueError):
            pass
    deadline = started + timeout
    stopped = None
    open_fds = set(streams)
    # The deadline governs pipe EOF: a direct child can exit while a descendant
    # keeps the inherited pipe open, so do not stop reading just because the
    # parent was reaped.
    while open_fds:
        wait = max(0.0, min(0.1, deadline - time.monotonic()))
        try:
            ready, _, _ = select.select(list(open_fds), [], [], wait)
        except (OSError, ValueError):
            ready = []
        for fd in ready:
            stream_sink = streams.get(fd)
            if stream_sink is None:
                open_fds.discard(fd)
                continue
            try:
                chunk = os.read(fd, READ_CHUNK)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                chunk = b''
            if not chunk:
                open_fds.discard(fd)
                continue
            sink = stream_sink[1]
            sink['produced'] += len(chunk)
            room = MAX_OUTPUT_BYTES - len(sink['buf'])
            if room > 0:
                sink['buf'].extend(chunk[:room])
            if len(chunk) > room:
                sink['exceeded'] = True
        if sink_out['exceeded'] or sink_err['exceeded']:
            stopped = 'output_limit'
            break
        if time.monotonic() >= deadline:
            stopped = 'timeout'
            break
    if stopped:
        _kill_group(pgid)
        open_fds.clear()
    # Reap the direct child, bounded by the same deadline; kill the group if a
    # child closed its pipes but kept running past it. A small grace avoids
    # misreporting a just-exited child as a timeout.
    remaining = max(0.5, deadline - time.monotonic())
    try:
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        if stopped is None:
            stopped = 'timeout'
        _kill_group(pgid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    for stream, _ in streams.values():
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    duration = round(time.monotonic() - started, 3)
    suppressed = stopped in ('timeout', 'output_limit')
    if suppressed:
        # Do not return buffers that could hold a credential fragment cut before
        # exact-value redaction. Only safe counts and limits are exposed.
        stdout = stderr = ''
    else:
        stdout = clean(_decode(sink_out['buf']))
        stderr = clean(_decode(sink_err['buf']))
    if stopped == 'timeout':
        status, exit_code, cli_code = 'timeout', None, EXIT_TIMEOUT
    elif stopped == 'output_limit':
        status, exit_code, cli_code = 'output_limit', None, EXIT_OUTPUT_LIMIT
    else:
        exit_code = proc.returncode
        status = 'completed' if exit_code == 0 else 'nonzero'
        cli_code = exit_code if exit_code >= 0 else 128 - exit_code
    result = common.redact({
        'command': list(argv), 'status': status, 'exit_code': exit_code,
        'timed_out': stopped == 'timeout', 'truncated': stopped == 'output_limit',
        'output_suppressed': suppressed, 'stdout_bytes': sink_out['produced'],
        'stderr_bytes': sink_err['produced'], 'output_limit_bytes': MAX_OUTPUT_BYTES,
        'duration_seconds': duration, 'stdout': stdout, 'stderr': stderr,
        'injected': [env_name for env_name, _ in resolved], 'content_notice': _CONTENT_NOTICE,
    })
    return cli_code, result


def command(args):
    """CLI entry point. Prints sanitized JSON and returns a process exit code."""
    if args.action == 'register':
        result = register(args.name, file=args.file, env=args.env)
        code = 0
    elif args.action == 'list':
        result, code = listing(), 0
    elif args.action == 'remove':
        result, code = remove(args.name), 0
    elif args.action == 'run':
        argv = list(args.command or [])
        if argv and argv[0] == '--':
            argv = argv[1:]
        code, result = run(args.use, args.timeout, argv)
    else:
        raise CredentialError('Unknown credential action')
    print(json.dumps(common.redact(result), ensure_ascii=False, indent=2))
    return code
