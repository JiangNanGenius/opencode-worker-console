"""Ultra capacity fallback through real routing and durable task state, without I/O."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bailian_pool
import common
import delegate
import quota
import routing
import worker

KIMI = 'kimi-for-coding'
BAILIAN = 'bailian-token-plan'
IDENTITY = 'fixture-current-credential'


class UltraFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.now = 2000000000.0
        self.c = {
            'auto_approve': False, 'max_parallel_per_owner': 4,
            'kimi_weekly_reserve_dynamic': False,
            'routing': {'ultra': 'ultra', 'deep': 'max', 'background': 'flash'},
            'profiles': {
                'ultra': {'model': routing.KIMI_RESERVED_MODEL, 'variant': 'max', 'enabled': True},
                'max': {'model': BAILIAN + '/qwen3.8-max', 'variant': 'max', 'enabled': True},
                'flash': {'model': BAILIAN + '/qwen3.8-flash', 'enabled': True}},
            'routing_policy': {
                'ultra': [[{'profile': 'ultra', 'weight': 1}]],
                'deep': [[{'profile': 'max', 'weight': 1}]],
                'background': [[{'profile': 'flash', 'weight': 1}]]}}
        for module in (common, delegate, quota, routing, worker):
            p = patch.object(module, 'STATE', self.state)
            p.start()
            self.addCleanup(p.stop)
        for module in (common, delegate, quota, worker):
            p = patch.object(module, 'config', side_effect=lambda: self.c)
            p.start()
            self.addCleanup(p.stop)
        for p in (
                patch.object(common, 'CONFIG', Path(self.tmp.name) / 'config.json'),
                patch.object(quota.time, 'time', side_effect=lambda: self.now),
                patch.object(quota, 'credential_identity', return_value=IDENTITY),
                patch.object(quota, '_telemetry_identity', return_value=IDENTITY),
                patch.object(worker, 'credential_identity', return_value=IDENTITY),
                patch.object(bailian_pool, 'recovery_available', return_value={'configured': False})):
            p.start()
            self.addCleanup(p.stop)
        common.init()
        self.raw = {
            KIMI: self.reading([self.window('window_0', 80, 300),
                                self.window('overall', 80, 10080)]),
            BAILIAN: self.reading([self.window('monthly', 60, 43200)])}
        self.t = {
            'id': 'job-ultra-fallback', 'title': 'Finish accepted deployment',
            'status': 'running', 'created_at': self.now - 300,
            'objective': 'Verify the remaining acceptance; deployment was completed.',
            'acceptance': ['Existing deployment is verified without repeating it.'],
            'mode': 'write', 'workspace': 'shared', 'commands': [],
            'scopes': ['owned.txt'], 'targets': ['fixture-service'], 'resources': ['fixture'],
            'directory': self.tmp.name, 'source_dir': self.tmp.name, 'owner_thread_id': 'owner',
            'session_id': 'existing-session', 'message_id': 'existing-message',
            'dispatch_attempted_at': self.now - 200, 'profile': 'ultra',
            'tier': 'ultra', 'requested_profile': 'auto', 'auto_approve': False,
            '_billing_dispatch': {'provider': KIMI, 'identity': IDENTITY, 'at': self.now - 200}}
        common.write_json(common.task_path(self.t['id']), self.t)
        self.messages = [{'info': {'id': 'exhausted-response', 'role': 'assistant',
                                   'parentID': self.t['message_id'], 'providerID': KIMI,
                                   'modelID': 'k3.1-pro', 'error': {'name': 'APIError', 'data': {
                                       'statusCode': 429,
                                       'message': "You've reached your 5-hour usage limit for this usage window."}}},
                          'parts': [{'type': 'tool', 'tool': 'bash', 'callID': 'already-deployed',
                                     'state': {'status': 'completed', 'output': 'private tool output'}}]}]
        self.save()

    def window(self, name, remaining, minutes):
        return {'name': name, 'valid': True, 'limit': 100, 'remaining': remaining,
                'remaining_percent': remaining, 'duration_minutes': minutes,
                'resets_at': self.now + 3600}

    def reading(self, windows):
        return {'state': 'ok', 'available': True, 'sampled_at': self.now - 1,
                'checked_at': self.now - 1, '_credential': IDENTITY, 'windows': windows}

    def save(self):
        common.write_json(self.state / 'quota.json', self.raw)

    def view(self):
        self.save()
        result = quota.view(copy.deepcopy(self.raw))
        self.assertNotIn('_credential', result[KIMI])
        return result

    def route(self, task=None):
        return quota.route(task or self.t, self.c, self.view())[0]

    def deplete(self, provider=KIMI, index=0):
        self.raw[provider]['windows'][index].update(remaining=0, remaining_percent=0)
        self.raw[provider]['available'] = False
        self.save()

    def block(self, reason):
        common.write_json(self.state / quota.BILLING, {
            KIMI: {'credential': IDENTITY, 'opened_at': self.now - 10,
                   'reason': reason, 'message_ids': ['confirmed-model-error']}})

    def park_with_max_also_empty(self):
        self.deplete()
        self.deplete(BAILIAN)
        self.assertTrue(worker.reroute_after_capacity_stop(
            common.task(self.t['id']), KIMI, 'provider_usage_or_rate_limit', self.messages))
        waiting = common.task(self.t['id'])
        self.assertEqual(waiting['_capacity_wait']['phase'], 'waiting')
        self.assertEqual(waiting['session_id'], 'existing-session')
        return waiting

    def restore_max(self):
        self.now += 5
        self.raw[BAILIAN] = self.reading([self.window('monthly', 60, 43200)])
        self.save()

    def test_healthy_ultra_is_preferred_without_consuming_preview_counters(self):
        self.assertEqual(self.route(), 'ultra')
        self.assertFalse((self.state / routing.COUNTERS).exists())

    def test_new_task_real_view_zero_five_hour_and_weekly_route_to_max(self):
        original = copy.deepcopy(self.raw)
        for index in (0, 1):
            with self.subTest(window=index):
                self.raw = copy.deepcopy(original)
                self.deplete(index=index)
                self.assertEqual(self.route(), 'max')
                self.assertEqual(self.t['tier'], 'ultra')

    def test_verified_fresh_real_month_pool_zero_routes_to_max(self):
        self.raw[KIMI]['account'] = {
            'state': 'ok', 'sampled_at': self.now - 1,
            'account_binding': {'state': 'verified'},
            'windows': [{'name': 'monthTotal', 'kind': 'month_total', 'remaining_percent': 0}]}
        self.assertEqual(self.route(), 'max')

    def test_stale_month_pool_zero_does_not_authorize_downgrade(self):
        self.raw[KIMI]['account'] = {
            'state': 'ok', 'sampled_at': self.now - 901,
            'account_binding': {'state': 'verified'},
            'windows': [{'name': 'monthTotal', 'kind': 'month_total', 'remaining_percent': 0}]}
        self.assertNotEqual(self.route(), 'max')

    def test_confirmed_monthly_and_balance_circuits_route_to_max(self):
        for reason in (quota.MONTHLY_REASON, quota.BALANCE_REASON):
            with self.subTest(reason=reason):
                self.block(reason)
                self.assertEqual(self.route(), 'max')

    def test_stale_future_unbound_or_rotated_zero_cannot_authorize_downgrade(self):
        self.deplete()
        original = copy.deepcopy(self.raw)
        for override in ({'sampled_at': self.now - 901}, {'sampled_at': self.now + 1},
                         {'_credential': None}, {'_credential': 'another-credential'}):
            with self.subTest(override=override):
                self.raw = copy.deepcopy(original)
                self.raw[KIMI].update(override)
                self.assertNotEqual(self.route(), 'max')

    def test_generic_unavailable_without_zero_is_not_capacity_exhaustion(self):
        self.raw[KIMI]['available'] = False
        self.assertIsNone(self.route())

    def test_auth_telemetry_and_auth_circuit_win_over_zero(self):
        self.deplete()
        self.raw[KIMI]['state'] = 'auth_error'
        self.assertIsNone(self.route())
        self.raw[KIMI]['state'] = 'ok'
        self.block(quota.AUTH_REASON)
        self.assertIsNone(self.route())

    def test_explicit_pins_ultra_floor_and_disabled_fallback_wait(self):
        self.deplete()
        for override in ({'requested_profile': 'ultra'}, {'capability_floor': 'ultra'}):
            with self.subTest(override=override):
                self.assertIsNone(self.route(dict(self.t, **override)))
        self.c['ultra_fallback_to_deep'] = False
        self.assertIsNone(self.route())

    def test_manual_exclusions_and_disabled_ultra_do_not_authorize_downgrade(self):
        self.deplete()
        for override in ({'excluded_providers': [KIMI]}, {'excluded_profiles': ['ultra']}):
            with self.subTest(override=override):
                self.assertIsNone(self.route(dict(self.t, **override)))
        self.c['profiles']['ultra']['enabled'] = False
        self.assertIsNone(self.route())

    def test_no_flash_or_unknown_deep_primary_can_receive_ultra_fallback(self):
        self.deplete()
        for model in (BAILIAN + '/qwen3.8-flash', 'custom/unknown-flash'):
            with self.subTest(model=model):
                self.c['profiles']['max']['model'] = model
                self.assertIsNone(self.route())

    def test_both_pools_empty_keep_original_task_waiting(self):
        waiting = self.park_with_max_also_empty()
        candidate, profile, _ = worker.capacity_resume_candidate(waiting, self.c, self.view())
        self.assertIsNone(profile)
        self.assertEqual(candidate['id'], self.t['id'])
        self.assertEqual(candidate['session_id'], self.t['session_id'])
        self.assertEqual(candidate['tier'], 'ultra')

    def test_legacy_single_mapping_supports_same_guarded_max_fallback(self):
        self.c.pop('routing_policy')
        self.deplete()
        self.assertEqual(self.route(), 'max')
        self.assertIsNone(self.route(dict(self.t, capability_floor='ultra')))

    def test_existing_provenance_waiter_can_resume_max_without_new_kimi_sample(self):
        waiting = self.park_with_max_also_empty()
        self.restore_max()
        candidate, profile, _ = worker.capacity_resume_candidate(waiting, self.c, self.view())
        self.assertEqual(profile, 'max')
        self.assertEqual(candidate['excluded_providers'], [KIMI])
        self.assertEqual(candidate['tier'], 'ultra')

    def test_legacy_manual_exclusion_still_does_not_authorize_max(self):
        self.c.pop('routing_policy')
        self.deplete()
        self.assertNotEqual(self.route(dict(self.t, excluded_providers=[KIMI])), 'max')

    def test_recorded_error_outweighs_positive_cache_from_before_the_failure(self):
        waiting = self.park_with_max_also_empty()
        self.restore_max()
        self.raw[KIMI] = self.reading([self.window('window_0', 80, 300),
                                       self.window('overall', 80, 10080)])
        self.raw[KIMI]['sampled_at'] = self.now - 60
        _, profile, _ = worker.capacity_resume_candidate(waiting, self.c, self.view())
        self.assertEqual(profile, 'max')
        waiting['_capacity_exclusions'][KIMI]['identity'] = 'old-credential'
        _, profile, _ = worker.capacity_resume_candidate(waiting, self.c, self.view())
        self.assertIsNone(profile)

    def test_capacity_downgrade_is_same_session_checkpointed_and_never_replayed(self):
        self.park_with_max_also_empty()
        self.restore_max()
        before = common.task(self.t['id'])
        with patch.object(worker, 'call', return_value={}) as call:
            self.assertTrue(worker.resume_capacity_wait(before, self.messages, {'type': 'busy'}))
            call.assert_not_called()
            self.assertTrue(worker.resume_capacity_wait(before, self.messages, {'type': 'idle'}))
            sent = common.task(self.t['id'])
            self.assertEqual(call.call_count, 1)
            body = call.call_args.args[3]
            self.assertEqual(body['model']['providerID'], BAILIAN)
            self.assertEqual(body['model']['modelID'], 'qwen3.8-max')
            self.assertIn('already-deployed', body['parts'][0]['text'])
            self.assertNotIn('private tool output', body['parts'][0]['text'])
            for key in ('id', 'session_id', 'tier', 'scopes', 'targets', 'resources',
                        'owner_thread_id', 'auto_approve'):
                self.assertEqual(sent[key], before[key], key)
            for _ in range(3):
                worker.resume_capacity_wait(common.task(sent['id']), self.messages, {'type': 'idle'})
            self.assertEqual(call.call_count, 1)
            ack = [{'info': {'id': 'max-ack', 'role': 'assistant', 'parentID': sent['message_id']}}]
            self.assertFalse(worker.resume_capacity_wait(common.task(sent['id']), ack, {'type': 'busy'}))
            self.assertEqual(call.call_count, 1)

    def test_lost_max_continuation_ack_is_observed_without_resend(self):
        self.park_with_max_also_empty()
        self.restore_max()
        with patch.object(worker, 'call', side_effect=common.HttpFailure(None, 'lost')) as call:
            worker.resume_capacity_wait(common.task(self.t['id']), self.messages, {'type': 'idle'})
            intent = common.task(self.t['id'])
            self.assertEqual(intent['status'], 'uncertain')
            for _ in range(3):
                worker.resume_capacity_wait(common.task(self.t['id']), self.messages, {'type': 'idle'})
            self.assertEqual(call.call_count, 1)

    def recovered_fallback(self):
        exclusion_at = self.now - 180
        t = dict(self.t, profile='max', excluded_providers=[KIMI], fallback_used=True,
                 _capacity_exclusions={KIMI: {'identity': IDENTITY, 'at': exclusion_at,
                                              'reason': 'provider_usage_or_rate_limit'}},
                 route_history=[{'from_provider': KIMI, 'from_profile': 'ultra',
                                 'to_profile': 'max', 'at': exclusion_at,
                                 'trigger': 'provider_usage_or_rate_limit'}])
        common.write_json(common.task_path(t['id']), t)
        self.save()
        checkpoint = [{'info': {'id': 'owned-max-checkpoint', 'role': 'assistant',
                                'parentID': t['message_id'], 'time': {'completed': self.now - 1}},
                       'parts': [{'type': 'text', 'text': 'Acceptance remains; deployment already completed.'}]}]
        return t, checkpoint

    def test_fresh_recovery_returns_ultra_only_at_owned_idle_checkpoint(self):
        t, checkpoint = self.recovered_fallback()
        with patch.object(worker, 'call', return_value={}) as call:
            self.assertFalse(worker.proactive_reroute_if_needed(t, {'type': 'busy'}, checkpoint))
            self.assertTrue(worker.proactive_reroute_if_needed(t, {'type': 'idle'}, checkpoint))
            call.assert_not_called()
            staged = common.task(t['id'])
            self.assertEqual(staged['_capacity_wait']['trigger'], 'preferred_route_restored')
            worker.resume_capacity_wait(staged, checkpoint, {'type': 'idle'})
            sent = common.task(t['id'])
            self.assertEqual(call.call_args.args[3]['model']['modelID'], 'k3.1-pro')
            self.assertEqual(sent['session_id'], t['session_id'])
            self.assertEqual(sent['tier'], 'ultra')
            worker.resume_capacity_wait(sent, checkpoint, {'type': 'idle'})
            self.assertEqual(call.call_count, 1)

    def test_legacy_fallback_returns_to_recovered_ultra_at_idle_checkpoint(self):
        self.c.pop('routing_policy')
        t, checkpoint = self.recovered_fallback()
        self.assertTrue(worker.proactive_reroute_if_needed(t, {'type': 'idle'}, checkpoint))
        self.assertEqual(common.task(t['id'])['_capacity_wait']['preferred_profile'], 'ultra')

    def test_recovery_never_restarts_unowned_final_or_retiering_turn(self):
        t, checkpoint = self.recovered_fallback()
        variants = [
            (dict(t, status='completed'), checkpoint),
            (dict(t, tier_change={'status': 'pending'}), checkpoint),
            (dict(t, message_id='newer-owner-message'), checkpoint),
            (t, [{'info': checkpoint[0]['info'], 'parts': [{'type': 'text', 'text': json.dumps({
                'outcome': 'done', 'summary': 'Accepted', 'evidence': [], 'tests': [], 'unresolved': []})}]}])]
        with patch.object(worker, 'call') as call:
            for task, messages in variants:
                with self.subTest(status=task.get('status'), owner=task.get('message_id')):
                    common.write_json(common.task_path(task['id']), task)
                    self.assertFalse(worker.proactive_reroute_if_needed(task, {'type': 'idle'}, messages))
            call.assert_not_called()

    def test_stale_or_old_positive_quota_cannot_return_ultra(self):
        t, checkpoint = self.recovered_fallback()
        for sampled in (self.now - 901, self.now - 200):
            with self.subTest(sampled=sampled):
                self.raw[KIMI]['sampled_at'] = sampled
                self.save()
                common.write_json(common.task_path(t['id']), t)
                self.assertFalse(worker.proactive_reroute_if_needed(t, {'type': 'idle'}, checkpoint))


if __name__ == '__main__':
    unittest.main()
