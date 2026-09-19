"""Runtime overlay contract: workers never receive an upstream step cap.

OpenCode's agent `steps` option limits iterations; omitting it lets the agent
run until the model stops or the user interrupts. Legacy max_steps values on
disk (80, any positive cap, or 0) must never be forwarded.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server


def profile(model='acme/worker', variant=None):
    spec = {'model': model, 'label': model.split('/', 1)[1]}
    if variant:
        spec['variant'] = variant
    return spec


class RuntimeOverlayTests(unittest.TestCase):
    def overlay(self, auto_approve=True, max_steps=None):
        c = {'profiles': {'fast-code': profile(),
                          'deep-research': profile('acme/deep', variant='max')},
             'auto_approve': auto_approve}
        if max_steps is not None:
            c['max_steps'] = max_steps
        return server.runtime_overlay(c)

    def assert_no_steps(self, overlay):
        self.assertEqual(set(overlay['agent']), {'fast-code', 'deep-research'})
        for name, agent in overlay['agent'].items():
            self.assertNotIn('steps', agent, name + ' must omit the upstream step cap')

    def test_legacy_80_positive_and_zero_caps_are_all_omitted(self):
        for max_steps in (80, 200, 1, 0):
            with self.subTest(max_steps=max_steps):
                self.assert_no_steps(self.overlay(max_steps=max_steps))

    def test_missing_max_steps_is_omitted(self):
        self.assert_no_steps(self.overlay())

    def test_original_kimi_profile_survives_catalog_provider_rename(self):
        overlay = server.runtime_overlay({'profiles': {
            'senior-code': profile('kimi-for-coding/kimi-for-coding', 'max'),
            'deep-research': profile('kimi-for-coding/k3', 'max')}})
        provider = overlay['provider']['kimi-for-coding']
        self.assertEqual(provider['options'], {'baseURL': 'https://api.kimi.com/coding/v1'})
        self.assertEqual(set(provider['models']), {'kimi-for-coding', 'k3'})
        self.assertNotIn('apiKey', str(provider))
        for agent in overlay['agent'].values():
            self.assertEqual(agent['variant'], 'max')
            self.assertNotIn('steps', agent)

    def test_variant_and_overlay_defaults_preserved(self):
        overlay = self.overlay(max_steps=80)
        fast = overlay['agent']['fast-code']
        deep = overlay['agent']['deep-research']
        self.assertEqual(fast['model'], 'acme/worker')
        self.assertNotIn('variant', fast)
        self.assertEqual(deep['model'], 'acme/deep')
        self.assertEqual(deep['variant'], 'max')
        self.assertEqual(overlay['permission'], 'allow')
        self.assertEqual(fast['permission'], 'allow')
        self.assertEqual(overlay['$schema'], 'https://opencode.ai/config.json')
        self.assertEqual(overlay['share'], 'disabled')
        self.assertFalse(overlay['autoupdate'])
        self.assertFalse(overlay['snapshot'])
        self.assertFalse(overlay['formatter'])

    def test_permissions_still_follow_auto_approve_without_steps(self):
        restricted = self.overlay(auto_approve=False, max_steps=0)
        self.assert_no_steps(restricted)
        self.assertEqual(restricted['permission'], 'ask')
        for agent in restricted['agent'].values():
            self.assertEqual(agent['permission']['*'], 'deny')
            self.assertEqual(agent['permission']['read'], 'allow')
            self.assertNotIn('bash', agent['permission'])


if __name__ == '__main__':
    unittest.main()
