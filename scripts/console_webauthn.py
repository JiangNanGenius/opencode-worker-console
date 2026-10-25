"""Single-admin WebAuthn ceremonies. Private keys never leave authenticators.

The maintained webauthn SDK performs all CBOR/COSE, origin, RP and signature
verification. Durable challenges are atomically consumed before verification.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import ipaddress

import console_auth as auth

RP_ID = 'desk.example.com'
ORIGIN = 'https://' + RP_ID
CHALLENGE_TTL = 180
GRANT_TTL = 600
RECENT_SECONDS = 300
MAX_CREDENTIALS = 20
MAX_CHALLENGES = 128


def rp_settings(settings=None):
    """Only operator configuration selects the RP; request headers never do."""
    if settings is None:
        import common
        settings = common.config()
    value = settings.get('console_passkey', {})
    origin = auth.validate_origin(value.get('public_origin', ORIGIN))
    parsed = urllib.parse.urlsplit(origin)
    rp_id = value.get('rp_id', parsed.hostname)
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or rp_id != parsed.hostname:
        raise ValueError('Passkey public_origin must be HTTPS on port 443 and rp_id must equal its hostname')
    try:
        ipaddress.ip_address(rp_id)
    except ValueError:
        if '.' not in rp_id:
            raise ValueError('Passkey RP must be a public DNS hostname')
    else:
        raise ValueError('Passkey RP must be a DNS hostname')
    return rp_id, origin


def enabled(config):
    value = config.get('console_passkey')
    return isinstance(value, dict) and value.get('enabled') is True


def sdk():
    try:
        import webauthn
        from webauthn.helpers import structs
    except ImportError:
        raise auth.AuthStateError('Passkey verification runtime is unavailable') from None
    return webauthn, structs


def available():
    try:
        sdk()
        return True
    except auth.AuthStateError:
        return False


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    if not isinstance(value, str) or len(value) > 16384:
        raise ValueError('Invalid encoded credential')
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, TypeError):
        raise ValueError('Invalid encoded credential') from None


def _top_level_credential(credential):
    try:
        data = json.loads(decode(credential['response']['clientDataJSON']))
        if data.get('crossOrigin', False) is not False or data.get('topOrigin') is not None:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError('Passkeys must be used from the top-level HTTPS site') from None


def _path(name, root):
    return auth._auth_dir(root) / name


def _read(name, root):
    path = _path(name, root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('entries'), dict):
            raise ValueError()
        return value['entries']
    except (OSError, ValueError):
        raise auth.AuthStateError('Passkey state is unavailable') from None


def _write(name, entries, root):
    auth._write_private(_path(name, root), {'version': 1, 'entries': entries})


def _account(root):
    account = auth._read_account(root)
    if account is None:
        raise auth.AuthStateError('Console account is missing')
    return account


def _live_entries(name, root):
    now = time.time()
    return {key: value for key, value in _read(name, root).items()
            if isinstance(value, dict) and isinstance(value.get('expires_at'), (int, float))
            and value['expires_at'] > now}


def credential_active(credential_id, username, root=None):
    try:
        entry = _read('passkeys.json', root).get(credential_id)
        return isinstance(entry, dict) and entry.get('username') == username
    except (auth.AuthStateError, TypeError):
        return False


def listing(root=None):
    username = _account(root)['username']
    return [{key: entry.get(key) for key in ('id', 'name', 'created_at', 'last_used_at', 'backup_eligible', 'backed_up', 'transports')}
            for entry in _read('passkeys.json', root).values()
            if isinstance(entry, dict) and entry.get('username') == username]


def create_grant(root=None):
    """Local CLI or recently password-reauthenticated LAN caller only."""
    with auth._locked(root):
        account = _account(root)
        token = secrets.token_urlsafe(32)
        grants = _live_entries('enrollment-grants.json', root)
        if len(grants) >= 16:
            raise ValueError('Too many active enrollment links')
        grants[auth._token_digest(token)] = {'account': auth._account_fingerprint(account),
                                            'expires_at': time.time() + GRANT_TTL, 'rp': list(rp_settings())}
        _write('enrollment-grants.json', grants, root)
    return rp_settings()[1] + '/console-passkeys#enroll=' + token


def _challenge(purpose, root, session_token=None, grant=None):
    """Caller holds auth lock. No plaintext grant/session token is persisted."""
    account = _account(root)
    entries = _live_entries('webauthn-challenges.json', root)
    if len(entries) >= MAX_CHALLENGES:
        raise ValueError('Too many authentication requests')
    challenge = secrets.token_bytes(32)
    ceremony_id = secrets.token_urlsafe(24)
    entry = {'purpose': purpose, 'challenge': encode(challenge), 'account': auth._account_fingerprint(account),
             'expires_at': time.time() + CHALLENGE_TTL, 'rp': list(rp_settings())}
    if grant is not None:
        if not isinstance(grant, str) or not 20 <= len(grant) <= 128:
            raise ValueError('Enrollment link is invalid or expired')
        grants = _live_entries('enrollment-grants.json', root)
        digest = auth._token_digest(grant)
        stored = grants.pop(digest, None)
        if stored is None or stored.get('account') != entry['account'] or stored.get('rp') != entry['rp']:
            raise ValueError('Enrollment link is invalid or expired')
        _write('enrollment-grants.json', grants, root)
        entry['authorization'] = 'grant'
    elif purpose == 'register':
        require_recent(session_token, root)
        entry['session'] = auth._token_digest(session_token)
        entry['authorization'] = 'session'
    entries[ceremony_id] = entry
    _write('webauthn-challenges.json', entries, root)
    return ceremony_id, challenge


def _consume(ceremony_id, purpose, root, session_token=None):
    if not isinstance(ceremony_id, str) or len(ceremony_id) > 128:
        raise ValueError('Authentication request is invalid or expired')
    entries = _live_entries('webauthn-challenges.json', root)
    entry = entries.pop(ceremony_id, None)
    _write('webauthn-challenges.json', entries, root)
    if entry is None or entry.get('purpose') != purpose or \
            entry.get('account') != auth._account_fingerprint(_account(root)) or entry.get('rp') != list(rp_settings()):
        raise ValueError('Authentication request is invalid or expired')
    if entry.get('authorization') == 'session':
        require_recent(session_token, root)
        if not secrets.compare_digest(entry.get('session', ''), auth._token_digest(session_token)):
            raise ValueError('Authentication session changed')
    return decode(entry['challenge'])


def require_recent(token, root=None):
    context = auth.session_context(token, root, require_passkey=True)
    if context is None or time.time() - context.get('authenticated_at', 0) > RECENT_SECONDS:
        raise ValueError('Please sign in again with a passkey before managing credentials')
    return context


def registration_options(root=None, session_token=None, grant=None):
    library, structs = sdk()
    with auth._locked(root):
        account = _account(root)
        credentials = _read('passkeys.json', root)
        if len(credentials) >= MAX_CREDENTIALS:
            raise ValueError('Passkey limit reached')
        handles = _read('webauthn-users.json', root)
        username = account['username']
        if username not in handles:
            handles[username] = encode(secrets.token_bytes(32))
            _write('webauthn-users.json', handles, root)
        ceremony_id, challenge = _challenge('register', root, session_token, grant)
        options = library.generate_registration_options(
            rp_id=rp_settings()[0], rp_name='Worker Desk', user_name=username,
            user_id=decode(handles[username]), challenge=challenge,
            exclude_credentials=[structs.PublicKeyCredentialDescriptor(id=decode(entry['id']))
                                 for entry in credentials.values() if entry.get('username') == username],
            authenticator_selection=structs.AuthenticatorSelectionCriteria(
                resident_key=structs.ResidentKeyRequirement.REQUIRED,
                user_verification=structs.UserVerificationRequirement.REQUIRED))
        return {'ceremony_id': ceremony_id, 'publicKey': json.loads(library.options_to_json(options))}


def register(body, root=None, session_token=None):
    library, _ = sdk()
    with auth._locked(root):
        challenge = _consume(body.get('ceremony_id'), 'register', root, session_token)
        _top_level_credential(body.get('credential'))
        try:
            result = library.verify_registration_response(credential=body.get('credential'),
                expected_challenge=challenge, expected_rp_id=rp_settings()[0], expected_origin=rp_settings()[1],
                require_user_verification=True)
        except Exception:
            raise ValueError('Passkey registration could not be verified') from None
        entries = _read('passkeys.json', root)
        credential_id = encode(result.credential_id)
        if credential_id in entries or len(entries) >= MAX_CREDENTIALS:
            raise ValueError('Passkey already registered or limit reached')
        name = body.get('name', 'Passkey')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Passkey name must contain 1-80 characters')
        transports = body['credential']['response'].get('transports', [])
        allowed_transports = ('usb', 'nfc', 'ble', 'internal', 'hybrid', 'smart-card')
        if not isinstance(transports, list) or len(transports) > 8 or any(value not in allowed_transports for value in transports):
            raise ValueError('Invalid authenticator transports')
        entries[credential_id] = {'id': credential_id, 'username': _account(root)['username'],
            'name': name.strip(), 'public_key': encode(result.credential_public_key),
            'sign_count': result.sign_count, 'created_at': time.time(), 'last_used_at': None,
            'transports': transports,
            'backup_eligible': str(result.credential_device_type) == 'CredentialDeviceType.MULTI_DEVICE',
            'backed_up': result.credential_backed_up}
        _write('passkeys.json', entries, root)
    # Enrollment grants intentionally do not issue any authenticated session.
    return {'registered': True}


def authentication_options(root=None):
    library, structs = sdk()
    with auth._locked(root):
        ceremony_id, challenge = _challenge('authenticate', root)
        options = library.generate_authentication_options(rp_id=rp_settings()[0], challenge=challenge,
            user_verification=structs.UserVerificationRequirement.REQUIRED)
        return {'ceremony_id': ceremony_id, 'publicKey': json.loads(library.options_to_json(options))}


def authenticate(body, root=None):
    library, _ = sdk()
    with auth._locked(root):
        challenge = _consume(body.get('ceremony_id'), 'authenticate', root)
        credential = body.get('credential')
        _top_level_credential(credential)
        if not isinstance(credential, dict):
            raise ValueError('Passkey authentication failed')
        credential_id = encode(decode(credential.get('id')))
        entries = _read('passkeys.json', root)
        entry = entries.get(credential_id)
        account = _account(root)
        if not isinstance(entry, dict) or entry.get('username') != account['username']:
            raise ValueError('Passkey authentication failed')
        response = credential.get('response')
        handles = _read('webauthn-users.json', root)
        if not isinstance(response, dict) or not isinstance(response.get('userHandle'), str) or \
                not secrets.compare_digest(decode(response['userHandle']), decode(handles.get(account['username']))):
            raise ValueError('Passkey authentication failed')
        try:
            result = library.verify_authentication_response(credential=credential,
                expected_challenge=challenge, expected_rp_id=rp_settings()[0], expected_origin=rp_settings()[1],
                credential_public_key=decode(entry['public_key']),
                credential_current_sign_count=entry['sign_count'], require_user_verification=True)
        except Exception:
            raise ValueError('Passkey authentication failed') from None
        if (str(result.credential_device_type) == 'CredentialDeviceType.MULTI_DEVICE') != entry.get('backup_eligible'):
            raise ValueError('Passkey backup eligibility changed')
        entry.update(sign_count=result.new_sign_count, last_used_at=time.time(),
                     backed_up=result.credential_backed_up)
        _write('passkeys.json', entries, root)
        return auth._issue_session_locked(account, root, method='webauthn',
                                         user_verified=True, credential_id=credential_id)


def delete(credential_id, token, root=None, public=True):
    with auth._locked(root):
        context = require_recent(token, root)
        entries = _read('passkeys.json', root)
        mine = [value for value in entries.values() if value.get('username') == context['username']]
        entry = entries.get(credential_id)
        if not isinstance(entry, dict) or entry.get('username') != context['username']:
            raise ValueError('Unknown passkey')
        if public and len(mine) <= 1:
            raise ValueError('The last passkey cannot be removed through the public site')
        entries.pop(credential_id)
        _write('passkeys.json', entries, root)
        sessions = {key: value for key, value in auth._read_sessions(root).items()
                    if value.get('credential_id') != credential_id}
        auth._write_sessions(sessions, root)
    return {'deleted': True}
