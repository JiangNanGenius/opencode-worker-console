#!/usr/bin/env python3
"""Loopback credential-pool proxy for the fixed Alibaba Token Plan endpoint.

The proxy is deliberately narrow. It accepts authenticated Anthropic-style
``POST /messages`` (and ``/messages/count_tokens``) requests on loopback, selects
the first eligible credential slot, and forwards the exact request bytes to the
single fixed upstream base URL in :mod:`providers`. Host, scheme and path are
never supplied by the caller. Local authentication is removed before forwarding
and the selected slot's key is injected as ``x-api-key``.

Only credential references are configured: a macOS Keychain service/account
pair or an owner-only file path. Key values are read at request time, never
stored in the private state file, never logged, and never included in errors.
A slot's blocking error is bound to a non-reversible credential generation so a
rotated key automatically becomes eligible again.

A request is retried on the next slot only when the upstream explicitly rejects
it before any response is forwarded with a confirmed quota/balance or auth
error. Transport failures, 5xx responses, temporary 429s and any response that
has started streaming are relayed or failed locally without replay.
"""
import gzip
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import stat as stat_module
import ssl
import sys
import time
import urllib.parse
import uuid
import math
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
import keychain_secrets
import providers


DEFAULT_PORT = 51530
DEFAULT_TIMEOUT_SECONDS = 900
MAX_REQUEST_BYTES = 64 * 1024 * 1024
ERROR_BODY_LIMIT = 1024 * 1024
STATE_FILE = 'bailian-pool.json'
ALLOWED_PATHS = frozenset(('/messages', '/messages/count_tokens'))
BLOCK_STATES = frozenset(('exhausted', 'auth'))
HOP_BY_HOP = frozenset(('connection', 'keep-alive', 'proxy-authenticate',
                        'proxy-authorization', 'te', 'trailers',
                        'transfer-encoding', 'upgrade'))
SENSITIVE_RESPONSE_HEADERS = frozenset(('authorization', 'x-api-key',
                                        'proxy-authorization'))
_SLOT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_GENERATION = re.compile(r'[A-Za-z0-9._:-]{1,128}')


def log(message):
    """Write one bounded, printable-only line; never headers or bodies."""
    text = ''.join(char if 32 <= ord(char) <= 126 else ' ' for char in str(message))
    print('bailian-pool: ' + text[:500], file=sys.stderr, flush=True)


def _safe_metadata(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255:
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)


def enabled(config):
    block = config.get('bailian_pool') if isinstance(config, dict) else None
    return isinstance(block, dict) and block.get('enabled') is True


