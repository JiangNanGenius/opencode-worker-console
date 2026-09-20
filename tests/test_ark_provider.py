"""Provider boundary: correct API, context, reasoning and no embedded credentials."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server
from providers import ARK_PROVIDER


class ArkProviderTests(unittest.TestCase):
    def test_ark_coexists_with_kimi_without_changing_worker_permissions(self):
        overlay = server.runtime_overlay({'profiles': {
            'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'max'},
            'ark-auto': {'model': ARK_PROVIDER + '/ark-code-latest', 'variant': 'max'},
            'ark-k3': {'model': ARK_PROVIDER + '/kimi-k3', 'variant': 'max'},
        }})
        self.assertIn('kimi-for-coding', overlay['provider'])
        ark = overlay['provider'][ARK_PROVIDER]
        self.assertEqual(ark['npm'], '@ai-sdk/openai')
        self.assertEqual(ark['options'], {'baseURL': 'https://ark.cn-beijing.volces.com/api/plan/v3'})
        self.assertEqual(ark['models']['ark-code-latest']['limit']['context'], 1024000)
        self.assertEqual(ark['models']['kimi-k3']['limit']['context'], 1024000)
        self.assertNotIn('apiKey', json.dumps(overlay))
        for model in ark['models'].values():
            self.assertEqual(model['variants']['max']['reasoningEffort'], 'max')
        for agent in overlay['agent'].values():
            self.assertEqual(agent['permission'], 'allow')
            self.assertNotIn('steps', agent)

    def test_ark_is_opt_in(self):
        overlay = server.runtime_overlay({'profiles': {'custom': {'model': 'custom/model'}}})
        self.assertNotIn(ARK_PROVIDER, overlay.get('provider', {}))


if __name__ == '__main__':
    unittest.main()
