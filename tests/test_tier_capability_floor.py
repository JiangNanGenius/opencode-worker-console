"""Read-only route previews for mandatory Deep/Ultra capability floors."""
import copy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install
import quota


def config():
    return {
        'profiles': {
            'fallback': {'model': 'deepseek/deepseek-flash'},
            'senior-code': {'model': 'kimi-for-coding/kimi-for-coding'},
            'aliyun-flash': {'model': 'bailian-token-plan/qwen3.8-flash'},
            'aliyun-max': {'model': 'bailian-token-plan/qwen3.8-max'},
            'ultra-reasoning': {'model': 'kimi-for-coding/k3.1-pro'},
        },
        'routing': {'fast': 'fallback', 'background': 'senior-code',
                    'deep': 'aliyun-max', 'ultra': 'ultra-reasoning'},
        'routing_policy': {
            'fast': [[{'profile': 'fallback', 'weight': 1}]],
            'background': [[{'profile': 'senior-code', 'weight': 1}],
                           [{'profile': 'fallback', 'weight': 1}]],
            'deep': [[{'profile': 'aliyun-max', 'weight': 4}],
                     [{'profile': 'senior-code', 'weight': 1},
                      {'profile': 'aliyun-flash', 'weight': 1}],
                     [{'profile': 'fallback', 'weight': 1}]],
            'ultra': [[{'profile': 'ultra-reasoning', 'weight': 1}],
                      [{'profile': 'aliyun-max', 'weight': 1}]],
        }}


class TierCapabilityFloorTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('billing_block', None), ('tier_guidance', {}),
                            ('kimi_request_allowed', (True, 'quota_available'))]:
            mock = patch.object(quota, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def quotas(self, kimi=True, bailian=True):
        now = time.time()
        return {name: {'available': value, 'state': 'ok', 'sampled_at': now,
                       'stale': False, 'windows': []}
                for name, value in [('deepseek', True), ('kimi-for-coding', kimi),
                                    ('bailian-token-plan', bailian)]}

    def test_deep_implicit_floor_blocks_every_lower_profile(self):
        c = config()
        for floor in (None, 'fast', 'normal', 'deep'):
            task = {'tier': 'deep', 'requested_profile': 'auto', 'capability_floor': floor}
            for profile in ('senior-code', 'aliyun-flash', 'fallback'):
                self.assertFalse(quota.meets_capability_floor(profile, task, c))
            self.assertIsNone(quota.route(task, c, self.quotas(bailian=False))[0])
            self.assertTrue(quota.meets_capability_floor('aliyun-max', task, c))
            self.assertTrue(quota.meets_capability_floor('ultra-reasoning', task, c))

    def test_ultra_default_floor_allows_only_the_authorized_deep_landing(self):
        c = config()
        for floor in (None, 'fast', 'normal', 'deep'):
            task = {'tier': 'ultra', 'requested_profile': 'auto', 'capability_floor': floor}
            # The implicit Ultra minimum is Deep: the Deep primary (Max) is the
            # one authorized capacity landing; nothing below it is ever eligible.
            self.assertTrue(quota.meets_capability_floor('aliyun-max', task, c))
            self.assertFalse(quota.meets_capability_floor('aliyun-flash', task, c))
            self.assertTrue(quota.meets_capability_floor('ultra-reasoning', task, c))
            # A generic available=False with no fresh zero window is not proof,
            # so even a default Ultra task keeps waiting rather than downgrading.
            self.assertIsNone(quota.route(task, c, self.quotas(kimi=False))[0])
        # An explicit Ultra floor is a hard requirement: Max never satisfies it.
        hard = {'tier': 'ultra', 'requested_profile': 'auto', 'capability_floor': 'ultra'}
        self.assertFalse(quota.meets_capability_floor('aliyun-max', hard, c))
        self.assertIsNone(quota.route(hard, c, self.quotas(kimi=False))[0])
        self.assertEqual(quota.route({'tier': 'ultra', 'requested_profile': 'auto'},
                                     c, self.quotas())[0], 'ultra-reasoning')

    def test_retained_legacy_deep_task_gets_floor_without_resaving(self):
        task = {'complexity': 'deep', 'urgency': 'background', 'requested_profile': 'auto'}
        self.assertIsNone(quota.route(task, config(), self.quotas(bailian=False))[0])
        self.assertNotIn('tier', task)
        self.assertNotIn('capability_floor', task)

    def test_conservation_replacement_cannot_bypass_deep_floor(self):
        c = config()
        c['routing_policy']['background'][0].append({'profile': 'aliyun-flash', 'weight': 1})
        with patch.object(quota, 'tier_guidance', return_value={'conservation_level': 2}):
            profile, _reason = quota.route({'tier': 'deep', 'requested_profile': 'auto'},
                                            c, self.quotas())
        self.assertEqual(profile, 'aliyun-max')
        with patch.object(quota, 'tier_guidance', return_value={'conservation_level': 2}):
            self.assertIsNone(quota.route({'tier': 'deep', 'requested_profile': 'auto'},
                                          c, self.quotas(bailian=False))[0])

    def test_normal_and_fast_keep_existing_fallback_behavior(self):
        c = config()
        self.assertEqual(quota.route({'tier': 'normal', 'requested_profile': 'auto'},
                                     c, self.quotas(kimi=False))[0], 'fallback')
        self.assertEqual(quota.route({'tier': 'fast', 'requested_profile': 'auto'},
                                     c, self.quotas())[0], 'fallback')
        self.assertFalse(quota.meets_capability_floor('fallback',
                         {'tier': 'normal', 'capability_floor': 'normal'}, c))

    def test_legacy_three_key_single_routes_remain_valid(self):
        c = config()
        c.pop('routing_policy')
        c['routing'].pop('ultra')
        c['profiles'].pop('ultra-reasoning')
        for tier, expected in [('fast', 'fallback'), ('normal', 'senior-code'), ('deep', 'aliyun-max')]:
            self.assertEqual(quota.route({'tier': tier, 'requested_profile': 'auto'},
                                         c, self.quotas())[0], expected)

    def test_migration_removes_deep_downgrades_preserving_weight_and_other_tiers(self):
        c = config()
        previous = copy.deepcopy(c['routing_policy'])
        self.assertTrue(install.migrate_kimi_four_tiers(c))
        self.assertEqual(c['routing_policy']['deep'], [[{'profile': 'aliyun-max', 'weight': 4}]])
        for tier in ('fast', 'background', 'ultra'):
            self.assertEqual(c['routing_policy'][tier], previous[tier])
        self.assertFalse(install.migrate_kimi_four_tiers(c))

    def test_migration_selects_available_qwen_when_old_deep_has_only_low_profiles(self):
        c = config()
        c['routing_policy']['deep'] = [[{'profile': 'fallback', 'weight': 1}]]
        install.migrate_kimi_four_tiers(c)
        self.assertEqual(c['routing_policy']['deep'], [[{'profile': 'aliyun-max', 'weight': 1}]])


if __name__ == '__main__':
    unittest.main()
