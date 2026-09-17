#!/usr/bin/env python3
"""Console account, server-side session and network configuration helpers.

This module owns the local single-admin console credential. It stores only a
PBKDF2-HMAC-SHA256 verifier (per-user random salt, at least 600000 iterations)
and never the plaintext password. Sessions are random 256-bit tokens stored only
as SHA-256 digests with an expiry; logging out, rotating the password or expiry
revokes them. Each session is bound to the account revision that issued it, so a
rotated, missing or corrupt account invalidates every stored session. Login
throttling is a bounded, persisted per-peer counter that consults a
caller-supplied ``X-Forwarded-For`` only when the direct socket peer is an
explicitly trusted reverse proxy.

The admin CLI is deliberately narrow:

    python3 scripts/console_auth.py set-user --username admin --password-stdin
    python3 scripts/console_auth.py configure --bind 127.0.0.1 --origin http://127.0.0.1:1234

``set-user`` accepts no positional or ``--password`` argument and returns only
safe metadata. ``configure`` changes network binding/origins without touching
credentials. A malformed account file is refused, never replaced by an implicit
``admin/admin`` default.
"""
import argparse
import contextlib
import getpass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import time
import urllib.parse

import common

AUTH_DIR_NAME = 'console-auth'
ACCOUNT_FILE = 'account.json'
SESSIONS_FILE = 'sessions.json'
THROTTLE_FILE = 'throttle.json'
INIT_MARKER_FILE = 'initialized.json'
LOCK_FILE = '.lock'

ALGORITHM = 'pbkdf2_hmac_sha256'
MIN_ITERATIONS = 600000
DEFAULT_USERNAME = 'admin'
DEFAULT_PASSWORD = 'admin'
MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_BYTES = 1024
SALT_BYTES = 32
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 4096
LOGIN_WINDOW_SECONDS = 300
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300
MAX_THROTTLE_PEERS = 1024
MAX_ORIGIN_LENGTH = 512
MAX_TRUSTED_PROXIES = 64

_USERNAME_RE = None  # populated below to avoid a stray regex dependency

# A fixed salt used only to spend comparable work when no account exists yet, so
# an unknown user is not obviously cheaper than a known one.
_DUMMY_SALT = hashlib.sha256(b'delegate-console-auth-dummy').digest()[:SALT_BYTES]


class AuthStateError(RuntimeError):
    """Local authentication state is missing, unreadable or malformed.

    The caller must fail closed. A malformed state is never silently replaced by
    a default credential.
    """


def _marker_path(root=None):
    return _auth_dir(root) / INIT_MARKER_FILE


def _mark_initialized(root=None):
    _write_private(_marker_path(root), {'version': 1, 'initialized_at': time.time()})


def _is_initialized(root=None):
    """True once an account has ever existed here.

    A missing marker means no account has ever been created. Any present but
    malformed marker raises: a deleted or corrupted account must fail closed
    rather than silently fall back to the default credential.
    """
    path = _marker_path(root)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AuthStateError('Console authentication state is malformed') from None
    if not isinstance(data, dict) or data.get('version') != 1:
        raise AuthStateError('Console authentication state is malformed')
    return True


def _valid_username(username):
    if not isinstance(username, str) or not 1 <= len(username) <= MAX_USERNAME_LENGTH:
        return False
    first = username[0]
    if not (first.isascii() and first.isalnum()):
        return False
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-')
    return all(c in allowed for c in username)


def _state_root(root=None):
    return Path(root) if root is not None else common.STATE


def _auth_dir(root=None):
    return _state_root(root) / AUTH_DIR_NAME


