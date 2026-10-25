"""Real signed WebAuthn fixtures and listener assurance; temporary state only."""
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import console
import console_auth as auth
import console_webauthn as pk
import test_console_auth as legacy

try:
    import cbor2
    import webauthn
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    SDK = True
except ImportError:
    SDK = False


@unittest.skipUnless(SDK, 'Requires isolated console runtime')
class SignedPasskeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {'console_passkey': {'enabled': True}}
        self.config_patch = patch('common.config', return_value=self.settings)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        auth.set_user('admin', 'synthetic-password', self.root)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b'synthetic-test-credential'

    def client_data(self, options, kind, origin=None, cross_origin=False):
        return json.dumps({'type': kind, 'challenge': options['publicKey']['challenge'],
                           'origin': origin or pk.ORIGIN, 'crossOrigin': cross_origin}).encode()

    def register(self, options=None, uv=True, origin=None):
        if options is None:
            grant = pk.create_grant(self.root).split('#enroll=')[1]
            options = pk.registration_options(self.root, grant=grant)
        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')}
        data = hashlib.sha256(pk.RP_ID.encode()).digest() + bytes([0x41 | (4 if uv else 0)]) + b'\0'*4
        data += b'\0'*16 + struct.pack('!H', len(self.credential_id)) + self.credential_id + cbor2.dumps(cose)
        credential = {'id': pk.encode(self.credential_id), 'rawId': pk.encode(self.credential_id),
            'type': 'public-key', 'response': {'clientDataJSON': pk.encode(self.client_data(options, 'webauthn.create', origin)),
            'attestationObject': pk.encode(cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': data}))}}
        body = {'ceremony_id': options['ceremony_id'], 'credential': credential, 'name': 'Test device'}
        return pk.register(body, self.root), body

    def assertion(self, options=None, uv=True, origin=None, handle=None, cross_origin=False, sign_count=1):
        options = options or pk.authentication_options(self.root)
        client_data = self.client_data(options, 'webauthn.get', origin, cross_origin)
        data = hashlib.sha256(pk.RP_ID.encode()).digest() + bytes([1 | (4 if uv else 0)]) + struct.pack('!I', sign_count)
        signature = self.key.sign(data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        user_handle = handle if handle is not None else pk._read('webauthn-users.json', self.root)['admin']
        return {'ceremony_id': options['ceremony_id'], 'credential': {
            'id': pk.encode(self.credential_id), 'rawId': pk.encode(self.credential_id), 'type': 'public-key',
            'response': {'clientDataJSON': pk.encode(client_data), 'authenticatorData': pk.encode(data),
                         'signature': pk.encode(signature), 'userHandle': user_handle}}}

    def test_real_signature_round_trip_and_replay(self):
        result, registration = self.register()
        self.assertEqual(result, {'registered': True})
        self.assertEqual(auth._read_sessions(self.root), {})
        with self.assertRaises(ValueError):
            pk.register(registration, self.root)
        assertion = self.assertion()
        token = pk.authenticate(assertion, self.root)
        context = auth.session_context(token, self.root, require_passkey=True)
        self.assertEqual(context['auth_method'], 'webauthn')
        self.assertTrue(context['user_verified'])
        with self.assertRaises(ValueError):
            pk.authenticate(assertion, self.root)
        with self.assertRaises(ValueError):
            pk.delete(pk.encode(self.credential_id), token, self.root, public=True)

    def test_wrong_uv_origin_handle_signature_and_cross_origin(self):
        self.register()
        cases = [dict(uv=False), dict(origin='https://wrong.example'), dict(handle=pk.encode(b'wrong')),
                 dict(cross_origin=True)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pk.authenticate(self.assertion(**changes), self.root)
        body = self.assertion()
        body['credential']['response']['signature'] = pk.encode(b'invalid')
        with self.assertRaises(ValueError):
            pk.authenticate(body, self.root)

    def test_registration_rejects_wrong_origin_and_missing_uv(self):
        for kwargs in (dict(uv=False), dict(origin='https://wrong.example')):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.register(**kwargs)

    def test_registration_grant_one_use_expiry_and_no_session(self):
        grant = pk.create_grant(self.root).split('#enroll=')[1]
        self.assertNotIn(grant, (self.root/'console-auth/enrollment-grants.json').read_text())
        options = pk.registration_options(self.root, grant=grant)
        with self.assertRaises(ValueError):
            pk.registration_options(self.root, grant=grant)
        with self.assertRaises(ValueError):
            pk.registration_options(self.root)
        with patch.object(pk.time, 'time', return_value=time.time() + 700), self.assertRaises(ValueError):
            self.register(options)
        self.assertEqual(auth._read_sessions(self.root), {})

    def test_rotation_invalidates_inflight_ceremony_and_sessions(self):
        self.register()
        token = pk.authenticate(self.assertion(), self.root)
        assertion = self.assertion(sign_count=2)
        auth.set_user('admin', 'replacement-password', self.root)
        self.assertIsNone(auth.session_context(token, self.root, True))
        with self.assertRaises(ValueError):
            pk.authenticate(assertion, self.root)

    def test_deletion_revokes_live_identity_and_sessions(self):
        self.register()
        token = pk.authenticate(self.assertion(), self.root)
        pk.delete(pk.encode(self.credential_id), token, self.root, public=False)
        self.assertIsNone(auth.session_context(token, self.root, True))

    def test_concurrent_challenge_consumption_has_one_winner(self):
        self.register()
        assertion = self.assertion()
        result = []
        def verify():
            try:
                result.append(bool(pk.authenticate(assertion, self.root)))
            except ValueError:
                result.append(False)
        threads = [threading.Thread(target=verify) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(sorted(result), [False, True])


class ListenerTests(unittest.TestCase):
    def test_dual_listener_roles_origins_and_cookie_isolation(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        settings = {'console_url': 'http://127.0.0.1:0', 'console_bind': '127.0.0.1',
                    'console_passkey': {'enabled': True, 'public_origin': 'https://worker.example.com'},
                    'console_public_port': port}
        # console_url normally requires a nonzero port; reserve it for this real bind test.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); lan_port = probe.getsockname()[1]
        settings['console_url'] = 'http://127.0.0.1:' + str(lan_port)
        servers = console.build_servers(settings)
        try:
            self.assertEqual([server.listener_role for server in servers], ['lan', 'public'])
            self.assertEqual(servers[0].auth_root, servers[1].auth_root)
            self.assertNotEqual(servers[0].cookie_name, servers[1].cookie_name)
            self.assertEqual(servers[1].allowed_origins, [('https', 'worker.example.com', 443)])
        finally:
            for server in servers: server.server_close()

    def test_rp_settings_are_operator_configured_and_exact(self):
        self.assertEqual(pk.rp_settings({'console_passkey': {'public_origin': 'https://worker.example.com'}}),
                         ('worker.example.com', 'https://worker.example.com'))
        for origin in ('http://worker.example.com', 'https://127.0.0.1', 'https://worker.example.com:444'):
            with self.assertRaises(ValueError):
                pk.rp_settings({'console_passkey': {'public_origin': origin}})


class PublicHttpTests(unittest.TestCase):
    setUp = legacy.ConsoleHttpTests.setUp
    tearDown = legacy.ConsoleHttpTests.tearDown
    write_config = legacy.ConsoleHttpTests.write_config
    start_server = legacy.ConsoleHttpTests.start_server
    call = legacy.ConsoleHttpTests.call
    login = legacy.ConsoleHttpTests.login
    cookie = legacy.ConsoleHttpTests.cookie
    fake_upstream = legacy.ConsoleHttpTests.fake_upstream

    def public(self):
        self.server.listener_role = 'public'
        self.server.cookie_name = '__Host-workerdesk_session'
        self.server.allowed_origins = [('https', pk.RP_ID, 443)]
        self.config['console_passkey'] = {'enabled': True}
        self.write_config()
        return {'Host': pk.RP_ID, 'Origin': pk.ORIGIN}

    def test_password_cookie_cannot_be_relabelled_for_any_public_route(self):
        self.start_server()
        token = self.cookie(self.login()).split('=', 1)[1]
        headers = self.public()
        headers['Cookie'] = '__Host-workerdesk_session=' + token
        for method, path in [('GET', '/console'), ('GET', '/console-api/state'), ('GET', '/session'),
                             ('POST', '/session'), ('PATCH', '/session'), ('DELETE', '/session')]:
            self.assertEqual(self.call(method, path, headers=headers)['status'], 401)
        headers.update(Upgrade='websocket', Connection='Upgrade')
        self.assertEqual(self.call('GET', '/event', headers=headers)['status'], 401)
        self.assertFalse(json.loads(self.call('GET', '/console-api/auth/status', headers=headers)['body'])['authenticated'])
        self.assertEqual(self.login(headers=headers)['status'], 403)

    def test_public_cannot_spoof_lan_host_or_forwarded_identity(self):
        self.start_server()
        self.public()
        headers = {'Host': '127.0.0.1:' + str(self.server.server_port), 'X-Forwarded-For': '192.168.1.10'}
        self.assertEqual(self.login(headers=headers)['status'], 403)

    def test_cross_site_entry_navigation_allows_enrollment_not_apis(self):
        (self.web / 'passkeys.html').write_text('<html>Passkey enrollment</html>')
        self.start_server()
        self.public()
        headers = {'Host': pk.RP_ID, 'Sec-Fetch-Site': 'cross-site',
                   'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
        for path in ('/console-login', '/console-passkeys'):
            self.assertEqual(self.call('GET', path, headers=headers)['status'], 200)
        self.assertEqual(self.call('GET', '/console-api/state', headers=headers)['status'], 403)
        self.assertEqual(self.call('POST', '/console-api/auth/passkey/authenticate/options', {}, headers)['status'], 403)
        headers['Sec-Fetch-Mode'] = 'cors'
        self.assertEqual(self.call('GET', '/console-passkeys', headers=headers)['status'], 403)
        headers['Sec-Fetch-Mode'] = 'navigate'
        headers['Origin'] = 'https://untrusted.example.com'
        self.assertEqual(self.call('GET', '/console-passkeys', headers=headers)['status'], 403)

    def test_missing_sdk_public_authentication_fails_closed(self):
        self.start_server()
        headers = self.public()
        with patch.object(pk, 'sdk', side_effect=auth.AuthStateError('Unavailable')):
            response = self.call('POST', '/console-api/auth/passkey/authenticate/options', {}, headers)
            self.assertEqual(response['status'], 503)
            status = json.loads(self.call('GET', '/console-api/auth/status', headers=headers)['body'])
            self.assertFalse(status['authenticated'])
            self.assertFalse(status['passkey_available'])

    def test_lan_password_reauthentication_creates_only_registration_grant(self):
        self.start_server()
        self.config['console_passkey'] = {'enabled': True}
        self.write_config()
        cookie = self.cookie(self.login())
        response = self.call('POST', '/console-api/auth/enrollment-grant', {'password': 'wrong'}, {'Cookie': cookie})
        self.assertEqual(response['status'], 401)
        response = self.call('POST', '/console-api/auth/enrollment-grant', {'password': 'synthetic-pass'}, {'Cookie': cookie})
        self.assertEqual(response['status'], 200)
        self.assertNotIn('set-cookie', response['headers'])
        token = json.loads(response['body'])['enrollment_url'].split('#enroll=')[1]
        self.assertIsNone(auth.session_context(token, self.state))

    def test_websocket_upgrade_filters_headers_and_preserves_first_frame_and_error_body(self):
        self.start_server()
        cookie = self.cookie(self.login())
        for status, extra, body in ((101, b'Upgrade: websocket\r\nConnection: Upgrade\r\n', b'\x81\x02ok'),
                                    (401, b'Content-Length: 6\r\n', b'denied')):
            with self.subTest(status=status):
                response = ('HTTP/1.1 %d Test\r\n' % status).encode() + extra
                response += b'Set-Cookie: upstream=unsafe\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Credentials: true\r\n\r\n' + body
                port, listener = self.fake_upstream(response)
                self.config['server_url'] = 'http://127.0.0.1:' + str(port)
                self.write_config()
                try:
                    with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as client:
                        client.sendall(('GET /session HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nCookie: %s\r\n\r\n' % (self.server.server_port, cookie)).encode())
                        result = b''
                        while body not in result:
                            result += client.recv(4096)
                        headers, actual_body = result.split(b'\r\n\r\n', 1)
                        self.assertIn(str(status).encode(), headers)
                        self.assertNotIn(b'set-cookie', headers.lower())
                        self.assertNotIn(b'access-control-', headers.lower())
                        self.assertEqual(actual_body, body)
                finally:
                    listener.close()

    @unittest.skipUnless(SDK, 'Requires isolated console runtime')
    def test_public_idle_streams_close_when_credential_is_revoked(self):
        self.start_server()
        headers = self.public()
        for websocket in (False, True):
            with self.subTest(websocket=websocket):
                response = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n'
                            if websocket else b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n')
                port, listener = self.fake_upstream(response)
                self.config['server_url'] = 'http://127.0.0.1:' + str(port)
                self.write_config()
                pk._write('passkeys.json', {'fixture': {'id': 'fixture', 'username': 'admin'}}, self.state)
                with auth._locked(self.state):
                    token = auth._issue_session_locked(auth._read_account(self.state), self.state,
                                                       method='webauthn', user_verified=True, credential_id='fixture')
                try:
                    with patch.object(console, 'STREAM_AUTH_INTERVAL', .2), patch.object(console, 'STREAM_IO_TIMEOUT', .1):
                        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as client:
                            request = 'GET /event HTTP/1.1\r\nHost: %s\r\nOrigin: %s\r\nCookie: __Host-workerdesk_session=%s\r\n' % (pk.RP_ID, pk.ORIGIN, token)
                            if websocket:
                                request += 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                            client.sendall((request + '\r\n').encode())
                            result = b''
                            while b'\r\n\r\n' not in result:
                                result += client.recv(4096)
                            self.assertIn(b'101' if websocket else b'200', result)
                            pk._write('passkeys.json', {}, self.state)
                            self.assertEqual(client.recv(4096), b'')
                finally:
                    listener.close()


if __name__ == '__main__':
    unittest.main()
