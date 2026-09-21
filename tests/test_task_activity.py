"""Isolated tests for live task activity and accurate token usage.

Every test uses a temporary state directory and synthetic message payloads.
No paid model request, native session, real task file or live service is
touched; ``common.api`` is either absent (retained paths must not call it) or
replaced by a fake local function.
"""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import console
import console_auth
import quota
import task_activity as ta
import worker

DAY_MS = 1_700_000_000_000
FAKE_KEY = 'sk-' + 'A' * 32


def assistant(mid, tokens=None, cost=None, parts=(), created=DAY_MS, completed=None):
    info = {'id': mid, 'role': 'assistant', 'time': {'created': created}, 'providerID': 'acme', 'modelID': 'worker'}
    if completed is not None:
        info['time']['completed'] = completed
    if tokens is not None:
        info['tokens'] = tokens
    if cost is not None:
        info['cost'] = cost
    return {'info': info, 'parts': list(parts)}


def user(mid, parts=(), created=DAY_MS):
    return {'info': {'id': mid, 'role': 'user', 'time': {'created': created}}, 'parts': list(parts)}


def text_part(pid, text):
    return {'id': pid, 'type': 'text', 'text': text}


def tool_part(pid, tool='bash', status='completed', inputs=None, at=DAY_MS, error=None):
    state = {'status': status, 'input': inputs or {}, 'time': {'start': at, 'end': at + 100}}
    if error is not None:
        state['error'] = error
    state['output'] = 'RAW TOOL OUTPUT'
    return {'id': pid, 'type': 'tool', 'tool': tool, 'state': state}


def sample_tokens(total, input, output, reasoning, read, write=0):
    return {'total': total, 'input': input, 'output': output, 'reasoning': reasoning,
            'cache': {'read': read, 'write': write}}


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps({
            'server_url': 'http://127.0.0.1:9', 'console_url': 'http://127.0.0.1:9',
            'profiles': {'fallback': {'model': 'acme/worker', 'label': 'Acme'}},
        }))
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(common, 'CONFIG', self.config_path)]
        for item in self.patchers:
            item.start()
        self.addCleanup(patch.stopall)
        ta._cache.clear()
        ta._result_cache.clear()
        ta._messages_cache.clear()
        ta._full_text_cache.clear()
        ta._locks.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, task_id='job-0000000000000001', **overrides):
        value = {'id': task_id, 'title': 'Demo task', 'status': 'completed', 'session_id': 'ses_demo',
                 'session_deleted': True, 'directory': str(self.root), 'source_dir': str(self.root),
                 'group_id': 'group-1', 'group_title': 'Demo', 'mode': 'read', 'workspace': 'shared',
                 'objective': 'Do the thing', 'acceptance': [], 'created_at': 1000.0, 'updated_at': 1000.0,
                 'started_at': 1000.0, 'errors': []}
        value.update(overrides)
        return value

    def write_task(self, task):
        path = self.state / 'tasks' / (task['id'] + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task))
        return path

    def write_messages(self, task_id, messages):
        art = self.state / 'artifacts' / task_id
        art.mkdir(parents=True, exist_ok=True)
        (art / 'messages.json').write_text(json.dumps(messages))
        return art

    def write_result(self, task_id, result):
        art = self.state / 'artifacts' / task_id
        art.mkdir(parents=True, exist_ok=True)
        (art / 'result.json').write_text(json.dumps(result))
        return art


