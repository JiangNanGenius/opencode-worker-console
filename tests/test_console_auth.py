"""Server-side console authentication, session and network configuration tests.

All credentials are synthetic and all state is a temporary directory. No real
account, password, cookie or session registry is read or written.
"""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import console
import console_auth


class ConsoleAuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_account_is_idempotent_and_never_resets_existing(self):
        first = console_auth.ensure_default_account(self.state)
        self.assertEqual(first['username'], 'admin')
        self.assertTrue(console_auth.verify_credentials('admin', 'admin', self.state))
        before = (self.state / 'console-auth' / 'account.json').read_bytes()
        console_auth.ensure_default_account(self.state)
        self.assertEqual((self.state / 'console-auth' / 'account.json').read_bytes(), before)
        console_auth.set_user('owner', 'rotated-pass', self.state)
        console_auth.ensure_default_account(self.state)
        self.assertFalse(console_auth.verify_credentials('admin', 'admin', self.state))
        self.assertTrue(console_auth.verify_credentials('owner', 'rotated-pass', self.state))

    def test_set_user_stores_only_salt_and_hash_with_private_modes(self):
        metadata = console_auth.set_user('admin', 'synthetic-pass', self.state)
        self.assertEqual(metadata['username'], 'admin')
        self.assertNotIn('hash', metadata)
        self.assertNotIn('salt', metadata)
        self.assertNotIn('password', metadata)
        directory = self.state / 'console-auth'
        account = directory / 'account.json'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(account.stat().st_mode), 0o600)
        self.assertGreaterEqual(metadata['iterations'], console_auth.MIN_ITERATIONS)
        raw = account.read_text()
        self.assertNotIn('synthetic-pass', raw)
        record = json.loads(raw)
        self.assertNotIn('password', record)
        self.assertEqual(record['algorithm'], 'pbkdf2_hmac_sha256')
        self.assertGreaterEqual(len(bytes.fromhex(record['salt'])), 16)
        self.assertEqual(len(bytes.fromhex(record['hash'])), 32)
        self.assertEqual(stat.S_IMODE((directory / 'sessions.json').stat().st_mode), 0o600)

    def test_malformed_state_is_refused_not_replaced(self):
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        account = self.state / 'console-auth' / 'account.json'
        account.write_text('{not json')
        with self.assertRaises(console_auth.AuthStateError):
            console_auth.ensure_default_account(self.state)
        with self.assertRaises(console_auth.AuthStateError):
            console_auth.verify_credentials('admin', 'admin', self.state)
        account.write_text(json.dumps({'version': 1, 'username': 'admin', 'algorithm': 'pbkdf2_hmac_sha256',
                                       'iterations': 1, 'salt': '00', 'hash': '00'}))
        with self.assertRaises(console_auth.AuthStateError):
            console_auth.verify_credentials('admin', 'admin', self.state)

    def test_unknown_username_still_runs_the_password_hash(self):
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        with patch.object(console_auth, '_hash_password', wraps=console_auth._hash_password) as hashed:
            self.assertFalse(console_auth.verify_credentials('ghost', 'synthetic-pass', self.state))
            self.assertEqual(hashed.call_count, 1)

    def test_password_rotation_invalidates_all_sessions(self):
        console_auth.set_user('admin', 'first-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        self.assertEqual(console_auth.session_username(token, self.state), 'admin')
        console_auth.set_user('admin', 'second-pass', self.state)
        self.assertIsNone(console_auth.session_username(token, self.state))

    def test_session_expiry_and_revocation(self):
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        sessions = console_auth._read_sessions(self.state)
        digest = console_auth._token_digest(token)
        self.assertIn(digest, sessions)
        sessions[digest]['expires_at'] = 1
        console_auth._write_sessions(sessions, self.state)
        self.assertIsNone(console_auth.session_username(token, self.state))
        fresh = console_auth.create_session('admin', self.state)
        self.assertTrue(console_auth.revoke_session(fresh, self.state))
        self.assertIsNone(console_auth.session_username(fresh, self.state))
        self.assertFalse(console_auth.revoke_session('not-a-token', self.state))

    def test_throttle_admission_locks_peer_and_persists_across_reload(self):
        for _ in range(console_auth.MAX_LOGIN_FAILURES):
            self.assertEqual(console_auth.admit_login_attempt('203.0.113.7', self.state), 0)
        self.assertGreater(console_auth.admit_login_attempt('203.0.113.7', self.state), 0)
        self.assertEqual(console_auth.admit_login_attempt('203.0.113.8', self.state), 0)
        # A read from disk (new process/module state) still sees the lock.
        self.assertGreater(console_auth.admit_login_attempt('203.0.113.7', self.state), 0)
        console_auth.record_login_success('203.0.113.7', self.state)
        self.assertEqual(console_auth.admit_login_attempt('203.0.113.7', self.state), 0)

    def test_concurrent_admission_never_exceeds_cap(self):
        admitted = []
        barrier = threading.Barrier(12)

        def attempt():
            barrier.wait(5)
            admitted.append(console_auth.admit_login_attempt('198.51.100.5', self.state))

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(1 for value in admitted if value == 0), console_auth.MAX_LOGIN_FAILURES)

    def test_rotation_race_binds_session_to_checked_revision(self):
        console_auth.set_user('admin', 'old-pass', self.state)
        real_hash = console_auth._hash_password
        started, release = threading.Event(), threading.Event()
        issued = {}

        def slow_hash(password, salt, iterations):
            started.set()
            release.wait(5)
            return real_hash(password, salt, iterations)

        with patch.object(console_auth, '_hash_password', side_effect=slow_hash):
            login = threading.Thread(target=lambda: issued.update(
                token=console_auth.authenticate('admin', 'old-pass', self.state)))
            login.start()
            self.assertTrue(started.wait(5))
            rotation = threading.Thread(target=lambda: console_auth.set_user('admin', 'new-pass', self.state))
            rotation.start()
            release.set()
            login.join(10)
            rotation.join(10)
        self.assertFalse(login.is_alive())
        self.assertFalse(rotation.is_alive())
        token = issued.get('token')
        self.assertTrue(token)
        # The rotation completed after verification; no token may outlive it.
        self.assertIsNone(console_auth.session_username(token, self.state))
        self.assertFalse(console_auth.verify_credentials('admin', 'old-pass', self.state))

    def test_missing_account_after_initialization_is_not_recreated(self):
        console_auth.ensure_default_account(self.state)
        marker = self.state / 'console-auth' / console_auth.INIT_MARKER_FILE
        self.assertTrue(marker.exists())
        (self.state / 'console-auth' / 'account.json').unlink()
        with self.assertRaises(console_auth.AuthStateError):
            console_auth.ensure_default_account(self.state)
        self.assertFalse((self.state / 'console-auth' / 'account.json').exists())
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        self.assertTrue(marker.exists())

    def test_present_malformed_marker_fails_closed(self):
        directory = self.state / 'console-auth'
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / console_auth.INIT_MARKER_FILE
        # A present but empty/parsed-invalid marker must raise, never read as
        # "not initialized" (which would allow the default to be recreated).
        for malformed in ({}, {'version': 2}, {'unexpected': True}, [], 'x', 0, None):
            marker.write_text(json.dumps(malformed))
            with self.assertRaises(console_auth.AuthStateError):
                console_auth._is_initialized(self.state)
            with self.assertRaises(console_auth.AuthStateError):
                console_auth.ensure_default_account(self.state)
            self.assertFalse((directory / console_auth.ACCOUNT_FILE).exists())
        # Unparseable content and a valid marker behave as before.
        marker.write_text('{not json')
        with self.assertRaises(console_auth.AuthStateError):
            console_auth._is_initialized(self.state)
        marker.write_text(json.dumps({'version': 1, 'initialized_at': 1}))
        self.assertTrue(console_auth._is_initialized(self.state))
        marker.unlink()
        self.assertFalse(console_auth._is_initialized(self.state))
        # A valid account plus a present-but-malformed marker still fails closed
        # during initialization instead of ignoring the corruption.
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        marker.write_text(json.dumps({}))
        with self.assertRaises(console_auth.AuthStateError):
            console_auth.ensure_default_account(self.state)
        self.assertTrue(console_auth.verify_credentials('admin', 'synthetic-pass', self.state))

    def test_session_is_bound_to_account_revision(self):
        console_auth.set_user('admin', 'first-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        self.assertEqual(console_auth.session_username(token, self.state), 'admin')
        # Simulate a crash between writing a new account and clearing sessions:
        # the account file is replaced while sessions.json still holds the old
        # token. The fingerprint mismatch must invalidate it.
        console_auth._write_account('admin', 'second-pass', self.state)
        self.assertIsNone(console_auth.session_username(token, self.state))
        # A missing account invalidates every session too.
        console_auth.set_user('admin', 'third-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        account = self.state / 'console-auth' / 'account.json'
        account.unlink()
        self.assertIsNone(console_auth.session_username(token, self.state))
        # As does a corrupt account.
        console_auth._write_account('admin', 'fourth-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        account.write_text('{broken')
        self.assertIsNone(console_auth.session_username(token, self.state))
        # Legacy entries without a bound fingerprint are never trusted.
        console_auth.set_user('admin', 'fifth-pass', self.state)
        token = console_auth.create_session('admin', self.state)
        sessions = console_auth._read_sessions(self.state)
        sessions[console_auth._token_digest(token)].pop('account')
        console_auth._write_sessions(sessions, self.state)
        self.assertIsNone(console_auth.session_username(token, self.state))

    def test_malformed_unicode_password_is_rejected_without_traceback(self):
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        lone = '\udcff'
        self.assertFalse(console_auth.verify_credentials('admin', lone, self.state))
        self.assertIsNone(console_auth.authenticate('admin', lone, self.state))
        with self.assertRaises(ValueError):
            console_auth.set_user('admin', lone, self.state)

        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps({'server_url': 'http://127.0.0.1:1',
                                           'console_url': 'http://127.0.0.1:2',
                                           'profiles': {'fallback': {'model': 'acme/worker'}},
                                           'custom': {'keep': True}}))
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        result = console_auth.configure(bind='0.0.0.0', origins=['https://desk.example.test'],
                                        config_path=config_path)
        self.assertEqual(result['console_bind'], '0.0.0.0')
        self.assertEqual(result['console_allowed_origins'], ['https://desk.example.test'])
        stored = json.loads(config_path.read_text())
        self.assertEqual(stored['custom'], {'keep': True})
        self.assertEqual(stored['profiles'], {'fallback': {'model': 'acme/worker'}})
        self.assertTrue(console_auth.verify_credentials('admin', 'synthetic-pass', self.state))
        trusted = console_auth.configure(trusted_proxies=['127.0.0.1', '10.0.0.0/8'], config_path=config_path)
        self.assertEqual(trusted['console_trusted_proxies'], ['127.0.0.1/32', '10.0.0.0/8'])
        cleared = console_auth.configure(clear_trusted_proxies=True, config_path=config_path)
        self.assertEqual(cleared['console_trusted_proxies'], [])
        with self.assertRaises(ValueError):
            console_auth.configure(origins=['https://user:pass@desk.example.test'],
                                   config_path=config_path)
        with self.assertRaises(ValueError):
            console_auth.configure(origins=['https://desk.example.test/path'], config_path=config_path)
        with self.assertRaises(ValueError):
            console_auth.configure(bind='198.51.100.9', config_path=config_path)
        with self.assertRaises(ValueError):
            console_auth.configure(trusted_proxies=['0.0.0.0/0'], config_path=config_path)

    def test_origin_and_bind_validation_rules(self):
        self.assertEqual(console_auth.validate_origin('https://desk.example.test'),
                         'https://desk.example.test')
        self.assertEqual(console_auth.validate_origin('http://127.0.0.1:8080/'),
                         'http://127.0.0.1:8080')
        self.assertEqual(console_auth.validate_origin('https://desk.example.test:443'),
                         'https://desk.example.test')
        for bad in ('ftp://desk.example.test', 'https://*.example.test', 'https://desk.example.test?x=1',
                    'https://desk.example.test#frag', 'https://user@desk.example.test',
                    'https://desk.example.test/console', '', 'not a url'):
            with self.assertRaises(ValueError):
                console_auth.validate_origin(bad)
        self.assertEqual(console_auth.validate_bind('127.0.0.1'), '127.0.0.1')
        self.assertEqual(console_auth.validate_bind('0.0.0.0'), '0.0.0.0')
        with self.assertRaises(ValueError):
            console_auth.validate_bind('203.0.113.9')
        with self.assertRaises(ValueError):
            console_auth.validate_bind('localhost')

    def test_trusted_proxy_and_client_identity_rules(self):
        self.assertEqual(console_auth.validate_trusted_proxy('192.168.1.5'), '192.168.1.5/32')
        self.assertEqual(console_auth.validate_trusted_proxy('10.0.0.0/8'), '10.0.0.0/8')
        for bad in ('example.test', '0.0.0.0/0', '::1', '10.0.0.0/8/9', ''):
            with self.assertRaises(ValueError):
                console_auth.validate_trusted_proxy(bad)
        trusted = console_auth.trusted_proxy_networks({'console_trusted_proxies': ['127.0.0.1', '10.0.0.0/8']})
        # A trusted peer may supply exactly one IP; the socket peer is the fallback.
        self.assertEqual(console_auth.client_identity('127.0.0.1', '203.0.113.9', trusted), '203.0.113.9')
        self.assertEqual(console_auth.client_identity('10.1.2.3', '198.51.100.7', trusted), '198.51.100.7')
        self.assertEqual(console_auth.client_identity('127.0.0.1', '203.0.113.9, 10.0.0.1', trusted), '127.0.0.1')
        self.assertEqual(console_auth.client_identity('127.0.0.1', 'not-an-ip', trusted), '127.0.0.1')
        self.assertEqual(console_auth.client_identity('127.0.0.1', None, trusted), '127.0.0.1')
        # An untrusted socket peer never influences the identity.
        self.assertEqual(console_auth.client_identity('203.0.113.5', '198.51.100.1', trusted), '203.0.113.5')
        self.assertEqual(console_auth.client_identity('203.0.113.5', '198.51.100.1', []), '203.0.113.5')

    def test_origin_authority_must_match_request_host(self):
        self.assertTrue(console_auth.origin_matches_host('https://desk.example.test', 'desk.example.test'))
        self.assertTrue(console_auth.origin_matches_host('https://desk.example.test', 'desk.example.test:443'))
        self.assertTrue(console_auth.origin_matches_host('http://127.0.0.1:8080', '127.0.0.1:8080'))
        self.assertFalse(console_auth.origin_matches_host('http://127.0.0.1:8080', '127.0.0.1:9090'))
        self.assertFalse(console_auth.origin_matches_host('http://desk.example.test', 'other.example.test'))
        self.assertFalse(console_auth.origin_matches_host('bad', 'desk.example.test'))

    def test_host_and_secure_matching_ignores_default_port(self):
        origins = console_auth.origin_tuples({'console_url': 'http://127.0.0.1:1234',
                                              'console_allowed_origins': ['https://desk.example.test']})
        self.assertTrue(console_auth.host_allowed('127.0.0.1:1234', origins))
        self.assertTrue(console_auth.host_allowed('desk.example.test', origins))
        self.assertTrue(console_auth.host_allowed('desk.example.test:443', origins))
        self.assertFalse(console_auth.host_allowed('attacker.example', origins))
        self.assertFalse(console_auth.host_allowed('127.0.0.1:9999', origins))
        self.assertFalse(console_auth.host_allowed('', origins))
        self.assertTrue(console_auth.secure_for_host('desk.example.test', origins))
        self.assertFalse(console_auth.secure_for_host('127.0.0.1:1234', origins))
        self.assertTrue(console_auth.origin_allowed('https://desk.example.test', origins))
        self.assertFalse(console_auth.origin_allowed('https://attacker.example', origins))


class ConsoleHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config_path = self.root / 'config.json'
        self.web = self.root / 'web'
        self.web.mkdir()
        (self.web / 'login.html').write_bytes(b'<html><body>Worker Desk login</body></html>')
        (self.web / 'login.js').write_bytes(b'/* login script */')
        (self.web / 'style.css').write_bytes(b'body{}')
        (self.web / 'i18n.js').write_bytes(b'global.I18n = {};')
        (self.web / 'index.html').write_bytes(b'<html>Worker Desk console</html>')
        (self.web / 'app.js').write_bytes(b'/* app */')
        (self.web / 'manage.js').write_bytes(b'/* manage */')
        (self.web / 'setup.js').write_bytes(b'/* WorkerDeskSetup */')
        (self.web / 'task-view.js').write_bytes(b'/* TaskView */')
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(console, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(console, 'CONFIG', self.config_path),
            patch.object(console, 'WEB', self.web),
        ]
        for item in self.patchers:
            item.start()
        self.addCleanup(patch.stopall)
        self.config = {'server_url': 'http://127.0.0.1:9',
                       'console_url': 'http://127.0.0.1:9',
                       'console_bind': '127.0.0.1',
                       'console_allowed_origins': ['http://127.0.0.1:9'],
                       'profiles': {'fallback': {'model': 'acme/worker'}}}
        self.write_config()
        (self.state / 'server-password').write_text('synthetic-server-password')
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.tmp.cleanup()

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def start_server(self, origins=None):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        self.server.daemon_threads = True
        port = self.server.server_port
        url = 'http://127.0.0.1:' + str(port)
        self.config['console_url'] = url
        self.config['console_allowed_origins'] = origins or [url]
        self.write_config()
        self.server.cookie_name = 'delegate_console_session_' + str(port)
        self.server.allowed_origins = console_auth.origin_tuples(self.config)
        self.server.trusted_proxy_networks = console_auth.trusted_proxy_networks(self.config)
        self.server.auth_root = self.state
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return url

    def call(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = dict(headers or {})
        payload = raw
        if payload is None and body is not None:
            payload = json.dumps(body).encode()
            request_headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = {'status': response.status,
                  'headers': {k.lower(): v for k, v in response.getheaders()},
                  'body': data}
        connection.close()
        return result

    def login(self, username='admin', password='synthetic-pass', headers=None):
        return self.call('POST', '/console-api/auth/login',
                         body={'username': username, 'password': password}, headers=headers)

    def cookie(self, response):
        return response['headers'].get('set-cookie', '').split(';')[0]

    def test_login_flow_generic_errors_and_status_shape(self):
        self.start_server()
        anonymous = self.call('GET', '/console-api/auth/status')
        self.assertEqual(anonymous['status'], 200)
        self.assertEqual(json.loads(anonymous['body']), {'authenticated': False})
        good = self.login()
        self.assertEqual(good['status'], 200)
        self.assertEqual(json.loads(good['body']), {'authenticated': True, 'username': 'admin'})
        set_cookie = good['headers']['set-cookie']
        self.assertIn('HttpOnly', set_cookie)
        self.assertIn('SameSite=Strict', set_cookie)
        self.assertNotIn('Secure', set_cookie)
        cookie = self.cookie(good)
        self.assertTrue(cookie.startswith('delegate_console_session_'))
        for bad in (self.login(password='wrong-pass'), self.login(username='ghost', password='synthetic-pass')):
            self.assertEqual(bad['status'], 401)
            self.assertEqual(json.loads(bad['body'])['error'], 'Invalid username or password')
            self.assertNotIn('admin', bad['body'].decode())
        status = self.call('GET', '/console-api/auth/status', headers={'Cookie': cookie})
        self.assertEqual(json.loads(status['body']), {'authenticated': True, 'username': 'admin'})
        lowered = status['body'].decode().lower()
        for secret in ('hash', 'salt', 'token', 'password', 'settings', 'allowed_origins', 'console_bind'):
            self.assertNotIn(secret, lowered)

    def test_login_throttling_returns_429(self):
        self.start_server()
        for _ in range(console_auth.MAX_LOGIN_FAILURES):
            self.assertEqual(self.login(password='wrong-pass')['status'], 401)
        limited = self.login(password='wrong-pass')
        self.assertEqual(limited['status'], 429)
        self.assertIn('retry-after', limited['headers'])
        # Correct credentials are still throttled while the peer is locked.
        self.assertEqual(self.login()['status'], 429)

    def test_spoofed_forwarded_headers_from_untrusted_peer_do_not_authorize_or_rebucket(self):
        # An explicitly configured allowlist that excludes the direct socket peer.
        self.config['console_trusted_proxies'] = ['10.0.0.1']
        self.start_server()
        spoof = {'X-Forwarded-For': '203.0.113.5', 'X-Forwarded-Host': 'attacker.example',
                 'X-Forwarded-Proto': 'https', 'X-Real-IP': '203.0.113.5'}
        self.assertEqual(self.call('GET', '/console-api/state', headers=spoof)['status'], 401)
        for index in range(console_auth.MAX_LOGIN_FAILURES):
            response = self.login(password='wrong-pass',
                                  headers={'X-Forwarded-For': '203.0.113.%d' % index})
            self.assertEqual(response['status'], 401)
        # Unique spoofed addresses must not create fresh throttle buckets.
        self.assertEqual(self.login(password='wrong-pass',
                                    headers={'X-Forwarded-For': '203.0.113.250'})['status'], 429)

    def test_trusted_proxy_separates_two_logical_clients(self):
        self.config['console_trusted_proxies'] = ['127.0.0.1']
        self.start_server()
        for _ in range(console_auth.MAX_LOGIN_FAILURES):
            response = self.login(password='wrong-pass', headers={'X-Forwarded-For': '203.0.113.1'})
            self.assertEqual(response['status'], 401)
        self.assertEqual(self.login(password='wrong-pass',
                                    headers={'X-Forwarded-For': '203.0.113.1'})['status'], 429)
        # A second logical client from the same trusted socket peer is independent.
        self.assertEqual(self.login(password='wrong-pass',
                                    headers={'X-Forwarded-For': '203.0.113.2'})['status'], 401)
        # A multivalue chain is not parsed: it falls back to the (unlocked) peer bucket.
        self.assertEqual(self.login(password='wrong-pass',
                                    headers={'X-Forwarded-For': '203.0.113.9, 10.0.0.1'})['status'], 401)

    def test_origin_authority_must_match_request_host(self):
        url = self.start_server(origins=None)
        self.config['console_allowed_origins'] = [url, 'http://desk.example.test']
        self.write_config()
        self.server.allowed_origins = console_auth.origin_tuples(self.config)
        mismatch = self.login(headers={'Host': 'desk.example.test', 'Origin': url})
        self.assertEqual(mismatch['status'], 403)
        matched = self.login(headers={'Host': 'desk.example.test', 'Origin': 'http://desk.example.test'})
        self.assertEqual(matched['status'], 200)

    def test_old_deterministic_cookie_and_unknown_token_are_denied(self):
        self.start_server()
        old_value = hmac.new((self.state / 'server-password').read_bytes(),
                             self.config['console_url'].encode(), hashlib.sha256).hexdigest()
        old_name = 'delegate_console_' + str(self.server.server_port)
        for cookie in (old_name + '=' + old_value,
                       self.server.cookie_name + '=' + old_value,
                       self.server.cookie_name + '=totally-unknown-token'):
            response = self.call('GET', '/console-api/state', headers={'Cookie': cookie})
            self.assertEqual(response['status'], 401)

    def test_protected_console_and_native_gateway_require_session(self):
        self.start_server()
        for method, path in [('GET', '/console-api/state'), ('GET', '/console-api/models'),
                             ('POST', '/console-api/quota'), ('PATCH', '/native/x'),
                             ('DELETE', '/native/x')]:
            response = self.call(method, path, body={} if method in ('POST', 'PATCH', 'DELETE') else None)
            self.assertEqual(response['status'], 401, method + ' ' + path)
        ws = self.call('GET', '/native/session/x', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade'})
        self.assertEqual(ws['status'], 401)
        cookie = self.cookie(self.login())
        with patch.object(console.management, 'catalog', return_value={'providers': []}):
            authed = self.call('GET', '/console-api/models', headers={'Cookie': cookie})
            self.assertEqual(authed['status'], 200)

    def test_console_redirects_and_public_login_assets(self):
        self.start_server()
        console_page = self.call('GET', '/console', headers={'Accept': 'text/html'})
        self.assertEqual(console_page['status'], 302)
        self.assertEqual(console_page['headers']['location'], '/console-login')
        native_html = self.call('GET', '/some/native/route', headers={'Accept': 'text/html'})
        self.assertEqual(native_html['status'], 302)
        native_json = self.call('GET', '/some/native/route', headers={'Accept': 'application/json'})
        self.assertEqual(native_json['status'], 401)
        console_json = self.call('GET', '/console', headers={'Accept': 'application/json'})
        self.assertEqual(console_json['status'], 401)
        self.assertEqual(self.call('GET', '/console-api/state', headers={'Accept': 'text/html'})['status'], 401)
        login_page = self.call('GET', '/console-login')
        self.assertEqual(login_page['status'], 200)
        self.assertIn(b'Worker Desk login', login_page['body'])
        self.assertEqual(login_page['headers'].get('cache-control'), 'no-store')
        for asset in ('/console-assets/login.js', '/console-assets/style.css', '/console-assets/i18n.js'):
            response = self.call('GET', asset)
            self.assertEqual(response['status'], 200, asset)
        for private in ('/console-assets/app.js', '/console-assets/manage.js', '/console-assets/setup.js', '/console-assets/task-view.js'):
            self.assertEqual(self.call('GET', private)['status'], 401, private)
        cookie = self.cookie(self.login())
        self.assertEqual(self.call('GET', '/console-assets/app.js', headers={'Cookie': cookie})['status'], 200)
        setup_asset = self.call('GET', '/console-assets/setup.js', headers={'Cookie': cookie})
        self.assertEqual(setup_asset['status'], 200)
        self.assertIn(b'WorkerDeskSetup', setup_asset['body'])
        self.assertEqual(self.call('GET', '/console-assets/task-view.js', headers={'Cookie': cookie})['status'], 200)
        for unknown in ('/console-assets/unknown.js', '/console-assets/../i18n.js', '/console-assets/'):
            self.assertEqual(self.call('GET', unknown)['status'], 404, unknown)

    def test_host_and_origin_csrf_guards_cover_auth_endpoints(self):
        self.start_server()
        self.assertEqual(self.call('GET', '/console-login', headers={'Host': 'attacker.example'})['status'], 403)
        self.assertEqual(self.login(headers={'Origin': 'https://attacker.example'})['status'], 403)
        self.assertEqual(self.login(headers={'Sec-Fetch-Site': 'cross-site'})['status'], 403)
        self.assertEqual(self.call('POST', '/console-api/auth/logout',
                                   headers={'Origin': 'https://attacker.example'})['status'], 403)
        same_origin = self.login(headers={'Origin': self.config['console_url'],
                                          'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(same_origin['status'], 200)

    def test_logout_revokes_session_and_clears_cookie(self):
        self.start_server()
        cookie = self.cookie(self.login())
        self.assertTrue(json.loads(self.call('GET', '/console-api/auth/status',
                                             headers={'Cookie': cookie})['body'])['authenticated'])
        logout = self.call('POST', '/console-api/auth/logout', body={}, headers={'Cookie': cookie})
        self.assertEqual(logout['status'], 200)
        self.assertEqual(json.loads(logout['body']), {'authenticated': False})
        self.assertIn('Max-Age=0', logout['headers']['set-cookie'])
        after = self.call('GET', '/console-api/auth/status', headers={'Cookie': cookie})
        self.assertEqual(json.loads(after['body']), {'authenticated': False})

    def test_password_rotation_invalidates_http_session(self):
        self.start_server()
        cookie = self.cookie(self.login())
        console_auth.set_user('admin', 'rotated-pass', self.state)
        status = self.call('GET', '/console-api/auth/status', headers={'Cookie': cookie})
        self.assertEqual(json.loads(status['body']), {'authenticated': False})
        self.assertEqual(self.login(password='synthetic-pass')['status'], 401)
        self.assertEqual(self.login(password='rotated-pass')['status'], 200)

    def test_secure_cookie_only_for_configured_https_origin(self):
        url = self.start_server(origins=None)
        plain = self.login()
        self.assertNotIn('Secure', plain['headers']['set-cookie'])
        https_origins = [url, 'https://desk.example.test']
        self.config['console_allowed_origins'] = https_origins
        self.write_config()
        self.server.allowed_origins = console_auth.origin_tuples(self.config)
        secure = self.login(headers={'Host': 'desk.example.test'})
        self.assertEqual(secure['status'], 200)
        self.assertIn('Secure', secure['headers']['set-cookie'])
        mismatched = self.login(headers={'Host': 'desk.example.test:8443'})
        self.assertEqual(mismatched['status'], 403)

    def test_malformed_account_state_fails_closed_over_http(self):
        self.start_server()
        (self.state / 'console-auth' / 'account.json').write_text('{broken')
        response = self.login()
        self.assertEqual(response['status'], 503)
        self.assertNotIn(b'admin', response['body'])

    def test_login_body_size_and_type_are_bounded(self):
        self.start_server()
        oversized = self.call('POST', '/console-api/auth/login', raw=b'x' * 5000,
                              headers={'Content-Type': 'application/json'})
        self.assertEqual(oversized['status'], 400)
        array_body = self.call('POST', '/console-api/auth/login', raw=b'[1,2,3]',
                               headers={'Content-Type': 'application/json'})
        self.assertEqual(array_body['status'], 400)
        wrong_type = self.call('POST', '/console-api/auth/login',
                               body={'username': 1, 'password': ['x']})
        self.assertEqual(wrong_type['status'], 400)

    def test_login_admission_failure_fails_closed(self):
        self.start_server()
        with patch.object(console_auth, 'admit_login_attempt',
                          side_effect=OSError('throttle store unavailable')):
            response = self.login()
        self.assertEqual(response['status'], 503)
        self.assertEqual(json.loads(response['body'])['error'],
                         'Console authentication is unavailable')
        self.assertNotIn(b'admin', response['body'])
        self.assertNotIn('set-cookie', response['headers'])

    def test_logout_revocation_failure_does_not_report_success(self):
        self.start_server()
        cookie = self.cookie(self.login())
        with patch.object(console_auth, 'revoke_session',
                          side_effect=OSError('session store unavailable')):
            response = self.call('POST', '/console-api/auth/logout', body={},
                                 headers={'Cookie': cookie})
        self.assertEqual(response['status'], 503)
        self.assertEqual(json.loads(response['body'])['error'],
                         'Console authentication is unavailable')
        # The browser cookie is still cleared even though logout reported failure.
        self.assertIn('Max-Age=0', response['headers']['set-cookie'])
        # No false success: the server-side session was not revoked.
        status = self.call('GET', '/console-api/auth/status', headers={'Cookie': cookie})
        self.assertEqual(json.loads(status['body']),
                         {'authenticated': True, 'username': 'admin'})

    def test_malformed_unicode_login_is_generic(self):
        self.start_server()
        # A JSON lone-surrogate escape yields a Python str that is not valid UTF-8.
        lone = b'{"username": "admin", "password": "\\udcff"}'
        response = self.call('POST', '/console-api/auth/login', raw=lone,
                             headers={'Content-Type': 'application/json'})
        self.assertIn(response['status'], (400, 401))
        self.assertNotIn(b'Traceback', response['body'])
        self.assertFalse(b'admin' in response['body'])
        # Invalid raw UTF-8 bytes are a generic 400 as well.
        bad = self.call('POST', '/console-api/auth/login',
                        raw=b'{"username": "admin", "password": "\xff\xfe"}',
                        headers={'Content-Type': 'application/json'})
        self.assertEqual(bad['status'], 400)
        self.assertNotIn(b'Traceback', bad['body'])
        # The server keeps serving normal logins afterwards.
        self.assertEqual(self.login()['status'], 200)

    def test_idle_stream_still_observes_session_revocation(self):
        port, listener = self.fake_upstream(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n', stream=b'')
        self.start_server()
        self.config['server_url'] = 'http://127.0.0.1:%d' % port
        self.write_config()
        cookie = self.cookie(self.login())
        original_interval = console.STREAM_AUTH_INTERVAL
        original_io = console.STREAM_IO_TIMEOUT
        console.STREAM_AUTH_INTERVAL = 1
        console.STREAM_IO_TIMEOUT = 1
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            connection.request('GET', '/event', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            # Let the streaming proxy park on the idle upstream read.
            time.sleep(1.5)
            console_auth.set_user('admin', 'rotated-pass', self.state)
            began = time.time()
            response.read()
            self.assertLess(time.time() - began, 8, 'idle stream survived session revocation')
        finally:
            console.STREAM_AUTH_INTERVAL = original_interval
            console.STREAM_IO_TIMEOUT = original_io
            if connection is not None:
                connection.close()
            listener.close()

    def test_prefetched_body_is_forwarded_without_waiting_for_an_event(self):
        # Headers and body arrive together and the upstream then keeps the TCP
        # connection open. http.client prefetches the body into its reader while
        # parsing the headers, so socket select() sees nothing to read. The
        # proxy must still forward the complete payload promptly.
        payload = b'{"ok": true}'
        head = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                b'Content-Length: %d\r\n\r\n' % len(payload)) + payload
        port, listener = self.fake_upstream(head, stream=b'')
        self.start_server()
        self.config['server_url'] = 'http://127.0.0.1:%d' % port
        self.write_config()
        cookie = self.cookie(self.login())
        began = time.time()
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            connection.request('GET', '/event', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            body = response.read()
            elapsed = time.time() - began
        finally:
            if connection is not None:
                connection.close()
            listener.close()
        self.assertEqual(body, payload)
        self.assertLess(elapsed, 5, 'prefetched body waited for a new network event')

    def test_healthy_idle_stream_survives_io_timeout_and_delivers_late_data(self):
        # The upstream sends only headers, stays idle for longer than the
        # proxy's I/O timeout, and only then delivers a chunk. A blocking buffer
        # probe would latch a read timeout and close this healthy stream.
        headers = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n'
        chunk = b'data: late\n\n'
        port, listener = self.delayed_upstream(headers, delay=2.0, chunk=chunk)
        self.start_server()
        self.config['server_url'] = 'http://127.0.0.1:%d' % port
        self.write_config()
        cookie = self.cookie(self.login())
        original_io = console.STREAM_IO_TIMEOUT
        console.STREAM_IO_TIMEOUT = 1
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            connection.request('GET', '/event', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            began = time.time()
            body = response.read()
            elapsed = time.time() - began
        finally:
            console.STREAM_IO_TIMEOUT = original_io
            if connection is not None:
                connection.close()
            listener.close()
        self.assertEqual(body, chunk)
        self.assertGreaterEqual(elapsed, 1.5, 'stream did not stay idle past the I/O timeout')

    def test_empty_keepalive_responses_complete_without_waiting_for_data(self):
        self.start_server()
        cookie = self.cookie(self.login())
        for status in ('200 OK', '204 No Content', '304 Not Modified'):
            with self.subTest(status=status):
                header = ('HTTP/1.1 %s\r\nContent-Length: 0\r\n\r\n' % status).encode()
                port, listener = self.fake_upstream(header)
                self.config['server_url'] = 'http://127.0.0.1:%d' % port
                self.write_config()
                try:
                    with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as client:
                        request = ('GET /empty HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s\r\n\r\n'
                                   % (self.server.server_port, cookie))
                        client.sendall(request.encode())
                        received = b''
                        while True:
                            data = client.recv(4096)
                            if not data:
                                break
                            received += data
                        self.assertIn(('HTTP/1.1 ' + status).encode(), received)
                        self.assertTrue(received.endswith(b'\r\n\r\n'))
                finally:
                    listener.close()

    def test_logout_body_does_not_poison_next_browser_request(self):
        self.start_server()
        cookie = self.cookie(self.login())
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('POST', '/console-api/auth/logout', body='{}',
                               headers={'Cookie': cookie, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Connection'), 'close')
            response.read()
            connection.request('GET', '/console-api/auth/status', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertFalse(json.loads(response.read())['authenticated'])
        finally:
            connection.close()

    def fake_upstream(self, response, stream=b''):
        """A loopback TCP server that answers one request and streams bytes."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        port = listener.getsockname()[1]

        def serve():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            try:
                connection.settimeout(5)
                head = b''
                while b'\r\n\r\n' not in head:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                connection.sendall(response)
                while True:
                    if stream:
                        connection.sendall(stream)
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

        threading.Thread(target=serve, daemon=True).start()
        return port, listener

    def delayed_upstream(self, headers, delay, chunk, hold=1.0):
        """A loopback TCP server that sends headers, waits, then sends one chunk."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        port = listener.getsockname()[1]

        def serve():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            try:
                connection.settimeout(5)
                head = b''
                while b'\r\n\r\n' not in head:
                    received = connection.recv(4096)
                    if not received:
                        return
                    head += received
                connection.sendall(headers)
                time.sleep(delay)
                connection.sendall(chunk)
                time.sleep(hold)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

        threading.Thread(target=serve, daemon=True).start()
        return port, listener

    def test_websocket_closes_after_session_revocation(self):
        port, listener = self.fake_upstream(
            b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
        self.start_server()
        self.config['server_url'] = 'http://127.0.0.1:%d' % port
        self.write_config()
        cookie = self.cookie(self.login())
        original = console.STREAM_AUTH_INTERVAL
        console.STREAM_AUTH_INTERVAL = 1
        client = None
        try:
            client = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5)
            request = ('GET /session/x HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                       'Upgrade: websocket\r\nConnection: Upgrade\r\nCookie: %s\r\n\r\n'
                       % (self.server.server_port, cookie))
            client.sendall(request.encode())
            client.settimeout(5)
            self.assertIn(b'101', client.recv(4096))
            console_auth.set_user('admin', 'rotated-pass', self.state)
            closed = False
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    closed = True
                    break
            self.assertTrue(closed, 'WebSocket survived session revocation')
        finally:
            console.STREAM_AUTH_INTERVAL = original
            if client is not None:
                client.close()
            listener.close()

    def test_streaming_response_closes_after_session_revocation(self):
        port, listener = self.fake_upstream(
            b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n', stream=b'data: tick\n\n')
        self.start_server()
        self.config['server_url'] = 'http://127.0.0.1:%d' % port
        self.write_config()
        cookie = self.cookie(self.login())
        original = console.STREAM_AUTH_INTERVAL
        console.STREAM_AUTH_INTERVAL = 1
        connection = None
        try:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            connection.request('GET', '/event', headers={'Cookie': cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(1), b'd')
            console_auth.set_user('admin', 'rotated-pass', self.state)
            began = time.time()
            response.read()
            self.assertLess(time.time() - began, 8, 'stream survived session revocation')
        finally:
            console.STREAM_AUTH_INTERVAL = original
            if connection is not None:
                connection.close()
            listener.close()

    def test_proxy_target_is_loopback_only(self):
        self.start_server()
        cookie = self.cookie(self.login())
        self.config['server_url'] = 'http://example.com:4321'
        self.write_config()
        response = self.call('GET', '/native/session/x', headers={'Cookie': cookie})
        self.assertEqual(response['status'], 502)
        self.assertIn(b'loopback', response['body'])


if __name__ == '__main__':
    unittest.main()