def pool_settings(config):
    """Return normalized settings, None when disabled, or raise ValueError.

    Validation happens before any process starts and before the runtime overlay
    is built. Only reference metadata is accepted; a slot that looks like it
    carries a plaintext key is rejected rather than ignored.
    """
    block = config.get('bailian_pool') if isinstance(config, dict) else None
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError('bailian_pool must be an object')
    if block.get('enabled') is False or 'enabled' not in block:
        return None
    if block.get('enabled') is not True:
        raise ValueError('bailian_pool.enabled must be true or false')
    unknown = sorted(set(block) - {'enabled', 'listen_port', 'request_timeout_seconds', 'slots'})
    if unknown:
        raise ValueError('bailian_pool has unsupported settings: ' + ', '.join(unknown))
    port = block.get('listen_port', DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('bailian_pool.listen_port must be an integer from 1 to 65535')
    timeout = block.get('request_timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ValueError('bailian_pool.request_timeout_seconds must be between 1 and 3600')
    raw_slots = block.get('slots')
    if not isinstance(raw_slots, list) or not 1 <= len(raw_slots) <= 16:
        raise ValueError('bailian_pool.slots must contain 1 to 16 slots')
    slots = []
    seen = set()
    for item in raw_slots:
        if not isinstance(item, dict):
            raise ValueError('each bailian_pool slot must be an object')
        unknown = sorted(set(item) - {'id', 'keychain', 'file', 'generation'})
        if unknown:
            raise ValueError('slot has unsupported settings: ' + ', '.join(unknown))
        slot_id = item.get('id')
        if not isinstance(slot_id, str) or not _SLOT_ID.fullmatch(slot_id):
            raise ValueError('slot id must be 1-64 letters, digits, dot, dash or underscore')
        if slot_id in seen:
            raise ValueError('slot ids must be unique')
        seen.add(slot_id)
        keychain = item.get('keychain')
        file_value = item.get('file')
        if (keychain is None) == (file_value is None):
            raise ValueError('slot must set exactly one of keychain or file')
        generation = item.get('generation')
        if generation is not None and (not isinstance(generation, str) or
                                       not _GENERATION.fullmatch(generation)):
            raise ValueError('slot generation must be 1-128 safe metadata characters')
        if keychain is not None:
            if not isinstance(keychain, dict) or set(keychain) != {'service', 'account'}:
                raise ValueError('slot keychain must contain only service and account')
            service, account = keychain.get('service'), keychain.get('account')
            if not _safe_metadata(service) or not _safe_metadata(account):
                raise ValueError('slot keychain service and account are required metadata')
            slots.append({'id': slot_id, 'source': 'keychain', 'service': service,
                          'account': account, 'generation': generation})
        else:
            if not providers.valid_key_file(file_value):
                raise ValueError('slot file must be an absolute owner-controlled path')
            slots.append({'id': slot_id, 'source': 'file', 'file': file_value,
                          'generation': generation})
    return {'port': port, 'timeout': float(timeout), 'slots': slots}


def validate_config(config):
    """Compatibility alias used by service and tests."""
    return pool_settings(config)


def _file_identity(value):
    path = Path(value).expanduser()
    try:
        stat = path.stat()
        return '\x1f'.join(('file', str(path.resolve()), str(stat.st_dev),
                            str(stat.st_ino), str(stat.st_size),
                            str(stat.st_mtime_ns), str(stat.st_ctime_ns)))
    except OSError:
        return '\x1f'.join(('file', str(path), 'missing'))


def pool_identity(config):
    """Opaque provider identity for pooled inference; never reads key material.

    It changes when the slot order/source metadata changes and, for file-backed
    slots, when the file's stat identity changes. Keychain slots can add a
    non-secret ``generation`` label when a rotated value must also detach a
    provider-level billing circuit.
    """
    try:
        settings = pool_settings(config)
    except Exception:
        return None
    if settings is None:
        return None
    parts = ['bailian-pool-v1']
    for slot in settings['slots']:
        parts.extend(('slot', slot['id'], slot['source'], slot.get('generation') or ''))
        if slot['source'] == 'keychain':
            parts.extend((slot['service'], slot['account']))
        else:
            parts.append(_file_identity(slot['file']))
    return hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()


def state_path():
    return common.STATE / STATE_FILE


def read_state():
    value = common.read_json(state_path(), {})
    return value if isinstance(value, dict) else {}


def _write_state(value):
    value['version'] = 1
    value['updated_at'] = time.time()
    common.write_json(state_path(), value)


def _record(state, slot_id):
    slots = state.get('slots')
    record = slots.get(slot_id) if isinstance(slots, dict) else None
    return record if isinstance(record, dict) else None


def _slot_is_blocked(record, generation):
    if not isinstance(record, dict) or record.get('state') not in BLOCK_STATES:
        return False
    stored = record.get('generation')
    # A block is bound to the generation that produced it. A changed
    # generation is a new credential and can be tested; a missing generation
    # cannot prove a rotation, so fail closed and keep the block.
    return not stored or stored == generation


def _physical_generation(slot, key):
    return 'p1_' + hashlib.sha256(b'bailian-pool-physical\x00' + slot['id'].encode() +
                                 b'\x00' + key).hexdigest()[:32]


def _effective_generation(slot, key):
    digest = hashlib.sha256(b'bailian-pool-slot\x00' + slot['id'].encode('utf-8') +
                            b'\x00' + key).hexdigest()[:32]
    configured = slot.get('generation')
    if configured:
        digest = hashlib.sha256(b'bailian-pool-configured\x00' +
                                configured.encode('utf-8') + b'\x00' +
                                digest.encode('ascii')).hexdigest()[:32]
    return 'g1_' + digest


class SlotCredentialError(Exception):
    """A slot's reference could not be resolved; no value is attached."""


def _load_key(slot):
    if slot['source'] == 'keychain':
        try:
            raw = keychain_secrets.get_bounded(slot['service'], slot['account'])
        except Exception:
            raise SlotCredentialError() from None
    else:
        path = Path(slot['file']).expanduser()
        try:
            stat = path.stat()
        except OSError:
            raise SlotCredentialError() from None
        if not stat_module.S_ISREG(stat.st_mode):
            raise SlotCredentialError()
        if hasattr(os, 'getuid') and stat.st_uid != os.getuid():
            raise SlotCredentialError()
        if stat.st_mode & 0o077:
            raise SlotCredentialError()
        if not 1 <= stat.st_size <= 65536:
            raise SlotCredentialError()
        try:
            raw = path.read_bytes()
        except OSError:
            raise SlotCredentialError() from None
    if isinstance(raw, str):
        raw = raw.encode('utf-8', 'strict')
    if not isinstance(raw, (bytes, bytearray)):
        raise SlotCredentialError()
    key = bytes(raw).strip()
    if not 1 <= len(key) <= 65536:
        raise SlotCredentialError()
    if any(byte < 0x20 or byte == 0x7f for byte in key):
        raise SlotCredentialError()
    try:
        key.decode('ascii')
    except UnicodeDecodeError:
        raise SlotCredentialError() from None
    return key, _effective_generation(slot, key)


RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 3600


def _number(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _retry_at(record):
    return _number(record.get('retry_at'), _number(record.get('blocked_at'),
                   _number(record.get('updated_at'))) + RETRY_BASE_SECONDS)


def _probe_due(record, now=None):
    now = time.time() if now is None else now
    lease = record.get('probe_lease') or {}
    return (record.get('state') == 'exhausted' and _retry_at(record) <= now and
            _number(lease.get('until')) <= now)


def _claim_attempt(slot, generation, timeout=DEFAULT_TIMEOUT_SECONDS, allow_probe=True, allow_rotation=True):
    """CAS one durable half-open lease; normal traffic shares the block epoch."""
    try:
        loaded_key, loaded_generation = _load_key(slot)
        if loaded_generation != generation:
            return None
        physical = _physical_generation(slot, loaded_key)
    except SlotCredentialError:
        return None
    with common.locked('bailian-pool'):
        state = read_state()
        slots = state.setdefault('slots', {})
        if not isinstance(slots, dict):
            slots = {}
            state['slots'] = slots
        record = _record(state, slot['id']) or {}
        if not allow_rotation and (record.get('state') in BLOCK_STATES or
                                   record.get('generation') and record['generation'] != generation):
            return None
        blocked = _slot_is_blocked(record, generation)
        physical_rotated = bool(record.get('key_generation') and record['key_generation'] != physical)
        probe = blocked and record.get('state') == 'exhausted' or allow_probe and physical_rotated
        if blocked and (not allow_probe or not _probe_due(record)):
            return None
        if record.get('generation') and record['generation'] != generation:
            previous_physical = record.get('key_generation')
            record = {'epoch': int(_number(record.get('epoch'))) + 1}
            if probe and physical_rotated:
                record.update(state='exhausted', kind='credential_rotation', retry_at=time.time(),
                              rotation_from=previous_physical)
        fresh = dict(record)
        epoch = int(_number(record.get('epoch')))
        attempt = {'epoch': epoch, 'probe': probe, 'id': uuid.uuid4().hex}
        if probe:
            epoch += 1
            attempt['epoch'] = epoch
            fresh['probe_lease'] = {'id': attempt['id'], 'until': time.time() + timeout + 60,
                                    'generation': generation, 'epoch': epoch}
        else:
            fresh.update(state='ok')
            for k in ('kind', 'last_error', 'http_status', 'probe_lease'):
                fresh.pop(k, None)
        fresh.update(generation=generation, key_generation=physical,
                     configured_generation=slot.get('generation'), epoch=epoch, updated_at=time.time())
        slots[slot['id']] = fresh
        state['active'] = slot['id']
        _write_state(state)
        return attempt


def _claim(slot, generation):
    """Compatibility admission; only proxy /messages may claim a half-open probe."""
    return _claim_attempt(slot, generation, allow_probe=False) is not None


def _attempt_matches(record, attempt):
    if attempt is None:
        return True
    if _number(record.get('epoch')) != attempt['epoch']:
        return False
    return not attempt['probe'] or (record.get('probe_lease') or {}).get('id') == attempt['id']


def _mark(slot, generation, state_name, kind=None, http_status=None, last_error=None,
          attempt=None, verified_model=False):
    """Result CAS: epoch + lease + actual current credential; never header-only success."""
    try:
        loaded_key, loaded_generation = _load_key(slot)
        if loaded_generation != generation:
            return False
        physical = _physical_generation(slot, loaded_key)
    except SlotCredentialError:
        return False
    now = time.time()
    with common.locked('bailian-pool'):
        state = read_state()
        slots = state.setdefault('slots', {})
        if not isinstance(slots, dict):
            slots = {}
            state['slots'] = slots
        record = _record(state, slot['id']) or {}
        if record.get('generation') and record['generation'] != generation or not _attempt_matches(record, attempt):
            return False
        fresh = dict(record)
        fresh.update(key_generation=physical, configured_generation=slot.get('generation'))
        if state_name == 'ok':
            if not verified_model:
                fresh['last_success_at'] = now
                if record.get('state') in BLOCK_STATES:
                    slots[slot['id']] = fresh
                    _write_state(state)
                    return False
            if record.get('state') in BLOCK_STATES and not (attempt and attempt['probe'] and verified_model):
                return False
            if verified_model:
                fresh.update(last_model_success_at=now, success_generation=generation,
                             success_epoch=int(_number(record.get('epoch'))))
            fresh.update(state='ok', generation=generation, updated_at=now)
            for k in ('probe_lease', 'retry_at', 'failure_count', 'kind', 'last_error', 'http_status'):
                fresh.pop(k, None)
        else:
            count = min(16, int(_number(record.get('failure_count'))) + 1)
            fresh.update(state=state_name, generation=generation, updated_at=now,
                         epoch=int(_number(record.get('epoch'))) + 1, blocked_at=now,
                         failure_count=count)
            fresh.pop('probe_lease', None)
            if state_name == 'auth':
                fresh.pop('rotation_from', None)
            if state_name == 'exhausted':
                fresh['retry_at'] = now + min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (count - 1))
            for k, v in (('kind', kind), ('http_status', http_status), ('last_error', last_error)):
                if v is not None:
                    fresh[k] = v
        slots[slot['id']] = fresh
        _write_state(state)
        return True


def _renew_probe(slot, generation, attempt, timeout):
    if not attempt['probe']:
        return
    with common.locked('bailian-pool'):
        state = read_state()
        record = _record(state, slot['id']) or {}
        if record.get('generation') == generation and _attempt_matches(record, attempt):
            lease = record['probe_lease']
            if lease['until'] - time.time() < timeout + 30:
                lease['until'] = time.time() + timeout + 60
                _write_state(state)


def _finish_probe(slot, generation, attempt):
    """Ambiguous/temporary/incomplete result: no replay, stay exhausted with cooldown."""
    if not attempt['probe']:
        return
    with common.locked('bailian-pool'):
        state = read_state()
        record = _record(state, slot['id']) or {}
        if record.get('generation') == generation and _attempt_matches(record, attempt):
            record.pop('probe_lease', None)
            record['retry_at'] = time.time() + RETRY_BASE_SECONDS
            _write_state(state)


def recovery_available(config, after=None):
    """Read-only current-key proof or due real-request probe authorization.

    Default/aggregate health is not recovery proof for old provider exclusions.
    Does not acquire a probe, clear a circuit, write state or make network calls.
    """
    result = {'available': False, 'healthy': False, 'authorized_probe': False,
              'last_success_at': None, 'configured': False, 'admission_available': False,
              'credential_rotated': False}
    settings = pool_settings(config)
    if not settings:
        return result
    result['configured'] = True
    state = read_state()
    for slot in settings['slots']:
        try:
            loaded_key, generation = _load_key(slot)
            physical = _physical_generation(slot, loaded_key)
        except SlotCredentialError:
            continue
        record = _record(state, slot['id']) or {}
        match = record.get('generation') == generation
        blocked = _slot_is_blocked(record, generation)
        due = blocked and _probe_due(record)
        if not blocked or due:
            result['admission_available'] = True
        success = _number(record.get('last_model_success_at'))
        proven = (match and not blocked and record.get('state') == 'ok' and
                  record.get('success_generation') == generation and _number(after) < success <= time.time() and
                  record.get('success_epoch') == int(_number(record.get('epoch'))))
        rotated = bool(record.get('generation') and not match)
        if proven:
            result['healthy'] = True
            result['last_success_at'] = max(result['last_success_at'] or 0, success)
        if record.get('key_generation') and (record['key_generation'] != physical or
                record.get('generation') == generation and record.get('rotation_from') and
                record['rotation_from'] != physical):
            result['credential_rotated'] = True
        if due or rotated:
            result['authorized_probe'] = True
    result['available'] = result['healthy'] or result['authorized_probe']
    return result


def _record_issue(slot, generation, code, http_status=None):
    """Record a non-blocking error without changing an established block state."""
    now = time.time()

    def mutate(state):
        slots = state.setdefault('slots', {})
        if not isinstance(slots, dict):
            slots = {}
            state['slots'] = slots
        record = slots.get(slot['id'])
        stored = record.get('generation') if isinstance(record, dict) else None
        if stored and generation and stored != generation:
            return
        current = record.get('state') if isinstance(record, dict) else 'ok'
        if current not in BLOCK_STATES:
            current = 'ok'
        fresh = dict(record) if isinstance(record, dict) else {}
        fresh['state'] = current
        if generation:
            fresh['generation'] = generation
        fresh['updated_at'] = now
        fresh['last_error'] = code
        if http_status is None:
            fresh.pop('http_status', None)
        else:
            fresh['http_status'] = int(http_status)
        slots[slot['id']] = fresh

    with common.locked('bailian-pool'):
        state = read_state()
        mutate(state)
        _write_state(state)


def status_snapshot(config):
    """Public, secret-free pool status for the health endpoint and CLI."""
    try:
        settings = pool_settings(config)
    except ValueError as exc:
        return {'enabled': None, 'error': str(exc)}
    if settings is None:
        return {'enabled': False}
    state = read_state()
    slots = []
    available = 0
    for slot in settings['slots']:
        record = _record(state, slot['id'])
        record = record if isinstance(record, dict) else {}
        slot_state = record.get('state') or 'ok'
        if slot_state not in BLOCK_STATES:
            available += 1
        item = {'id': slot['id'], 'state': slot_state}
        if slot_state == 'exhausted':
            item['retry_at'] = _retry_at(record)
            item['probe_due'] = _probe_due(record)
            item['probe_in_flight'] = _number((record.get('probe_lease') or {}).get('until')) > time.time()
        for key in ('kind', 'last_error', 'http_status', 'updated_at'):
            if record.get(key) is not None:
                item[key] = record[key]
        slots.append(item)
    active = state.get('active')
    if active not in {slot['id'] for slot in settings['slots']}:
        active = None
    return {'enabled': True, 'provider': providers.BAILIAN_PROVIDER,
            'port': settings['port'], 'active': active,
            'available': available, 'configured': len(settings['slots']),
            'slots': slots, 'updated_at': state.get('updated_at')}


def _classification(status, body):
    """Classify only explicit pre-response rejection bodies.

    Returns ``(decision, kind)`` where decision is one of ``quota``, ``balance``,
    ``auth``, ``temporary`` or ``other``. No body text is returned, logged or
    persisted.
    """
    text = body.decode('utf-8', 'replace')[:65536].lower()
    fields = [text]
    try:
        value = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        value = None
    if isinstance(value, dict):
        error = value.get('error')
        for item in (value, error):
            if isinstance(item, dict):
                for key in ('type', 'code', 'message', 'status'):
                    if isinstance(item.get(key), str):
                        fields.append(item[key].lower())
    blob = ' '.join(fields)
    quota_markers = (
        'insufficient_quota', 'quota_exceeded', 'quota exceeded', 'quota exhausted',
        'exceeded your current quota', 'credit balance', 'no credits', 'out of credits',
        'credits exhausted', 'allocationquota', 'freetieronly', 'quota limit',
        '额度已用完', '配额已用完', '超出额度',
    )
    balance_markers = (
        'insufficient balance', 'insufficient_balance', 'balance insufficient',
        'balance is insufficient', 'negative balance', 'arrearage', 'arrears',
        'overdue', 'payment required', 'billing_hard_limit', 'account is in arrears',
        '余额不足', '欠费',
    )
    auth_markers = (
        'invalid_api_key', 'invalid api key', 'invalidapikey', 'authentication_error',
        'authentication failed', 'unauthorized', 'permission_error', 'access denied',
        'accessdenied', 'forbidden', 'invalid access key',
    )
    temporary_markers = (
        'rate_limit_error', 'rate limit', 'too many requests', 'throttling',
        'throttled', 'try again',
    )
    if status in (400, 402, 403, 429) and any(marker in blob for marker in balance_markers):
        return 'balance', 'balance'
    if status in (400, 402, 403, 429) and any(marker in blob for marker in quota_markers):
        return 'quota', 'quota'
    if status in (400, 401, 403) and any(marker in blob for marker in auth_markers):
        return 'auth', 'auth'
    if status == 402:
        return 'balance', 'balance'
    if status == 429:
        return 'temporary', 'temporary'
    if status in (401, 403):
        # An explicit 401/403 without a recognised body marker is an
        # authentication/permission rejection, not a transport ambiguity.
        return 'auth', 'auth'
    return 'other', None


def _read_bounded(response, limit):
    body = response.read(limit)
    return body, len(body) < limit


def _response_headers(response):
    return list(response.getheaders())


def _secret_representations(key):
    try:
        return common.secret_representations(key.decode('ascii'))
    except Exception:
        return [key.decode('utf-8', 'replace')]


def _redact_key_bytes(body, key):
    """Remove the resolved slot key and common encodings from an error body."""
    if not body or not key:
        return body
    for representation in sorted(_secret_representations(key), key=len, reverse=True):
        if representation:
            body = body.replace(representation.encode('utf-8'), b'<redacted>')
    return body


def _redact_key_text(value, key):
    if not isinstance(value, str) or not value or not key:
        return value
    for representation in sorted(_secret_representations(key), key=len, reverse=True):
        if representation:
            value = value.replace(representation, '<redacted>')
    return value


def _safe_error_headers(headers, key, drop_encoding=False):
    """Drop credential-bearing response headers and redact echoed key text."""
    safe = []
    for name, value in headers:
        lower = name.lower()
        if lower in SENSITIVE_RESPONSE_HEADERS:
            continue
        if drop_encoding and lower == 'content-encoding':
            continue
        safe.append((name, _redact_key_text(value, key)))
    return safe


def _decode_error_body(body, encoding):
    """Decode a complete error body for inspection; None means do not relay."""
    if encoding in ('', 'identity'):
        return body, False
    if not body:
        return b'', True
    try:
        if encoding in ('gzip', 'x-gzip'):
            return gzip.decompress(body), True
        if encoding == 'deflate':
            try:
                return zlib.decompress(body), True
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS), True
    except Exception:
        return None, True
    # Unknown encodings (for example brotli) cannot be inspected safely.
    return None, True


def _upstream_endpoint():
    base = urllib.parse.urlsplit(providers.BAILIAN_BASE_URL)
    if base.scheme not in ('http', 'https') or not base.hostname:
        raise RuntimeError('fixed upstream base URL is invalid')
    return base


def _open_upstream(path, query, body, headers, key, timeout):
    base = _upstream_endpoint()
    port = base.port or (443 if base.scheme == 'https' else 80)
    if base.scheme == 'https':
        connection = http.client.HTTPSConnection(
            base.hostname, port, timeout=timeout, context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(base.hostname, port, timeout=timeout)
    target = (base.path.rstrip('/') or '') + path
    if query:
        target += '?' + query
    try:
        connection.putrequest('POST', target, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        # endheaders() does not infer a body length in the low-level path; set
        # it explicitly so the upstream receives the full request body.
        connection.putheader('Content-Length', str(len(body)))
        connection.putheader('x-api-key', key.decode('ascii'))
        connection.endheaders(body)
        return connection, connection.getresponse()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise


class RequestError(Exception):
    def __init__(self, status, error_type, message):
        self.status = status
        self.error_type = error_type
        self.message = message
        super().__init__(message)


def _content_block_valid(block):
    if not isinstance(block, dict):
        return False
    kind = block.get('type')
    if kind in ('text', 'thinking'):
        value = block.get(kind)
        return isinstance(value, str) and bool(value.strip())
    if kind == 'tool_use':
        return (isinstance(block.get('id'), str) and bool(block['id'].strip()) and
                isinstance(block.get('name'), str) and bool(block['name'].strip()) and
                isinstance(block.get('input'), dict))
    return False


class _ModelResponseProof:
    """Bounded observer only: preserves wire bytes; HTTP 200/empty blocks are not proof."""
    def __init__(self, response):
        self.stream = 'text/event-stream' in (response.getheader('Content-Type') or '').lower()
        self.length = response.getheader('Content-Length')
        self.encoding = (response.getheader('Content-Encoding') or '').lower()
        self.received = 0
        self.buffer = b''
        self.valid = self.encoding in ('', 'identity')
        self.started = self.stopped = self.stop_reason = False
        self.error_classification = None
        self.blocks = {}
        self.closed_blocks = set()

    def _event(self, value):
        if not isinstance(value, dict):
            self.valid = False
            return
        kind = value.get('type')
        if kind == 'error':
            decision = _classification(400, json.dumps(value).encode())
            error = value.get('error') or {}
            if decision[0] == 'other' and error.get('type') in ('rate_limit_error', 'overloaded_error'):
                decision = ('temporary', 'temporary')
            self.error_classification = decision
            self.valid = False
            return
        if self.stopped:
            self.valid = False
            return
        if kind == 'message_start':
            message = value.get('message') or {}
            if self.started:
                self.valid = False
            self.started = bool(isinstance(message.get('id'), str) and message['id'].strip() and
                                message.get('type') == 'message' and message.get('role') == 'assistant')
            if not self.started:
                self.valid = False
        elif kind in ('content_block_start', 'content_block_delta', 'content_block_stop'):
            if not self.started:
                self.valid = False
                return
            index = value.get('index')
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 1024:
                self.valid = False
                return
            if kind == 'content_block_start':
                block = value.get('content_block')
                if not isinstance(block, dict) or index in self.blocks or block.get('type') not in ('text', 'thinking', 'tool_use'):
                    self.valid = False
                    return
                self.blocks[index] = dict(block)
            elif index not in self.blocks or index in self.closed_blocks:
                self.valid = False
            elif kind == 'content_block_stop':
                block = self.blocks[index]
                partial = block.pop('_partial_json', None)
                if partial is not None:
                    try:
                        block['input'] = json.loads(partial)
                    except ValueError:
                        self.valid = False
                if not _content_block_valid(block):
                    self.valid = False
                self.closed_blocks.add(index)
            else:
                block = self.blocks[index]
                delta = value.get('delta') or {}
                field = {'text_delta': 'text', 'thinking_delta': 'thinking',
                         'input_json_delta': '_partial_json'}.get(delta.get('type'))
                if field is None:
                    if delta.get('type') != 'signature_delta':
                        self.valid = False
                    return
                expected = {'text': 'text', 'thinking': 'thinking', '_partial_json': 'tool_use'}[field]
                text = delta.get('partial_json' if field == '_partial_json' else field)
                if block.get('type') != expected or not isinstance(text, str):
                    self.valid = False
                    return
                block[field] = block.get(field, '') + text
                if len(block[field]) > ERROR_BODY_LIMIT:
                    self.valid = False
        elif kind == 'message_delta':
            reason = (value.get('delta') or {}).get('stop_reason')
            self.stop_reason = isinstance(reason, str) and bool(reason)
        elif kind == 'message_stop':
            self.stopped = True

    def feed(self, chunk):
        self.received += len(chunk)
        if self.received > ERROR_BODY_LIMIT:
            self.valid = False
        if not self.valid:
            return
        self.buffer += chunk
        if len(self.buffer) > ERROR_BODY_LIMIT:
            self.valid = False
            self.buffer = b''
            return
        if self.stream:
            self.buffer = self.buffer.replace(b'\r\n', b'\n')
            while b'\n\n' in self.buffer:
                event, self.buffer = self.buffer.split(b'\n\n', 1)
                lines = [line[5:].lstrip() for line in event.split(b'\n') if line.startswith(b'data:')]
                if not lines:
                    continue
                try:
                    self._event(json.loads(b'\n'.join(lines)))
                except (ValueError, UnicodeDecodeError, TypeError, AttributeError):
                    self.valid = False

    def complete(self):
        if not self.valid or self.length is not None and str(self.received) != self.length:
            return False
        if self.stream:
            return (self.started and bool(self.blocks) and len(self.closed_blocks) == len(self.blocks) and
                    self.stop_reason and self.stopped and not self.buffer.strip())
        try:
            value = json.loads(self.buffer)
            return (isinstance(value, dict) and value.get('type') == 'message' and
                    value.get('role') == 'assistant' and isinstance(value.get('id'), str) and bool(value['id'].strip()) and
                    isinstance(value.get('content'), list) and bool(value['content']) and
                    all(_content_block_valid(b) for b in value['content']) and
                    isinstance(value.get('stop_reason'), str) and bool(value['stop_reason']) and
                    not value.get('error'))
        except (ValueError, UnicodeDecodeError):
            return False


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'BailianPool/1'
    settings = None
    timeout = 30  # A stalled downstream cannot outlive the renewable probe lease.

    def _log_method(self):
        command = getattr(self, 'command', None)
        if command not in ('GET', 'POST', 'HEAD', 'OPTIONS', 'PUT', 'DELETE', 'PATCH'):
            command = '-'
        return command

    def log_request(self, code='-', size='-'):
        # BaseHTTPRequestHandler's default includes the raw request target,
        # which may carry a query string. Log only the method and status.
        log('request %s %s' % (self._log_method(), code))

    def log_message(self, fmt, *args):
        # Called for parser errors and other diagnostics; never echo fmt/args,
        # because those can contain the raw request line or target.
        log('message %s' % self._log_method())

    def log_error(self, fmt, *args):
        log('error %s' % self._log_method())

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('HEAD')

    def do_POST(self):
        self._dispatch('POST')

    def _dispatch(self, method):
        self._response_started = False
        try:
            parts = urllib.parse.urlsplit(self.path)
            if parts.scheme or parts.netloc:
                raise RequestError(400, 'invalid_request_error', 'Absolute request URLs are not accepted')
            if parts.path == '/health':
                if method not in ('GET', 'HEAD'):
                    raise RequestError(405, 'invalid_request_error', 'Method not allowed')
                if not self._authorized():
                    raise RequestError(401, 'authentication_error', 'Invalid local pool credential')
                try:
                    config = common.config()
                except Exception:
                    raise RequestError(503, 'api_error', 'Pool configuration unavailable') from None
                snapshot = status_snapshot(config)
                if snapshot.get('enabled') is not True or snapshot.get('error'):
                    raise RequestError(503, 'api_error', 'Pool is not ready')
                self._json_response(200, snapshot)
                return
            if method != 'POST':
                raise RequestError(405, 'invalid_request_error', 'Method not allowed')
            if parts.path not in ALLOWED_PATHS:
                raise RequestError(404, 'not_found_error', 'Unknown inference path')
            if len(parts.query) > 2048 or any(ord(char) < 0x20 or ord(char) == 0x7f
                                              for char in parts.query):
                raise RequestError(400, 'invalid_request_error', 'Invalid query string')
            if not self._authorized():
                raise RequestError(401, 'authentication_error', 'Invalid local pool credential')
            body = self._read_request_body()
            self._proxy(parts.path, parts.query, body)
        except RequestError as exc:
            # Early errors can leave an unread request body in the socket. Do
            # not let HTTP/1.1 keep-alive parse that JSON as a second request;
            # close the connection after the response.
            self.close_connection = True
            if not self._response_started:
                self._json_error(exc.status, exc.error_type, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            log('internal failure (' + type(exc).__name__ + ')')
            if not self._response_started:
                self._json_error(500, 'api_error', 'Internal pool failure')
            else:
                self.close_connection = True

    def _authorized(self):
        provided = self.headers.get('x-api-key')
        if not provided:
            authorization = self.headers.get('authorization', '')
            if authorization.lower().startswith('bearer '):
                provided = authorization[7:].strip()
        if not provided:
            return False
        try:
            expected = (common.STATE / 'server-password').read_text().strip()
        except OSError:
            log('local server password is unavailable')
            return False
        if not expected:
            return False
        try:
            return hmac.compare_digest(provided.strip().encode('utf-8'),
                                       expected.encode('utf-8'))
        except (UnicodeError, AttributeError):
            return False

    def _read_request_body(self):
        if self.headers.get('transfer-encoding'):
            raise RequestError(400, 'invalid_request_error', 'Chunked request bodies are not accepted')
        values = self.headers.get_all('content-length') or []
        if len(values) != 1:
            raise RequestError(411, 'invalid_request_error', 'Content-Length is required')
        try:
            length = int(values[0])
        except ValueError:
            raise RequestError(400, 'invalid_request_error', 'Invalid Content-Length') from None
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestError(413, 'invalid_request_error', 'Request body too large')
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError(400, 'invalid_request_error', 'Incomplete request body')
        return body

    def _forward_headers(self):
        headers = []
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP or lower in ('host', 'authorization', 'x-api-key',
                                                'content-length', 'transfer-encoding'):
                continue
            headers.append((name, value))
        return headers

    def _proxy(self, path, query, body):
        settings = self.settings
        if not settings:
            self._json_error(503, 'api_error', 'Credential pool is not configured')
            return
        headers = self._forward_headers()
        last_rejection = None
        recoverable_rejection = False
        saw_recoverable = False
        saw_auth = False
        saw_blocked = False
        saw_unavailable = False
        attempted = False
        # Healthy/currently unblocked slots first; a due primary probe must not
        # delay a healthy secondary. Claims still CAS against fresh state.
        initial = read_state()
        loaded = []
        for slot in settings['slots']:
            try:
                key, generation = _load_key(slot)
                record = _record(initial, slot['id']) or {}
                blocked = (_slot_is_blocked(record, generation) or
                           bool(record.get('key_generation') and
                                record['key_generation'] != _physical_generation(slot, key)))
                loaded.append((blocked, slot, key, generation))
            except SlotCredentialError:
                saw_unavailable = True
                _record_issue(slot, None, 'credential_unavailable')
                log('slot ' + slot['id'] + ' credential unavailable')
        for _, slot, key, generation in sorted(loaded, key=lambda item: item[0]):
            attempt = _claim_attempt(slot, generation, settings['timeout'],
                                     allow_probe=path == '/messages', allow_rotation=path == '/messages')
            if attempt is None:
                saw_blocked = True
                current = _record(read_state(), slot['id']) or {}
                saw_recoverable = saw_recoverable or current.get('state') == 'exhausted'
                saw_auth = saw_auth or current.get('state') == 'auth'
                continue
            attempted = True
            connection = None
            response = None
            try:
                connection, response = _open_upstream(
                    path, query, body, headers, key, settings['timeout'])
                status = response.status
                reason = response.reason
                if 300 <= status < 400:
                    _record_issue(slot, generation, 'redirect_refused', status)
                    log('slot ' + slot['id'] + ' refused upstream redirect')
                    self._json_error(502, 'api_error', 'Upstream redirect refused')
                    return
                if status >= 400:
                    prefix, complete = _read_bounded(response, ERROR_BODY_LIMIT)
                    encoding = (response.getheader('Content-Encoding') or '').strip().lower()
                    decoded_encoding = False
                    if encoding not in ('', 'identity'):
                        if not complete:
                            _record_issue(slot, generation, 'upstream_error', status)
                            log('slot ' + slot['id'] + ' compressed upstream error too large')
                            self._json_error(502, 'api_error',
                                             'Compressed upstream error response too large')
                            return
                        prefix, decoded_encoding = _decode_error_body(prefix, encoding)
                        if prefix is None:
                            _record_issue(slot, generation, 'upstream_error', status)
                            log('slot ' + slot['id'] + ' compressed upstream error refused')
                            self._json_error(502, 'api_error',
                                             'Compressed upstream error refused')
                            return
                    decision, kind = _classification(status, prefix)
                    if decision in ('quota', 'balance'):
                        saw_recoverable = True
                        recoverable_rejection = True
                        _mark(slot, generation, 'exhausted', kind=kind, http_status=status, attempt=attempt)
                        log('slot ' + slot['id'] + ' ' + kind + ' exhaustion (HTTP ' + str(status) + ')')
                        if complete:
                            last_rejection = (
                                status, _redact_key_text(reason, key),
                                _safe_error_headers(_response_headers(response), key, decoded_encoding),
                                _redact_key_bytes(prefix, key))
                        else:
                            last_rejection = None
                        continue
                    if decision == 'auth':
                        saw_auth = True
                        _mark(slot, generation, 'auth', kind='auth', http_status=status, attempt=attempt)
                        log('slot ' + slot['id'] + ' authentication rejection (HTTP ' + str(status) + ')')
                        if not recoverable_rejection:
                            if complete:
                                last_rejection = (
                                    status, _redact_key_text(reason, key),
                                    _safe_error_headers(_response_headers(response), key, decoded_encoding),
                                    _redact_key_bytes(prefix, key))
                            else:
                                last_rejection = None
                        continue
                    if not complete:
                        # A partial error body cannot be safely redacted; never
                        # relay it. The request itself was not replayed.
                        _record_issue(slot, generation, 'upstream_error', status)
                        log('slot ' + slot['id'] + ' upstream error body too large')
                        self._json_error(502, 'api_error', 'Upstream error response too large')
                        return
                    if decision == 'temporary':
                        _record_issue(slot, generation, 'temporary_429', status)
                    else:
                        _record_issue(slot, generation, 'upstream_error', status)
                    self._send_buffered(
                        status, _redact_key_text(reason, key),
                        _safe_error_headers(_response_headers(response), key, decoded_encoding),
                        _redact_key_bytes(prefix, key))
                    return
                proof = _ModelResponseProof(response) if path == '/messages' and status == 200 else None
                error_recorded = {'done': False}
                def observe(chunk):
                    _renew_probe(slot, generation, attempt, settings['timeout'])
                    if proof is not None:
                        proof.feed(chunk)
                        if proof.error_classification and not error_recorded['done']:
                            # Persist a complete explicit SSE error BEFORE writing it to
                            # the SDK. A disconnect/truncated stream cannot lose the block.
                            error_recorded['done'] = True
                            decision, kind = proof.error_classification
                            if decision in ('quota', 'balance', 'auth'):
                                _mark(slot, generation, 'auth' if decision == 'auth' else 'exhausted',
                                      kind=kind, http_status=status, attempt=attempt)
                            else:
                                _record_issue(slot, generation, 'temporary_429' if decision == 'temporary' else 'upstream_error', status)
                self._relay(response, status, reason, b'', False, observer=observe)
                if proof is not None and proof.complete():
                    _mark(slot, generation, 'ok', attempt=attempt, verified_model=True)
                elif not 200 <= status < 300:
                    _record_issue(slot, generation, 'upstream_error', status)
                return
            except Exception:
                if self._response_started:
                    # A downstream disconnect or a truncated read after headers
                    # were sent is not an upstream ambiguity and must not be
                    # replayed or converted into a second HTTP response.
                    self.close_connection = True
                    return
                _record_issue(slot, generation, 'transport')
                log('slot ' + slot['id'] + ' transport failure')
                self._json_error(502, 'api_error',
                                 'Upstream transport failed; request was not replayed')
                return
            finally:
                _finish_probe(slot, generation, attempt)
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
        if saw_recoverable and not recoverable_rejection:
            # One seat's auth failure must not turn another seat's recoverable
            # quota cooldown into a permanent provider-level authentication block.
            self._json_error(429, 'rate_limit_error', 'Alibaba credential pool quota exhausted; retry after cooldown')
            return
        if last_rejection is not None:
            status, reason, headers, body = last_rejection
            self._send_buffered(status, reason, headers, body)
            return
        if saw_auth and not saw_recoverable and not saw_unavailable:
            self._json_error(401, 'authentication_error', 'Alibaba credential pool authentication rejected')
        elif saw_unavailable and not attempted:
            self._json_error(503, 'api_error', 'Alibaba credential pool unavailable')
        elif saw_blocked or attempted:
            self._json_error(429, 'rate_limit_error', 'Alibaba credential pool exhausted')
        else:
            self._json_error(503, 'api_error', 'Alibaba credential pool unavailable')

    def _relay(self, response, status, reason, prefix, prefix_complete, observer=None):
        headers = _response_headers(response)
        self.send_response(status, reason)
        for name, value in headers:
            if name.lower() in HOP_BY_HOP or name.lower() in SENSITIVE_RESPONSE_HEADERS:
                continue
            self.send_header(name, value)
        if not any(name.lower() == 'content-length' for name, _ in headers):
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        self._response_started = True
        if self.command == 'HEAD':
            return
        relayed = len(prefix)
        if prefix:
            self.wfile.write(prefix)
        if not prefix_complete:
            while True:
                chunk = response.read1(65536) if hasattr(response, 'read1') else response.read(65536)
                if not chunk:
                    break
                relayed += len(chunk)
                if observer is not None:
                    observer(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        self.wfile.flush()
        length = response.getheader('Content-Length')
        if length is not None and str(relayed) != length:
            # read1 may reach EOF without raising IncompleteRead. Do not leave
            # the downstream waiting on a keep-alive socket for missing bytes.
            self.close_connection = True

    def _send_buffered(self, status, reason, headers, body):
        self.send_response(status, reason)
        for name, value in headers:
            if name.lower() in HOP_BY_HOP or name.lower() == 'content-length' or \
                    name.lower() in SENSITIVE_RESPONSE_HEADERS:
                continue
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self._response_started = True
        if self.command != 'HEAD':
            self.wfile.write(body)
            self.wfile.flush()

    def _json_response(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self._response_started = True
        if self.command != 'HEAD':
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _json_error(self, status, error_type, message):
        self._json_response(status, {'type': 'error',
                                     'error': {'type': error_type, 'message': message}})


def create_server(config, port=None, host='127.0.0.1'):
    settings = pool_settings(config)
    if settings is None:
        raise ValueError('bailian_pool is disabled')
    bind_port = settings['port'] if port is None else port
    if isinstance(bind_port, bool) or not isinstance(bind_port, int) or not 0 <= bind_port <= 65535:
        raise ValueError('invalid pool listen port')
    handler = type('BoundBailianPoolHandler', (ProxyHandler,), {'settings': settings})
    server = ThreadingHTTPServer((host, bind_port), handler)
    server.daemon_threads = True
    return server


def wait_ready(port, password=None, timeout=10):
    if password is None:
        try:
            password = (common.STATE / 'server-password').read_text().strip()
        except OSError:
            return False
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', int(port), timeout=1)
            connection.request('GET', '/health', headers={'x-api-key': password})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return True
        except Exception:
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        time.sleep(0.1)
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else 'run'
    if command in ('status', 'check'):
        try:
            config = common.config()
        except Exception:
            print(json.dumps({'error': 'Configuration unavailable'}))
            return 1
        if command == 'status':
            print(json.dumps(status_snapshot(config), indent=2, sort_keys=True))
            return 0
        try:
            settings = pool_settings(config)
        except ValueError as exc:
            print(json.dumps({'error': str(exc)}))
            return 1
        if settings is None:
            print(json.dumps({'enabled': False}))
            return 0
        print(json.dumps({'enabled': True, 'port': settings['port'],
                          'slots': [{'id': slot['id'], 'source': slot['source']}
                                    for slot in settings['slots']]}, indent=2))
        return 0
    if command != 'run':
        print('usage: bailian_pool.py [run|status|check]', file=sys.stderr)
        return 2
    try:
        config = common.config()
        settings = pool_settings(config)
    except Exception as exc:
        log('startup validation failed: ' + str(exc))
        return 1
    if settings is None:
        log('disabled')
        return 0
    try:
        server = create_server(config)
    except Exception as exc:
        log('startup failed: ' + str(exc))
        return 1
    log('listening on 127.0.0.1:%d with %d slot(s)' %
        (server.server_address[1], len(settings['slots'])))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