class UsageCountingTests(IsolatedCase):
    def test_nested_cache_cost_and_totals_across_unique_messages(self):
        messages = [
            assistant('msg_a', sample_tokens(10340, 648, 100, 248, 9344), cost=0.001,
                      parts=[text_part('prt_a', 'first')]),
            assistant('msg_b', sample_tokens(20000, 1000, 200, 300, 18500, 0), cost=0.002,
                      parts=[text_part('prt_b', 'second')]),
        ]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(usage['input'], 1648)
        self.assertEqual(usage['output'], 300)
        self.assertEqual(usage['reasoning'], 548)
        self.assertEqual(usage['cache_read'], 27844)
        self.assertEqual(usage['cache_write'], 0)
        self.assertEqual(usage['total'], 30340)
        self.assertAlmostEqual(usage['cost'], 0.003)
        self.assertEqual(usage['source'], 'saved')
        self.assertTrue(usage['complete'])

    def test_provider_total_is_never_added_to_its_components(self):
        messages = [assistant('msg_a', sample_tokens(100, 10, 5, 5, 80))]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(usage['total'], 100)

    def test_missing_values_are_null_and_real_zeros_are_numbers(self):
        messages = [assistant('msg_a', {'input': 5, 'output': 0})]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(usage['input'], 5)
        self.assertEqual(usage['output'], 0)
        self.assertIsNone(usage['reasoning'])
        self.assertIsNone(usage['cache_read'])
        self.assertIsNone(usage['cache_write'])
        self.assertEqual(usage['total'], 5)
        self.assertIsNone(usage['cost'])
        self.assertFalse(usage['complete'])

    def test_duplicate_message_ids_are_counted_once_with_latest_values(self):
        messages = [
            assistant('msg_a', {'input': 1}),
            assistant('msg_a', sample_tokens(15, 4, 2, 3, 6, 0), cost=0.01),
            assistant('msg_b', sample_tokens(5, 1, 1, 1, 2, 0), cost=0.02),
        ]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(usage['input'], 5)
        self.assertEqual(usage['output'], 3)
        self.assertEqual(usage['total'], 20)
        self.assertAlmostEqual(usage['cost'], 0.03)
        self.assertTrue(usage['complete'])

    def test_partial_message_marks_incomplete_but_keeps_known_totals(self):
        messages = [assistant('msg_a', sample_tokens(30, 10, 5, 5, 10, 0)), assistant('msg_b', {'input': 7})]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(usage['total'], 37)
        self.assertEqual(usage['reasoning'], 5)  # sum of reported values, not nulled
        self.assertEqual(usage['cache_read'], 10)
        self.assertFalse(usage['complete'])  # msg_b omitted the remaining counters

    def test_complete_empty_history_is_zero_not_null(self):
        usage = ta.usage_from_messages([], complete_history=True)
        for name in ta.COMPONENTS:
            self.assertEqual(usage[name], 0, name)
        self.assertEqual(usage['total'], 0)
        self.assertEqual(usage['cost'], 0.0)
        self.assertTrue(usage['complete'])
        partial = ta.usage_from_messages([], complete_history=False)
        self.assertIsNone(partial['total'])
        self.assertFalse(partial['complete'])

    def test_flat_cache_keys_and_invalid_numbers(self):
        flat = ta.usage_from_messages([assistant('msg_a', {'total': 8, 'input': 1, 'output': 1, 'reasoning': 1,
                                                           'cache_read': 4, 'cache_write': 1}, cost=0.0)])
        self.assertEqual(flat['cache_read'], 4)
        self.assertEqual(flat['cache_write'], 1)
        self.assertEqual(flat['cost'], 0.0)
        self.assertTrue(flat['complete'])
        no_cost = ta.usage_from_messages([assistant('msg_a', {'total': 8, 'input': 1, 'output': 1, 'reasoning': 1,
                                                             'cache_read': 4, 'cache_write': 1})])
        self.assertIsNone(no_cost['cost'])
        self.assertFalse(no_cost['complete'])
        invalid = ta.usage_from_messages([assistant('msg_a', {'input': -5, 'output': float('inf'),
                                                              'reasoning': True, 'cache': {'read': -1}})])
        self.assertIsNone(invalid['total'])
        self.assertIsNone(invalid['input'])
        self.assertIsNone(invalid['output'])
        self.assertIsNone(invalid['reasoning'])
        self.assertIsNone(invalid['cache_read'])
        self.assertFalse(invalid['complete'])

    def test_component_sum_reproduces_native_provider_total(self):
        # Native normalization: total = input + output + reasoning + cache.read + cache.write.
        messages = [assistant('msg_a', sample_tokens(10340, 648, 100, 248, 9344, 0), cost=0.0)]
        usage = ta.usage_from_messages(messages)
        self.assertEqual(sum(usage[name] for name in ta.COMPONENTS), usage['total'])
        aggregate = ta.usage_from_session({'tokens': {'input': 648, 'output': 100, 'reasoning': 248,
                                                      'cache': {'read': 9344, 'write': 0}}, 'cost': 0.0})
        self.assertEqual(aggregate['total'], usage['total'])

    def test_session_aggregate_maps_cache_and_cost(self):
        usage = ta.usage_from_session({'tokens': {'input': 10, 'output': 2, 'reasoning': 3,
                                                  'cache': {'read': 100, 'write': 5}}, 'cost': 0.25})
        self.assertEqual(usage['total'], 120)
        self.assertEqual(usage['cache_read'], 100)
        self.assertEqual(usage['cache_write'], 5)
        self.assertAlmostEqual(usage['cost'], 0.25)
        self.assertEqual(usage['source'], 'live')
        self.assertTrue(usage['complete'])
        sparse = ta.usage_from_session({'tokens': {'input': 10, 'output': 2, 'reasoning': 3}})
        self.assertIsNone(sparse['cache_read'])
        self.assertIsNone(sparse['cost'])
        self.assertEqual(sparse['total'], 15)
        self.assertFalse(sparse['complete'])


