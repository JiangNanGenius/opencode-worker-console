"""Real loopback recovery plus deterministic generation/lease/routing guards."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch
from tests import test_bailian_pool as fixtures

bailian_pool = fixtures.bailian_pool
common = fixtures.common
MODEL = {'id': 'msg_fake', 'type': 'message', 'role': 'assistant',
         'content': [{'type': 'text', 'text': 'real model result'}], 'stop_reason': 'end_turn'}


class RecoveryEdges(unittest.TestCase):
    setUp = fixtures.PoolProxyTests.setUp
    tearDown = fixtures.PoolProxyTests.tearDown
    def request(self, *args, **kwargs):
        result = fixtures.PoolProxyTests.request(self, *args, **kwargs)
        deadline = time.monotonic() + 2
        while any(self.slot(slot['id']).get('probe_lease') for slot in self.slots()) and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        return result
    stored = fixtures.PoolProxyTests.stored
    slot = fixtures.PoolProxyTests.slot

    def slots(self):
        return bailian_pool.pool_settings(self.config)['slots']

    def block(self, index=0, state='exhausted', age=301):
        slot = self.slots()[index]
        generation = bailian_pool._load_key(slot)[1]
        with patch.object(bailian_pool.time, 'time', return_value=time.time() - age):
            bailian_pool._mark(slot, generation, state, kind='quota' if state == 'exhausted' else 'auth')
        return slot, generation

    def all_blocked(self):
        common.write_json(bailian_pool.state_path(), {})
        self.block(1, state='auth')
        return self.block()

    def test_due_real_messages_recovers_and_returns_exact_bytes(self):
        self.all_blocked()
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(200, MODEL)
        before = self.stored()
        proof = bailian_pool.recovery_available(self.config, after=time.time() - 1)
        self.assertTrue(proof['authorized_probe'])
        self.assertFalse(proof['healthy'])
        self.assertEqual(self.stored(), before)  # status reads never acquire a lease
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), MODEL)
        self.assertEqual(self.slot('seat-1')['state'], 'ok')
        proof = bailian_pool.recovery_available(self.config, after=time.time() - 1)
        self.assertTrue(proof['healthy'])
        self.assertFalse(proof['authorized_probe'])
        self.assertFalse(bailian_pool.recovery_available(self.config, after=time.time() + 1)['healthy'])

    def test_normal_secondary_precedes_due_primary(self):
        self.block()
        self.upstream.routes[self.keys['seat-2']] = fixtures._json_route(200, MODEL)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.upstream.requests[0]['headers']['x-api-key'], self.keys['seat-2'])
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')

    def test_rejection_exponential_backoff_and_cap(self):
        slot, gen = self.all_blocked()
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(429, fixtures.QUOTA)
        started = time.time()
        self.assertEqual(self.request()[0], 429)
        self.assertGreaterEqual(self.slot('seat-1')['retry_at'], started + 600)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(len(self.upstream.requests), 1)
        for _ in range(20):
            bailian_pool._mark(slot, gen, 'exhausted', kind='quota')
        self.assertLessEqual(self.slot('seat-1')['retry_at'] - time.time(), 3600)

    def test_auth_never_probes_but_actual_rotation_authorizes(self):
        self.block(0, state='auth', age=100000)
        self.block(1, state='auth', age=100000)
        self.assertFalse(bailian_pool.recovery_available(self.config)['available'])
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.upstream.requests, [])
        self.keys['seat-1'] += '-rotated'
        p = bailian_pool.recovery_available(self.config)
        self.assertTrue(p['authorized_probe'])
        self.assertFalse(p['healthy'])

    def test_single_flight_and_crash_expired_lease(self):
        slot, gen = self.all_blocked()
        barrier = threading.Barrier(3)
        results = []
        def claim():
            barrier.wait()
            results.append(bailian_pool._claim_attempt(slot, gen, timeout=30))
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for w in workers: w.start()
        barrier.wait()
        for w in workers: w.join()
        self.assertEqual(sum(x is not None for x in results), 1)
        original = next(x for x in results if x)
        self.assertFalse(bailian_pool.recovery_available(self.config)['authorized_probe'])
        with patch.object(bailian_pool.time, 'time', return_value=time.time() + 100):
            newer = bailian_pool._claim_attempt(slot, gen, timeout=30)
            self.assertIsNotNone(newer)
            self.assertNotEqual(original['id'], newer['id'])
            self.assertFalse(bailian_pool._mark(slot, gen, 'ok', attempt=original, verified_model=True))

    def test_stream_activity_renews_lease(self):
        slot, gen = self.all_blocked()
        attempt = bailian_pool._claim_attempt(slot, gen, timeout=30)
        old = self.slot('seat-1')['probe_lease']['until']
        with patch.object(bailian_pool.time, 'time', return_value=old - 20):
            bailian_pool._renew_probe(slot, gen, attempt, 30)
            self.assertGreater(self.slot('seat-1')['probe_lease']['until'], old)
            self.assertIsNone(bailian_pool._claim_attempt(slot, gen, timeout=30))

    def test_late_success_cannot_override_new_rejection_or_rotation(self):
        slot, gen = self.all_blocked()
        attempt = bailian_pool._claim_attempt(slot, gen)
        bailian_pool._mark(slot, gen, 'exhausted', kind='quota', attempt=attempt)
        self.assertFalse(bailian_pool._mark(slot, gen, 'ok', attempt=attempt, verified_model=True))
        self.keys['seat-1'] += '-rotated'
        self.assertFalse(bailian_pool._mark(slot, gen, 'ok', verified_model=True))
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')

    def test_count_tokens_cannot_probe_or_recover(self):
        self.all_blocked()
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(200, {'input_tokens': 7})
        self.assertEqual(self.request(path='/messages/count_tokens')[0], 429)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')

    def test_http_200_incomplete_or_non_model_does_not_recover(self):
        self.all_blocked()
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(200, {'ok': True})
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')
        self.assertNotIn('probe_lease', self.slot('seat-1'))
        self.assertNotIn('last_model_success_at', self.slot('seat-1'))

    def test_stream_success_requires_real_message_stop_and_preserves_bytes(self):
        self.all_blocked()
        events = [
            {'type': 'message_start', 'message': {'id': 'm', 'type': 'message', 'role': 'assistant'}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'ok'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
            {'type': 'message_stop'}]
        payload = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events)
        self.upstream.routes[self.keys['seat-1']] = fixtures._stream_route([payload[:23], payload[23:]])
        self.assertEqual(self.request()[2], payload)
        self.assertEqual(self.slot('seat-1')['state'], 'ok')

    def test_sse_error_or_truncation_never_recovers_or_replays(self):
        for tail in (b'data: {"type":"error","error":{"message":"quota"}}\n\n', b'data: {"type":"message_stop"'):
            with self.subTest(tail=tail):
                self.all_blocked()
                self.upstream.routes[self.keys['seat-1']] = fixtures._stream_route([tail])
                prior = len(self.upstream.requests)
                self.assertEqual(self.request()[2], tail)
                self.assertEqual(len(self.upstream.requests), prior + 1)
                self.assertEqual(self.slot('seat-1')['state'], 'exhausted')

    def test_halfopen_temporary_or_transport_never_cross_slot_replays(self):
        for route in (fixtures._json_route(429, fixtures.TEMPORARY),
                      fixtures._json_route(500, {'error': {'message': 'upstream'}}),
                      fixtures._close_without_response):
            self.all_blocked()
            self.upstream.routes[self.keys['seat-1']] = route
            prior = len(self.upstream.requests)
            self.request()
            self.assertEqual(len(self.upstream.requests), prior + 1)
            self.assertEqual(self.slot('seat-1')['state'], 'exhausted')

    def test_default_ok_and_aggregate_are_not_recovery_proof(self):
        common.write_json(self.config_path, dict(self.config, account_total={'remaining_percent': 25}))
        proof = bailian_pool.recovery_available(self.config)
        self.assertTrue(proof['admission_available'])
        self.assertFalse(proof['available'])
        self.assertFalse(proof['healthy'])
        self.block(0, age=0)
        self.block(1, age=0)
        self.assertFalse(bailian_pool.recovery_available(self.config)['available'])

    def test_quota_admission_due_probe_not_account_zero_or_auth_bypass(self):
        import quota
        now = time.time()
        provider = bailian_pool.providers.BAILIAN_PROVIDER
        metadata = {'configured': True, 'admission_available': True, 'available': True,
                    'healthy': False, 'authorized_probe': True, 'sampled_at': now}
        q = {provider: {'state': 'ok', 'available': True, 'windows': [], 'inference_pool': metadata}}
        with patch.object(quota, 'billing_block', return_value={'reason': quota.MONTHLY_REASON}):
            self.assertTrue(quota.allowed(provider, q, 'normal', prepaid=True)[0])
            q[provider]['windows'] = [{'valid': True, 'remaining': 0}]
            self.assertFalse(quota.allowed(provider, q, 'normal', prepaid=True)[0])
            q[provider]['windows'] = []
        with patch.object(quota, 'billing_block', return_value={'reason': quota.AUTH_REASON}):
            self.assertFalse(quota.allowed(provider, q, 'normal', prepaid=True)[0])
        metadata.update(available=False, authorized_probe=False, admission_available=False)
        with patch.object(quota, 'billing_block', return_value={}):
            self.assertFalse(quota.allowed(provider, q, 'normal', prepaid=True)[0])

    def test_malformed_or_empty_content_is_not_success_but_tool_use_is(self):
        class Response:
            def getheader(self, name):
                return 'application/json' if name == 'Content-Type' else None
        for content, expected in (([{}], False), ([{'type': 'text', 'text': ''}], False),
                                  ([{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}], True),
                                  ([{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': 'bad'}], False)):
            proof = bailian_pool._ModelResponseProof(Response())
            proof.feed(json.dumps(dict(MODEL, content=content)).encode())
            self.assertEqual(proof.complete(), expected)

    def test_empty_sse_start_not_success_and_complete_tool_input_is(self):
        class Response:
            def getheader(self, name):
                return 'text/event-stream' if name == 'Content-Type' else None
        def prove(block, deltas):
            proof = bailian_pool._ModelResponseProof(Response())
            events = [{'type': 'message_start', 'message': {'id': 'm', 'type': 'message', 'role': 'assistant'}},
                      {'type': 'content_block_start', 'index': 0, 'content_block': block}]
            events += [{'type': 'content_block_delta', 'index': 0, 'delta': d} for d in deltas]
            events += [{'type': 'content_block_stop', 'index': 0},
                       {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}, {'type': 'message_stop'}]
            for e in events:
                proof.feed(b'data: ' + json.dumps(e).encode() + b'\n\n')
            return proof.complete()
        self.assertFalse(prove({'type': 'text', 'text': ''}, []))
        self.assertTrue(prove({'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}, []))
        self.assertTrue(prove({'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}},
                              [{'type': 'input_json_delta', 'partial_json': '{"path":'},
                               {'type': 'input_json_delta', 'partial_json': '"file"}'}]))
        self.assertFalse(prove({'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}},
                               [{'type': 'input_json_delta', 'partial_json': '{'}]))

    def test_complete_stream_error_persists_before_downstream_disconnect(self):
        import http.client
        self.all_blocked()
        error = b'data: {"type":"error","error":{"type":"authentication_error","message":"invalid api key"}}\n\n'
        def route(handler):
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Content-Length', str(len(error) + 1000))
            handler.end_headers()
            handler.wfile.write(error)
            handler.wfile.flush()
            handler.close_connection = True  # terminal error followed by transport truncation
        self.upstream.routes[self.keys['seat-1']] = route
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('POST', '/messages', body=fixtures.PAYLOAD,
                               headers={'x-api-key': fixtures.PASSWORD, 'content-type': 'application/json'})
            with self.assertRaises(http.client.IncompleteRead):
                connection.getresponse().read()
        finally:
            connection.close()
        deadline = time.monotonic() + 2
        while self.slot('seat-1').get('state') != 'auth' and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertEqual(self.slot('seat-1')['state'], 'auth')
        self.assertEqual(self.slot('seat-1')['failure_count'], 2)  # exactly one SSE rejection
        self.assertEqual(len(self.upstream.requests), 1)

    def test_error_persisted_even_if_downstream_write_raises(self):
        import io
        class Response:
            status = 200
            reason = 'OK'
            def getheader(self, name):
                return 'text/event-stream' if name == 'Content-Type' else None
            def getheaders(self):
                return [('Content-Type', 'text/event-stream')]
            def close(self):
                pass
            def read1(self, size):
                if hasattr(self, 'sent'):
                    return b''
                self.sent = True
                return b'data: {"type":"error","error":{"message":"insufficient balance"}}\n\n'
        class BrokenWriter(io.BytesIO):
            def write(self, value):
                raise BrokenPipeError()
        self.all_blocked()
        # Exercise real observer/_proxy error path without a network write race.
        handler = object.__new__(bailian_pool.ProxyHandler)
        handler.settings = bailian_pool.pool_settings(self.config)
        handler.headers = {}
        handler.command = 'POST'
        handler.wfile = BrokenWriter()
        handler._response_started = False
        handler.send_response = lambda *a: None
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        with patch.object(bailian_pool, '_open_upstream', return_value=(None, Response())):
            handler._proxy('/messages', '', fixtures.PAYLOAD)
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')
        self.assertEqual(self.slot('seat-1')['kind'], 'balance')
        self.assertEqual(self.slot('seat-1')['failure_count'], 2)
        self.assertGreater(self.slot('seat-1')['retry_at'], time.time() + 590)

    def test_mixed_quota_and_auth_never_becomes_global_auth(self):
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(429, fixtures.QUOTA)
        self.upstream.routes[self.keys['seat-2']] = fixtures._json_route(401, fixtures.AUTH)
        status, _, body = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), fixtures.QUOTA)
        self.assertEqual(self.slot('seat-1')['state'], 'exhausted')
        self.assertEqual(self.slot('seat-2')['state'], 'auth')
        # Subsequent call during cooldown must also not surface the auth seat.
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(len(self.upstream.requests), 2)

    def test_same_label_physical_rotation_can_authorize_old_auth_circuit(self):
        import quota
        self.block(0, state='auth')
        self.block(1, state='auth')
        self.assertFalse(bailian_pool.recovery_available(self.config)['credential_rotated'])
        self.keys['seat-1'] += '-physical-rotation'
        evidence = bailian_pool.recovery_available(self.config)
        self.assertTrue(evidence['credential_rotated'])
        self.assertTrue(evidence['authorized_probe'])
        evidence['sampled_at'] = time.time()
        provider = bailian_pool.providers.BAILIAN_PROVIDER
        q = {provider: {'state': 'ok', 'available': True, 'windows': [], 'inference_pool': evidence}}
        with patch.object(quota, 'billing_block', return_value={'reason': quota.AUTH_REASON}):
            self.assertTrue(quota.allowed(provider, q, 'normal', prepaid=True)[0])
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(200, MODEL)
        self.assertEqual(self.request()[0], 200)
        deadline = time.monotonic() + 2
        while not bailian_pool.recovery_available(self.config)['healthy'] and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        self.assertTrue(bailian_pool.recovery_available(self.config)['healthy'])

    def test_configured_label_change_is_not_physical_auth_rotation(self):
        slot, gen = self.block(0, state='auth')
        self.block(1, state='auth')
        changed = copy.deepcopy(self.config)
        changed['bailian_pool']['slots'][0]['generation'] = 'new-label-same-key'
        evidence = bailian_pool.recovery_available(changed)
        self.assertTrue(evidence['authorized_probe'])
        self.assertFalse(evidence['credential_rotated'])
        import quota
        evidence['sampled_at'] = time.time()
        provider = bailian_pool.providers.BAILIAN_PROVIDER
        q = {provider: {'state': 'ok', 'available': True, 'windows': [], 'inference_pool': evidence}}
        with patch.object(quota, 'billing_block', return_value={'reason': quota.AUTH_REASON}):
            self.assertFalse(quota.allowed(provider, q, 'normal', prepaid=True)[0])

    def test_count_tokens_does_not_consume_physical_rotation_proof(self):
        self.block(0, state='auth')
        self.block(1, state='auth')
        original = self.slot('seat-1')
        self.keys['seat-1'] += '-new'
        self.upstream.routes[self.keys['seat-1']] = fixtures._json_route(200, {'input_tokens': 3})
        self.assertEqual(self.request(path='/messages/count_tokens')[0], 401)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.slot('seat-1'), original)
        self.assertTrue(bailian_pool.recovery_available(self.config)['credential_rotated'])

    def test_rotated_primary_probe_also_defers_to_healthy_secondary(self):
        self.block(0, state='auth')
        self.keys['seat-1'] += '-new-key'
        self.upstream.routes[self.keys['seat-2']] = fixtures._json_route(200, MODEL)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(self.upstream.requests[0]['headers']['x-api-key'], self.keys['seat-2'])