def _ensure_dir(root=None):
    directory = _auth_dir(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


@contextlib.contextmanager
def _locked(root=None):
    """Serialize account/session/throttle updates under the auth directory."""
    import fcntl
    directory = _ensure_dir(root)
    handle = open(directory / LOCK_FILE, 'a')
    try:
        os.chmod(directory / LOCK_FILE, 0o600)
    except OSError:
        pass
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _write_private(path, value):
    """Atomically persist JSON with an owner-only mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read_account(root=None):
    """Return the validated account verifier or None when no account exists.

    Raises AuthStateError for any malformed state; never falls back.
    """
    path = _auth_dir(root) / ACCOUNT_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AuthStateError('Console authentication state is unreadable') from None
    if not isinstance(data, dict) or data.get('version') != 1:
        raise AuthStateError('Console authentication state is malformed')
    username = data.get('username')
    if not _valid_username(username):
        raise AuthStateError('Console authentication state is malformed')
    if data.get('algorithm') != ALGORITHM:
        raise AuthStateError('Console authentication state is malformed')
    iterations = data.get('iterations')
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < MIN_ITERATIONS:
        raise AuthStateError('Console authentication state is malformed')
    salt, digest = data.get('salt'), data.get('hash')
    if not isinstance(salt, str) or not isinstance(digest, str):
        raise AuthStateError('Console authentication state is malformed')
    try:
        salt_bytes, digest_bytes = bytes.fromhex(salt), bytes.fromhex(digest)
    except ValueError:
        raise AuthStateError('Console authentication state is malformed') from None
    if len(salt_bytes) < 16 or len(digest_bytes) != 32:
        raise AuthStateError('Console authentication state is malformed')
    return {'username': username, 'iterations': iterations, 'salt': salt_bytes,
            'hash': digest_bytes, 'updated_at': data.get('updated_at')}


def has_account(root=None):
    return _read_account(root) is not None


def _safe_metadata(account):
    return {'username': account['username'], 'algorithm': ALGORITHM,
            'iterations': account['iterations'], 'updated_at': account.get('updated_at')}


def _account_fingerprint(account):
    """Stable digest of the exact verifier revision sessions are bound to.

    Any new account file, rotated salt/hash/iterations or different username
    yields a different fingerprint, so a session issued for one revision can
    never validate against another. A crash between writing a new account and
    clearing the session registry therefore leaves no usable old session.
    """
    payload = json.dumps({'version': 1, 'username': account['username'],
                          'algorithm': ALGORITHM, 'iterations': account['iterations'],
                          'salt': account['salt'].hex(), 'hash': account['hash'].hex()},
                         sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _password_bytes(password):
    """UTF-8 bytes for a password, or None when it is not valid Unicode text.

    JSON may carry lone surrogate escapes, which Python represents as a ``str``
    that cannot be encoded as UTF-8. Treat those as invalid input rather than
    letting an encode error escape as an unhandled traceback.
    """
    try:
        return password.encode('utf-8')
    except UnicodeEncodeError:
        return None


def _hash_password(password, salt, iterations):
    raw = _password_bytes(password)
    if raw is None:
        raise ValueError('Password must be valid UTF-8')
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError('Password is too long')
    return hashlib.pbkdf2_hmac('sha256', raw, salt, iterations)


def _validate_password(password):
    if not isinstance(password, str) or not password:
        raise ValueError('Password must be a non-empty string')
    raw = _password_bytes(password)
    if raw is None:
        raise ValueError('Password must be valid UTF-8')
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError('Password is too long')


def _write_account(username, password, root=None):
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _hash_password(password, salt, MIN_ITERATIONS)
    account = {'version': 1, 'username': username, 'algorithm': ALGORITHM,
               'iterations': MIN_ITERATIONS, 'salt': salt.hex(), 'hash': digest.hex(),
               'updated_at': time.time()}
    _write_private(_auth_dir(root) / ACCOUNT_FILE, account)
    return account


def set_user(username, password, root=None):
    """Replace the single admin account and invalidate every existing session."""
    if not _valid_username(username):
        raise ValueError('Username must be 1-%d safe characters' % MAX_USERNAME_LENGTH)
    _validate_password(password)
    with _locked(root):
        account = _write_account(username, password, root)
        _write_private(_auth_dir(root) / SESSIONS_FILE, {'version': 1, 'sessions': {}})
        _mark_initialized(root)
    return _safe_metadata(account)


def ensure_default_account(root=None):
    """Create the documented fresh ``admin/admin`` account only on first use.

    Existing credentials are never reset. Once an account has existed, a missing
    or malformed account file fails closed instead of silently recreating the
    default. A pre-existing account without a marker (a migration) is marked but
    left untouched. A marker that is present but malformed always raises, even
    with an otherwise valid account, so a corrupted initialization state is
    surfaced rather than ignored.
    """
    with _locked(root):
        account = _read_account(root)
        if account is not None:
            if not _marker_path(root).exists():
                _mark_initialized(root)
            else:
                _is_initialized(root)  # validates a present marker; raises if malformed
            return _safe_metadata(account)
        if _is_initialized(root):
            raise AuthStateError('Console account is missing; refusing to recreate a default')
        account = _write_account(DEFAULT_USERNAME, DEFAULT_PASSWORD, root)
        _write_private(_auth_dir(root) / SESSIONS_FILE, {'version': 1, 'sessions': {}})
        _mark_initialized(root)
    return _safe_metadata(account)


def _account_check(account, username, password):
    candidate = _hash_password(password, account['salt'], account['iterations'])
    password_ok = secrets.compare_digest(candidate, account['hash'])
    username_ok = secrets.compare_digest(username.encode('utf-8', 'ignore'),
                                         account['username'].encode('utf-8'))
    return password_ok and username_ok


def verify_credentials(username, password, root=None):
    """Constant-cost password check for the configured account.

    A wrong username still performs the full PBKDF2 computation so an unknown
    account is not measurably cheaper. Raises AuthStateError when the local state
    is malformed so callers can fail closed.
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return False
    raw = _password_bytes(password)
    if raw is None or len(raw) > MAX_PASSWORD_BYTES:
        return False
    account = _read_account(root)
    if account is None:
        _hash_password(password, _DUMMY_SALT, MIN_ITERATIONS)
        return False
    return _account_check(account, username, password)


# --- Server-side sessions -------------------------------------------------

def _read_sessions(root=None):
    path = _auth_dir(root) / SESSIONS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    sessions = data.get('sessions') if isinstance(data, dict) else None
    return sessions if isinstance(sessions, dict) else {}


def _write_sessions(sessions, root=None):
    _write_private(_auth_dir(root) / SESSIONS_FILE, {'version': 1, 'sessions': sessions})


def _token_digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _issue_session_locked(account, root=None):
    """Create a random 256-bit token bound to the account revision. Caller holds lock."""
    token = secrets.token_urlsafe(32)
    digest = _token_digest(token)
    fingerprint = _account_fingerprint(account)
    now = time.time()
    sessions = {k: v for k, v in _read_sessions(root).items()
                if isinstance(v, dict) and isinstance(v.get('expires_at'), (int, float))
                and v['expires_at'] > now}
    if len(sessions) >= MAX_SESSIONS:
        oldest = sorted(sessions.items(), key=lambda kv: kv[1].get('created_at', 0))
        for key, _ in oldest[:len(sessions) - MAX_SESSIONS + 1]:
            sessions.pop(key, None)
    sessions[digest] = {'username': account['username'], 'account': fingerprint,
                        'created_at': now, 'expires_at': now + SESSION_TTL_SECONDS}
    _write_sessions(sessions, root)
    return token


def create_session(username, root=None):
    """Create a session for an already-authenticated caller.

    The session is bound to the account revision present when it is issued; it
    is unusable once that account is missing, corrupt or rotated.
    """
    if not _valid_username(username):
        raise ValueError('Invalid username')
    with _locked(root):
        account = _read_account(root)
        if account is None:
            raise AuthStateError('Console account is missing')
        return _issue_session_locked(account, root)


def authenticate(username, password, root=None):
    """Verify credentials and issue a session atomically for one account revision.

    ``set_user`` takes the same lock and invalidates every session, so a
    concurrent password rotation cannot interleave between the verifier check and
    session issuance: any token returned here is bound to the verifier it passed.
    Returns the session token or None for invalid credentials.
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    raw = _password_bytes(password)
    if raw is None or len(raw) > MAX_PASSWORD_BYTES:
        return None
    with _locked(root):
        account = _read_account(root)
        if account is None:
            _hash_password(password, _DUMMY_SALT, MIN_ITERATIONS)
            return None
        if not _account_check(account, username, password):
            return None
        return _issue_session_locked(account, root)


def session_username(token, root=None):
    """Return the username for a live session token, or None.

    The stored session must still match the current account fingerprint. A
    missing, unreadable or rotated account invalidates every session, so a
    crash between writing a new account and clearing the registry cannot leave
    an old token usable. Any malformed entry fails closed.
    """
    if not isinstance(token, str) or not token:
        return None
    entry = _read_sessions(root).get(_token_digest(token))
    if not isinstance(entry, dict):
        return None
    expires = entry.get('expires_at')
    if not isinstance(expires, (int, float)) or expires <= time.time():
        return None
    username = entry.get('username')
    if not _valid_username(username):
        return None
    stored = entry.get('account')
    if not isinstance(stored, str):
        return None
    try:
        account = _read_account(root)
    except AuthStateError:
        return None
    if account is None or account['username'] != username:
        return None
    if not secrets.compare_digest(stored, _account_fingerprint(account)):
        return None
    return username


def revoke_session(token, root=None):
    if not isinstance(token, str) or not token:
        return False
    digest = _token_digest(token)
    with _locked(root):
        sessions = _read_sessions(root)
        if digest not in sessions:
            return False
        sessions.pop(digest, None)
        _write_sessions(sessions, root)
    return True


def clear_sessions(root=None):
    with _locked(root):
        _write_sessions({}, root)


# --- Bounded per-peer login throttling ------------------------------------

def _peer_key(peer):
    if not isinstance(peer, str) or not peer:
        return 'unknown'
    return peer[:MAX_USERNAME_LENGTH]


def _read_throttle(root=None):
    path = _auth_dir(root) / THROTTLE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    peers = data.get('peers') if isinstance(data, dict) else None
    return peers if isinstance(peers, dict) else {}


def _write_throttle(peers, root=None):
    _write_private(_auth_dir(root) / THROTTLE_FILE, {'version': 1, 'peers': peers})


def throttle_retry_after(peer, root=None):
    """Seconds until the peer may try again, or 0 when not locked."""
    entry = _read_throttle(root).get(_peer_key(peer))
    if not isinstance(entry, dict):
        return 0
    locked_until = entry.get('locked_until')
    if isinstance(locked_until, (int, float)) and locked_until > time.time():
        return max(1, int(math.ceil(locked_until - time.time())))
    return 0


def admit_login_attempt(peer, root=None):
    """Atomically reserve a login attempt before any password hashing.

    Returns 0 when the attempt may proceed, or the seconds to wait when the peer
    is locked. The reservation is written under one lock so concurrent or slow
    requests cannot each observe an unlocked bucket and then all hash: at most
    ``MAX_LOGIN_FAILURES`` attempts are admitted before the peer locks. A
    successful login clears the reservation with ``record_login_success``.
    """
    now = time.time()
    key = _peer_key(peer)
    with _locked(root):
        peers = {k: v for k, v in _read_throttle(root).items() if isinstance(v, dict)}
        entry = peers.get(key) or {'failures': [], 'locked_until': 0}
        locked_until = entry.get('locked_until')
        if isinstance(locked_until, (int, float)) and locked_until > now:
            return max(1, int(math.ceil(locked_until - now)))
        failures = [t for t in entry.get('failures', [])
                    if isinstance(t, (int, float)) and now - t < LOGIN_WINDOW_SECONDS]
        if len(failures) >= MAX_LOGIN_FAILURES:
            peers[key] = {'failures': [], 'locked_until': now + LOGIN_LOCKOUT_SECONDS}
            _write_throttle(peers, root)
            return LOGIN_LOCKOUT_SECONDS
        failures.append(now)
        peers[key] = {'failures': failures, 'locked_until': 0}
        if len(peers) > MAX_THROTTLE_PEERS:
            ordered = sorted(peers.items(), key=lambda kv: min(kv[1].get('failures') or [0]))
            for extra, _ in ordered[:len(peers) - MAX_THROTTLE_PEERS]:
                peers.pop(extra, None)
        _write_throttle(peers, root)
    return 0


def record_login_success(peer, root=None):
    key = _peer_key(peer)
    with _locked(root):
        peers = {k: v for k, v in _read_throttle(root).items() if isinstance(v, dict)}
        if key in peers:
            peers.pop(key, None)
            _write_throttle(peers, root)


# --- Network configuration validation -------------------------------------

def validate_origin(origin):
    """Strictly validate an allowed console origin and return a canonical form."""
    if not isinstance(origin, str) or not origin or len(origin) > MAX_ORIGIN_LENGTH:
        raise ValueError('Origin must be a non-empty http(s) URL')
    parts = urllib.parse.urlsplit(origin)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('Origin must use http or https')
    if parts.username or parts.password:
        raise ValueError('Origin must not include user information')
    if parts.path not in ('', '/'):
        raise ValueError('Origin must not include a path')
    if parts.query or parts.fragment:
        raise ValueError('Origin must not include a query or fragment')
    host = parts.hostname
    if not host or '*' in host:
        raise ValueError('Origin must name a specific host')
    try:
        port = parts.port
    except ValueError:
        raise ValueError('Origin port is invalid') from None
    if port is None:
        port = 443 if parts.scheme == 'https' else 80
    if not 1 <= port <= 65535:
        raise ValueError('Origin port is invalid')
    default_port = 443 if parts.scheme == 'https' else 80
    authority = host.lower() + ('' if port == default_port else ':' + str(port))
    return parts.scheme + '://' + authority


def _origin_tuple(origin):
    canonical = validate_origin(origin)
    parts = urllib.parse.urlsplit(canonical)
    return (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))


def origin_tuples(config):
    """All allowed origins as (scheme, host, port); console_url is implicit."""
    seen, origins = set(), []
    raw = config.get('console_allowed_origins')
    candidates = list(raw) if isinstance(raw, list) else []
    console_url = config.get('console_url')
    if console_url:
        candidates.append(console_url)
    for item in candidates:
        parsed = _origin_tuple(item)
        if parsed not in seen:
            seen.add(parsed)
            origins.append(parsed)
    if not origins:
        raise ValueError('console_url or console_allowed_origins is required')
    return origins


def _normalize_host_header(host_header):
    if not isinstance(host_header, str) or not host_header:
        return None
    host_header = host_header.strip()
    if not host_header or '@' in host_header or '/' in host_header or '\\' in host_header:
        return None
    if host_header.startswith('['):
        return None  # IPv6 literals are out of scope for this console
    host, sep, port = host_header.partition(':')
    host = host.lower()
    if not host or '*' in host:
        return None
    if sep:
        if not port.isdigit():
            return None
        value = int(port)
        if not 1 <= value <= 65535:
            return None
        return host, value
    return host, None


def host_allowed(host_header, origins):
    normalized = _normalize_host_header(host_header)
    if normalized is None:
        return False
    host, port = normalized
    for scheme, allowed_host, allowed_port in origins:
        if allowed_host != host:
            continue
        if port is None:
            if allowed_port in (80, 443):
                return True
        elif port == allowed_port:
            return True
    return False


def origin_allowed(origin_header, origins):
    if not isinstance(origin_header, str):
        return False
    try:
        return _origin_tuple(origin_header) in origins
    except ValueError:
        return False


def origin_matches_host(origin_header, host_header):
    """True when the Origin authority equals the request Host authority.

    Membership in the allowed-origin list is not enough: a browser must not be
    able to reuse an allowed origin while addressing a different allowed host.
    """
    try:
        _, origin_host, origin_port = _origin_tuple(origin_header)
    except ValueError:
        return False
    normalized = _normalize_host_header(host_header)
    if normalized is None:
        return False
    host, port = normalized
    if origin_host != host:
        return False
    if port is None:
        return origin_port in (80, 443)
    return port == origin_port


def secure_for_host(host_header, origins):
    """True when the matching configured origin is HTTPS, so the cookie is Secure."""
    normalized = _normalize_host_header(host_header)
    if normalized is None:
        return False
    host, port = normalized
    for scheme, allowed_host, allowed_port in origins:
        if allowed_host != host:
            continue
        if port is None:
            if scheme == 'https' and allowed_port == 443:
                return True
        elif scheme == 'https' and port == allowed_port:
            return True
    return False


def validate_bind(host):
    """Accept 127.0.0.1, 0.0.0.0, or an IPv4 actually bound locally."""
    if not isinstance(host, str):
        raise ValueError('console_bind must be an IPv4 address')
    host = host.strip()
    if not host:
        raise ValueError('console_bind must be an IPv4 address')
    if host in ('127.0.0.1', '0.0.0.0'):
        return host
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        raise ValueError('console_bind must be 127.0.0.1, 0.0.0.0 or a local IPv4 address') from None
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
    except OSError:
        raise ValueError('console_bind must be a local IPv4 address') from None
    finally:
        probe.close()
    return host


def console_bind(config):
    """Validated bind address, defaulting to loopback."""
    return validate_bind(config.get('console_bind') or '127.0.0.1')


def validate_trusted_proxy(value):
    """Validate one trusted reverse-proxy IPv4 address or CIDR entry."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError('Trusted proxy must be an IPv4 address or CIDR')
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValueError('Trusted proxy must be an IPv4 address or CIDR') from None
    if network.version != 4:
        raise ValueError('Trusted proxy must be IPv4')
    if network.prefixlen == 0:
        raise ValueError('Trusted proxy must not be a catch-all network')
    return str(network)


def trusted_proxy_networks(config):
    """Validated trusted-proxy networks; empty means trust no forwarded header."""
    raw = config.get('console_trusted_proxies')
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TRUSTED_PROXIES:
        raise ValueError('console_trusted_proxies must be a bounded list')
    return [ipaddress.ip_network(validate_trusted_proxy(item), strict=False) for item in raw]


def client_identity(socket_peer, forwarded_for, trusted_networks):
    """Throttle identity for a request.

    The socket peer is always the fallback and the only identity unless the peer
    is an explicitly trusted reverse proxy. A trusted proxy must supply exactly
    one sanitized IP in ``X-Forwarded-For``; list values, malformed values or a
    header from an untrusted peer are ignored.
    """
    peer = _peer_key(socket_peer)
    if not trusted_networks:
        return peer
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(address in network for network in trusted_networks):
        return peer
    if not isinstance(forwarded_for, str):
        return peer
    value = forwarded_for.strip()
    if not value or ',' in value:
        return peer  # require one sanitized value; never parse an untrusted chain
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return peer


def configure(bind=None, origins=None, trusted_proxies=None, clear_trusted_proxies=False,
              config_path=None):
    """Persist network binding/origins/trusted proxies without touching credentials.

    Unrelated configuration and all authentication state are preserved.
    """
    if bind is None and not origins and trusted_proxies is None and not clear_trusted_proxies:
        raise ValueError('Provide --bind, --origin, --trust-proxy or --clear-trusted-proxies')
    path = Path(config_path) if config_path is not None else common.CONFIG
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError('Pool configuration is unavailable') from None
    if not isinstance(config, dict):
        raise ValueError('Pool configuration is unavailable')
    result = {}
    if bind is not None:
        config['console_bind'] = validate_bind(bind)
        result['console_bind'] = config['console_bind']
    if origins:
        config['console_allowed_origins'] = [validate_origin(origin) for origin in origins]
        result['console_allowed_origins'] = config['console_allowed_origins']
    if trusted_proxies is not None:
        if len(trusted_proxies) > MAX_TRUSTED_PROXIES:
            raise ValueError('At most %d trusted proxies are supported' % MAX_TRUSTED_PROXIES)
        config['console_trusted_proxies'] = [validate_trusted_proxy(item) for item in trusted_proxies]
        result['console_trusted_proxies'] = config['console_trusted_proxies']
    elif clear_trusted_proxies:
        config['console_trusted_proxies'] = []
        result['console_trusted_proxies'] = []
    _write_private(path, config)
    return result


# --- CLI ------------------------------------------------------------------

def _read_password(password_stdin):
    if password_stdin:
        raw = sys.stdin.buffer.read(MAX_PASSWORD_BYTES + 2)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('Password must be valid UTF-8') from None
        if text.endswith('\r\n'):
            text = text[:-2]
        elif text.endswith('\n'):
            text = text[:-1]
        return text
    first = getpass.getpass('New console password: ')
    second = getpass.getpass('Confirm console password: ')
    if first != second:
        raise ValueError('Passwords do not match')
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    setter = sub.add_parser('set-user', help='Set the single console admin username/password')
    setter.add_argument('--username', required=True)
    setter.add_argument('--password-stdin', action='store_true',
                        help='Read the password from stdin instead of an interactive prompt')
    configure_parser = sub.add_parser('configure', help='Set console bind, allowed origins and trusted proxies')
    configure_parser.add_argument('--bind', help='127.0.0.1 (default), 0.0.0.0 or a local IPv4 address')
    configure_parser.add_argument('--origin', action='append', default=[],
                                  help='Allowed public origin, e.g. https://desk.example.test (repeatable)')
    configure_parser.add_argument('--trust-proxy', action='append', default=[],
                                  help='Trusted reverse-proxy IPv4 address or CIDR (repeatable)')
    configure_parser.add_argument('--clear-trusted-proxies', action='store_true',
                                  help='Remove every trusted reverse-proxy entry')
    args = parser.parse_args(argv)
    try:
        if args.action == 'set-user':
            result = set_user(args.username, _read_password(args.password_stdin))
        else:
            result = configure(args.bind, args.origin, args.trust_proxy or None,
                               args.clear_trusted_proxies)
    except (AuthStateError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