class LiveSnapshotTests(IsolatedCase):
    def session(self, input=10, output=2, reasoning=3, read=100, write=0, cost=0.5):
        return {'id': 'ses_demo', 'tokens': {'input': input, 'output': output, 'reasoning': reasoning,
                                             'cache': {'read': read, 'write': write}}, 'cost': cost}

    def live_api(self, calls, session_payload=None, messages=None):
        def fake(path, directory=None, method='GET', data=None, timeout=15, include_cursor=False):
            calls.append(path)
            if '/message?limit=' in path:
                return (messages if messages is not None else [], None)
            return session_payload if session_payload is not None else self.session()
        return fake

    def test_live_snapshot_shape_ttl_cache_and_list_usage(self):
        task = self.task(status='running', session_deleted=False)
        calls = []
        clock = {'t': 1000.0}
        with patch.object(common, 'api', side_effect=self.live_api(calls)), \
             patch.object(ta, '_now', side_effect=lambda: clock['t']):
            first = ta.snapshot(task)
            self.assertEqual(sorted(first.keys()), ['activity', 'usage'])
            self.assertEqual(first['activity']['source'], 'live')
            self.assertEqual(first['activity']['stale'], False)
            self.assertIsNone(first['activity']['error'])
            self.assertEqual([sorted(event.keys()) for event in first['activity']['events']],
                             [['id', 'label', 'status', 'text', 'time', 'type']] * len(first['activity']['events']))
            self.assertEqual(first['usage']['source'], 'live')
            self.assertEqual(first['usage']['total'], 115)
            self.assertTrue(first['usage']['complete'])
            self.assertEqual(len(calls), 2)
            second = ta.snapshot(task)
            self.assertEqual(len(calls), 2)
            self.assertEqual(second['activity']['sampled_at'], first['activity']['sampled_at'])
            self.assertEqual(ta.usage_for_task(task), first['usage'])
            self.assertEqual(len(calls), 2)
            clock['t'] += ta.LIVE_TTL + 0.5
            ta.snapshot(task)
            self.assertEqual(len(calls), 4)

    def test_live_requests_follow_session_directory_and_rebind_invalidates_cache(self):
        task = self.task('job-0000000000000008', status='running', session_deleted=False,
                         directory='/tmp/original', session_directory='/tmp/rebound')
        calls = []
        clock = {'t': 1000.0}

        def fake(path, directory=None, method='GET', data=None, timeout=15, include_cursor=False):
            calls.append((path, directory))
            return ([], None) if '/message?limit=' in path else self.session()

        with patch.object(common, 'api', side_effect=fake), \
             patch.object(ta, '_now', side_effect=lambda: clock['t']):
            ta.snapshot(task)
            self.assertEqual(len(calls), 2)
            for _, directory in calls:
                self.assertEqual(directory, '/tmp/rebound')
            ta.snapshot(task)
            self.assertEqual(len(calls), 2)  # cached for the same session + directory
            rebound = dict(task, session_directory='/tmp/moved-again')
            clock['t'] += 0.5
            ta.snapshot(rebound)
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[-2][1], '/tmp/moved-again')
            self.assertEqual(calls[-1][1], '/tmp/moved-again')
            self.assertEqual(ta.usage_for_task(rebound)['source'], 'live')
            # A rebind back to the first directory never reuses the second's cache.
            ta.snapshot(task)
            self.assertEqual(len(calls), 6)
            self.assertEqual(calls[-1][1], '/tmp/rebound')
            plain = self.task('job-0000000000000009', status='running', session_deleted=False,
                              directory='/tmp/plain')
            ta._cache.clear()
            calls.clear()
            ta.snapshot(plain)
            self.assertEqual(calls[0][1], '/tmp/plain')

    def test_live_failure_degrades_to_cached_then_retained_with_stale_and_error(self):
        task_id = 'job-0000000000000002'
        retained = [assistant('msg_retained', sample_tokens(9, 1, 2, 3, 3, 0), cost=0.01)]
        self.write_messages(task_id, retained)
        task = self.task(task_id, status='running', session_deleted=False)
        calls = []
        clock = {'t': 1000.0}
        with patch.object(common, 'api', side_effect=self.live_api(calls)), \
             patch.object(ta, '_now', side_effect=lambda: clock['t']):
            live = ta.snapshot(task)
            self.assertEqual(live['usage']['source'], 'live')
            clock['t'] += ta.LIVE_TTL + 0.5
            with patch.object(common, 'api', side_effect=common.HttpFailure(None, 'local server down')):
                transient = ta.snapshot(task)
            self.assertFalse(transient['activity']['stale'])
            self.assertIsNone(transient['activity']['error'])
            clock['t'] += ta.LIVE_WARNING_AFTER
            with patch.object(common, 'api', side_effect=common.HttpFailure(None, 'local server down')):
                degraded = ta.snapshot(task)
            self.assertTrue(degraded['activity']['stale'])
            self.assertIn('down', degraded['activity']['error'])
            self.assertEqual(degraded['usage']['source'], 'live')  # last complete cached sample
            self.assertEqual(degraded['usage']['total'], live['usage']['total'])
            ta._cache.clear()
            with patch.object(common, 'api', side_effect=common.HttpFailure(None, 'local server down')):
                saved = ta.snapshot(task)
        self.assertEqual(saved['activity']['source'], 'saved')
        self.assertTrue(saved['activity']['stale'])
        self.assertIn('down', saved['activity']['error'])
        self.assertEqual(saved['usage']['source'], 'saved')
        self.assertEqual(saved['usage']['total'], 9)
        self.assertEqual(saved['usage']['cache_read'], 3)
        self.assertAlmostEqual(saved['usage']['cost'], 0.01)

    def test_no_data_for_active_task_returns_task_state_and_nulls(self):
        task = self.task('job-0000000000000003', status='running', session_deleted=False)
        with patch.object(common, 'api', side_effect=common.HttpFailure(None, 'down')):
            snapshot = ta.snapshot(task)
        self.assertEqual(snapshot['activity']['source'], 'task')
        self.assertTrue(snapshot['activity']['stale'])
        self.assertTrue(snapshot['activity']['error'])
        self.assertEqual(snapshot['usage']['source'], 'none')
        self.assertIsNone(snapshot['usage']['total'])
        self.assertFalse(snapshot['usage']['complete'])

    def test_concurrent_snapshots_do_not_overlap_backend_fetches(self):
        task = self.task('job-0000000000000004', status='running', session_deleted=False)
        started, release = threading.Event(), threading.Event()
        running = {'now': 0, 'max': 0}
        calls = []

        def fake(path, directory=None, method='GET', data=None, timeout=15, include_cursor=False):
            running['now'] += 1
            running['max'] = max(running['max'], running['now'])
            calls.append(path)
            try:
                if '/message?limit=' in path:
                    return ([], None)
                started.set()
                self.assertTrue(release.wait(5))
                return self.session()
            finally:
                running['now'] -= 1

        results = {}

        def first():
            with patch.object(common, 'api', side_effect=fake):
                results['first'] = ta.snapshot(task)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(5))
        with patch.object(common, 'api', side_effect=fake):
            results['second'] = ta.snapshot(task)
        self.assertEqual(running['max'], 1)
        self.assertFalse(results['second']['activity']['stale'])
        self.assertIsNone(results['second']['activity']['error'])
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(running['max'], 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(results['first']['activity']['stale'])

    def test_usage_for_task_prefers_stored_snapshot_without_network(self):
        explicit = {'input': 1, 'output': 2, 'reasoning': 3, 'cache_read': 4, 'cache_write': 5,
                    'total': 15, 'cost': 0.01, 'source': 'saved', 'complete': True}
        stored = self.task('job-0000000000000005', usage=explicit)
        legacy = self.task('job-0000000000000006', tokens={'total': 9, 'input': 4, 'output': 3,
                                                           'reasoning': 2, 'cache': {'read': 0, 'write': 0}})
        with patch.object(common, 'api', side_effect=AssertionError('list must not fetch')):
            self.assertEqual(ta.usage_for_task(stored), explicit)
            self.assertEqual(ta.usage_for_task(legacy)['source'], 'task')
            self.assertEqual(ta.usage_for_task(legacy)['total'], 9)
            empty = ta.usage_for_task(self.task('job-0000000000000007'))
            self.assertEqual(empty['source'], 'none')
            self.assertFalse(empty['complete'])


class RetainedEvidenceTests(IsolatedCase):
    def test_terminal_task_uses_saved_messages_without_network(self):
        task_id = 'job-0000000000000010'
        self.write_messages(task_id, [assistant('msg_a', sample_tokens(30, 10, 5, 5, 10, 0), cost=0.02,
                                                parts=[text_part('prt_a', 'hello')])])
        task = self.task(task_id)
        with patch.object(common, 'api', side_effect=AssertionError('terminal tasks must not fetch')):
            snapshot = ta.snapshot(task)
        self.assertEqual(snapshot['usage']['source'], 'saved')
        self.assertEqual(snapshot['usage']['total'], 30)
        self.assertEqual(snapshot['usage']['cache_read'], 10)
        self.assertAlmostEqual(snapshot['usage']['cost'], 0.02)
        self.assertTrue(snapshot['usage']['complete'])
        self.assertEqual(snapshot['activity']['source'], 'saved')
        self.assertFalse(snapshot['activity']['stale'])
        self.assertIsNone(snapshot['activity']['error'])
        self.assertEqual(snapshot['activity']['sampled_at'], (self.state / 'artifacts' / task_id / 'messages.json').stat().st_mtime)

    def test_result_only_fallback_is_partial_but_explicit(self):
        task_id = 'job-0000000000000011'
        self.write_result(task_id, {
            'tokens': {'total': 42, 'input': 10, 'output': 12, 'reasoning': 20},
            'errors': [{'source': 'transport', 'code': 'http_503', 'message': 'bridge unavailable',
                        'message_id': 'msg_x'}],
            'pending': [{'id': 'que_1', 'questions': [{'question': 'Which environment should I use?'}]}],
            'worker_report': {'summary': 'Partial progress recorded.'},
        })
        task = self.task(task_id, status='needs_attention')
        with patch.object(common, 'api', side_effect=AssertionError('terminal tasks must not fetch')):
            snapshot = ta.snapshot(task)
        self.assertEqual(snapshot['usage']['source'], 'result')
        self.assertEqual(snapshot['usage']['total'], 42)
        self.assertIsNone(snapshot['usage']['cache_read'])
        self.assertIsNone(snapshot['usage']['cost'])
        self.assertFalse(snapshot['usage']['complete'])
        self.assertEqual(snapshot['activity']['source'], 'result')
        types = [event['type'] for event in snapshot['activity']['events']]
        self.assertIn('error', types)
        self.assertIn('pending', types)
        self.assertIn('assistant', types)
        error = next(event for event in snapshot['activity']['events'] if event['type'] == 'error')
        self.assertEqual(error['label'], 'transport')
        self.assertIn('bridge unavailable', error['text'])

    def test_no_session_job_shows_state_and_retained_tokens(self):
        completed = self.task('job-0000000000000012', session_id=None, session_deleted=True)
        snapshot = ta.snapshot(completed)
        self.assertEqual(snapshot['activity']['source'], 'task')
        self.assertEqual(len(snapshot['activity']['events']), 1)
        self.assertEqual(snapshot['activity']['events'][0]['status'], 'completed')
        self.assertEqual(snapshot['usage']['source'], 'none')
        legacy = self.task('job-0000000000000013', session_id=None,
                           tokens={'total': 7, 'input': 3, 'output': 2, 'reasoning': 2,
                                   'cache': {'read': 0, 'write': 0}})
        legacy_snapshot = ta.snapshot(legacy)
        self.assertEqual(legacy_snapshot['usage']['source'], 'task')
        self.assertEqual(legacy_snapshot['usage']['total'], 7)
        queued = self.task('job-0000000000000014', status='queued', session_id=None,
                           created_at=2000.0, queue_reason='waiting_for_worker_slot')
        queued_snapshot = ta.snapshot(queued)
        self.assertEqual(queued_snapshot['activity']['source'], 'task')
        statuses = [event['status'] for event in queued_snapshot['activity']['events']]
        self.assertIn('queued', statuses)
        self.assertIn('waiting_for_worker_slot',
                      ' '.join(event['text'] for event in queued_snapshot['activity']['events']))

    def test_redaction_drops_reasoning_and_tool_output_and_masks_secrets(self):
        task_id = 'job-0000000000000015'
        (self.state / 'server-password').write_text('synthetic-server-secret-value')
        messages = [
            user('msg_u', parts=[text_part('prt_u', 'prompt carrying %s and synthetic-server-secret-value' % FAKE_KEY)]),
            assistant('msg_a', sample_tokens(10, 1, 2, 3, 4, 0), cost=0.01, parts=[
                {'id': 'prt_reason', 'type': 'reasoning', 'text': 'SECRET-REASONING-MARKER'},
                tool_part('prt_tool', status='error', inputs={'command': 'echo %s' % FAKE_KEY},
                          error='boom %s' % FAKE_KEY),
                text_part('prt_text', 'key %s and synthetic-server-secret-value visible tail' % FAKE_KEY),
            ]),
        ]
        self.write_messages(task_id, messages)
        task = self.task(task_id)
        with patch.object(common, 'api', side_effect=AssertionError('terminal tasks must not fetch')):
            snapshot = ta.snapshot(task)
        blob = json.dumps(snapshot)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn('synthetic-server-secret-value', blob)
        self.assertNotIn('SECRET-REASONING-MARKER', blob)
        self.assertNotIn('RAW TOOL OUTPUT', blob)
        self.assertIn('visible tail', blob)
        self.assertNotIn('reasoning', [event['type'] for event in snapshot['activity']['events']])
        self.assertIn('<redacted>', blob)
        tool_event = next(event for event in snapshot['activity']['events'] if event['type'] == 'tool')
        self.assertEqual(tool_event['label'], 'bash')
        self.assertEqual(tool_event['status'], 'error')
        self.assertIn('echo <redacted>', tool_event['text'])
        user_event = next(event for event in snapshot['activity']['events'] if event['type'] == 'user')
        self.assertIn('prompt carrying <redacted>', user_event['text'])
        self.assertNotIn(FAKE_KEY, user_event['text'])

    def test_long_assistant_message_is_previewed_then_loaded_in_full(self):
        task_id = 'job-0000000000000016'
        full = ('Detailed progress line. ' * 30) + FAKE_KEY + ' complete tail marker'
        messages = [assistant('msg_a', sample_tokens(10, 1, 2, 3, 4, 0), cost=0.01,
                              parts=[text_part('prt_long', full)])]
        self.write_messages(task_id, messages)
        task = self.task(task_id)
        activity = ta.activity_from_messages(messages, task)
        event = next(item for item in activity['events'] if item['id'] == 'prt_long')
        self.assertTrue(event['truncated'])
        self.assertLess(len(event['text']), len(full))
        with patch.object(common, 'api', side_effect=AssertionError('terminal tasks must not fetch')):
            expanded = ta.full_event_text(task, 'prt_long')
        self.assertIn('complete tail marker', expanded)
        self.assertNotIn(FAKE_KEY, expanded)
        self.assertIn('<redacted>', expanded)

    def test_long_user_tool_guidance_and_result_error_expand_from_their_sources(self):
        task_id = 'job-0000000000000017'
        user_text = 'user context ' * 40
        tool_command = 'printf ' + ('x' * 700)
        guidance = 'guidance detail ' * 40
        error_text = 'failure detail ' * 40
        messages = [user('msg_u', parts=[text_part('prt_user', user_text)]),
                    assistant('msg_a', sample_tokens(5, 1, 1, 1, 2, 0), cost=0.01,
                              parts=[tool_part('prt_tool', inputs={'command': tool_command})])]
        task = self.task(task_id, guidance=[{'request_id': 'g1', 'text': guidance, 'status': 'accepted'}])
        self.write_messages(task_id, messages)
        self.write_result(task_id, {'errors': [{'source': 'tool', 'code': 'failed',
                                                'message': error_text, 'message_id': 'm1'}]})
        activity = ta.activity_from_messages(messages, task)
        self.assertTrue(next(e for e in activity['events'] if e['id'] == 'prt_user')['truncated'])
        self.assertTrue(next(e for e in activity['events'] if e['id'] == 'prt_tool')['truncated'])
        self.assertEqual(ta.full_event_text(task, 'prt_user'), user_text)
        self.assertEqual(ta.full_event_text(task, 'prt_tool'), tool_command)
        guidance_id = ta._stable_id('guidance', task_id, 'g1')
        error_id = ta._stable_id('result-error', 'm1', 'failed', error_text, 0)
        self.assertEqual(ta.full_event_text(task, guidance_id), guidance)
        self.assertEqual(ta.full_event_text(task, error_id), error_text)

    def test_live_full_text_survives_upstream_part_compaction(self):
        task = self.task('job-0000000000000018', status='running', session_deleted=False)
        full = 'live detail ' * 80
        messages = [assistant('msg_a', sample_tokens(5, 1, 1, 1, 2, 0), cost=0.01,
                              parts=[text_part('prt_live', full)])]
        ta.activity_from_messages(messages, task, source='live')
        with patch.object(common, 'api', return_value=[]):
            self.assertEqual(ta.full_event_text(task, 'prt_live'), full)

    def test_events_are_bounded_chronological_and_stable(self):
        parts = [tool_part('prt_%03d' % index, inputs={'command': 'echo %d' % index},
                           at=DAY_MS + index * 1000) for index in range(90)]
        messages = [assistant('msg_a', sample_tokens(1, 1, 0, 0, 0, 0), parts=parts)]
        first = ta.activity_from_messages(messages, self.task(), source='saved', sampled_at=123.0)
        second = ta.activity_from_messages(messages, self.task(), source='saved', sampled_at=123.0)
        self.assertEqual(len(first['events']), ta.MAX_EVENTS)
        self.assertTrue(first['has_more'])
        times = [event['time'] for event in first['events']]
        self.assertEqual(times, sorted(times))
        self.assertEqual([event['id'] for event in first['events']], [event['id'] for event in second['events']])
        self.assertEqual(first['events'][0]['text'], 'echo 30')
        self.assertEqual(first['events'][-1]['text'], 'echo 89')
        self.assertEqual(first['events'][0]['type'], 'tool')
        self.assertEqual(len({event['id'] for event in first['events']}), ta.MAX_EVENTS)


class WorkerSnapshotTests(IsolatedCase):
    def test_finish_persists_full_usage_snapshot(self):
        task_id = 'job-0000000000000020'
        report = {'outcome': 'done', 'summary': 'done', 'evidence': [], 'tests': [], 'unresolved': []}
        messages = [
            user('msg_u', parts=[text_part('prt_u', 'do it')]),
            assistant('msg_a', sample_tokens(50, 10, 5, 5, 30, 0), cost=0.02,
                      parts=[text_part('prt_a', json.dumps(report))], completed=DAY_MS + 1000),
        ]
        task = self.task(task_id, status='running', session_deleted=False, profile='fallback',
                         started_at=1000.0, session_id='ses_demo')
        self.write_task(task)
        with patch.object(worker, 'collect_changes', return_value={}), \
             patch.object(worker, 'config', return_value={'profiles': {'fallback': {'model': 'acme/worker'}}}):
            worker._finish(common.task(task_id), messages)
        stored = common.task(task_id)
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['usage']['input'], 10)
        self.assertEqual(stored['usage']['cache_read'], 30)
        self.assertEqual(stored['usage']['total'], 50)
        self.assertAlmostEqual(stored['usage']['cost'], 0.02)
        self.assertTrue(stored['usage']['complete'])
        self.assertEqual(common.read_json(self.state / 'artifacts' / task_id / 'result.json')['tokens'],
                         {'total': 50, 'input': 10, 'output': 5, 'reasoning': 5})
        self.assertEqual(ta.usage_for_task(stored)['total'], 50)


class ConsoleIntegrationTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.web = self.root / 'web'
        self.web.mkdir()
        (self.web / 'login.html').write_bytes(b'<html>login</html>')
        (self.web / 'login.js').write_bytes(b'/* login */')
        (self.web / 'style.css').write_bytes(b'body{}')
        (self.web / 'i18n.js').write_bytes(b'global.I18n={};')
        (self.web / 'index.html').write_bytes(b'<html>console</html>')
        (self.web / 'app.js').write_bytes(b'/* app */')
        (self.web / 'manage.js').write_bytes(b'/* manage */')
        (self.web / 'setup.js').write_bytes(b'/* setup */')
        (self.web / 'task-view.js').write_bytes(b'/* task view */')
        self.patchers.extend([
            patch.object(console, 'STATE', self.state),
            patch.object(console, 'CONFIG', self.config_path),
            patch.object(console, 'WEB', self.web),
            patch.object(quota, 'STATE', self.state),
        ])
        for item in self.patchers[2:]:
            item.start()
        (self.state / 'server-password').write_text('synthetic-server-password')
        console_auth.set_user('admin', 'synthetic-pass', self.state)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        super().tearDown()

    def start_server(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        self.server.daemon_threads = True
        port = self.server.server_port
        url = 'http://127.0.0.1:' + str(port)
        config = json.loads(self.config_path.read_text())
        config['console_url'] = url
        config['console_allowed_origins'] = [url]
        self.config_path.write_text(json.dumps(config))
        self.server.cookie_name = 'delegate_console_session_' + str(port)
        self.server.allowed_origins = console_auth.origin_tuples(config)
        self.server.trusted_proxy_networks = console_auth.trusted_proxy_networks(config)
        self.server.auth_root = self.state
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = dict(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            request_headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        result = {'status': response.status, 'headers': {k.lower(): v for k, v in response.getheaders()}, 'body': data}
        connection.close()
        return result

    def login_cookie(self):
        response = self.call('POST', '/console-api/auth/login',
                             body={'username': 'admin', 'password': 'synthetic-pass'})
        self.assertEqual(response['status'], 200)
        return response['headers']['set-cookie'].split(';')[0]

    def test_task_detail_and_state_expose_usage_without_live_fetch(self):
        task_id = 'job-0000000000000030'
        task = self.task(task_id, session_id='ses_demo')
        messages = [assistant('msg_a', sample_tokens(60, 10, 10, 10, 30, 0), cost=0.03,
                              parts=[text_part('prt_a', 'working on it')])]
        self.write_messages(task_id, messages)
        self.write_result(task_id, {'tokens': {'total': 60, 'input': 10, 'output': 10, 'reasoning': 10}})
        self.write_task(task)
        self.start_server()
        cookie = self.login_cookie()
        with patch.object(common, 'api', side_effect=AssertionError('console must not fetch for retained')):
            detail = self.call('GET', '/console-api/task/' + task_id, headers={'Cookie': cookie})
        self.assertEqual(detail['status'], 200)
        payload = json.loads(detail['body'])
        self.assertEqual(payload['usage']['source'], 'saved')
        self.assertEqual(payload['usage']['total'], 60)
        self.assertEqual(payload['activity']['source'], 'saved')
        self.assertTrue(payload['activity']['events'])
        self.assertEqual(sorted(payload['activity'].keys()),
                         ['error', 'events', 'has_more', 'sampled_at', 'source', 'stale'])
        self.assertEqual(sorted(payload['usage'].keys()),
                         ['cache_read', 'cache_write', 'complete', 'cost', 'input', 'output', 'reasoning',
                          'source', 'total'])
        asset = self.call('GET', '/console-assets/task-view.js', headers={'Cookie': cookie})
        self.assertEqual(asset['status'], 200)
        self.assertEqual(asset['body'], b'/* task view */')
        self.assertEqual(self.call('GET', '/console-assets/task-view.js')['status'], 401)
        with patch.object(console, 'service_health', return_value={'pool': True, 'server': True}), \
             patch.object(common, 'api', side_effect=AssertionError('console must not fetch for retained')):
            state = self.call('GET', '/console-api/state', headers={'Cookie': cookie})
        self.assertEqual(state['status'], 200)
        entry = json.loads(state['body'])['tasks'][0]
        self.assertEqual(entry['id'], task_id)
        # The list uses the cheap retained snapshot (cache/cost not in result.json).
        self.assertEqual(entry['usage']['total'], 60)
        self.assertEqual(entry['usage']['source'], 'result')
        self.assertIsNone(entry['usage']['cache_read'])

    def test_full_activity_message_endpoint_requires_auth_and_returns_complete_text(self):
        task_id = 'job-0000000000000031'
        full = ('long model response ' * 40) + 'endpoint tail marker'
        self.write_messages(task_id, [assistant('msg_a', sample_tokens(12, 2, 2, 2, 6, 0), cost=0.01,
                                               parts=[text_part('prt_long', full)])])
        self.write_task(self.task(task_id))
        self.start_server()
        path = '/console-api/task/' + task_id + '/event/prt_long'
        self.assertEqual(self.call('GET', path)['status'], 401)
        response = self.call('GET', path, headers={'Cookie': self.login_cookie()})
        self.assertEqual(response['status'], 200)
        self.assertEqual(json.loads(response['body'])['text'], full)


if __name__ == '__main__':
    unittest.main()
