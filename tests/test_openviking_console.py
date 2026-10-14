"""Console memory bridge: verified session links, capability gating and legacy fallback."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import memory_manager


class SessionLinkTests(unittest.TestCase):
    def setUp(self):
        import console
        self.console = console
        self.console._MEMORY_SESSIONS.update(at=0.0, map={})

    def test_verified_session_gets_a_same_origin_link(self):
        with patch.object(self.console.management, 'sessions',
                          return_value={'sessions': [{'id': 'ses_abc', 'directory': '/Volumes/proj'}]}):
            link = self.console.memory_session_link('ses_abc')
        self.assertTrue(link['verified'])
        self.assertTrue(link['url'].startswith('/'))
        self.assertIn('/session/ses_abc', link['url'])
        self.assertNotIn('/Volumes', link['url'])

    def test_unknown_session_is_never_linked(self):
        with patch.object(self.console.management, 'sessions', return_value={'sessions': []}):
            link = self.console.memory_session_link('mem_legacy_session')
        self.assertFalse(link['verified'])
        self.assertIsNone(link['url'])

    def test_unverifiable_state_never_fabricates_a_link(self):
        with patch.object(self.console.management, 'sessions', side_effect=RuntimeError('opencode down')):
            link = self.console.memory_session_link('ses_abc')
        self.assertFalse(link['verified'])
        self.assertIsNone(link['url'])

    def test_empty_and_malformed_ids_do_not_call_opencode(self):
        with patch.object(self.console.management, 'sessions', side_effect=AssertionError('must not call')):
            self.assertIsNone(self.console.memory_session_link(''))
            malformed = self.console.memory_session_link('ses with spaces')
        self.assertFalse(malformed['verified'])
        self.assertIsNone(malformed['url'])


class CapabilityGateTests(unittest.TestCase):
    def test_native_only_surfaces_require_openviking(self):
        with patch.object(memory_manager, 'backend', return_value='opencode-mem'):
            with self.assertRaises(ValueError):
                memory_manager.detail('mem_1')
            with self.assertRaises(ValueError):
                memory_manager.activity()
            with self.assertRaises(ValueError):
                memory_manager.capacity()

    def test_legacy_browse_stays_flat_and_never_claims_native_navigation(self):
        legacy = {'items': [{'id': 'mem_1', 'content': 'x' * 800, 'isPinned': True,
                             'tags': ['team=core'], 'projectName': 'Project', 'updatedAt': 10}],
                  'total': 1, 'page': 1, 'pageSize': 25, 'totalPages': 1}
        with patch.object(memory_manager, 'backend', return_value='opencode-mem'), \
             patch.object(memory_manager, 'list_memories', return_value=legacy):
            result = memory_manager.browse(page=1, page_size=25)
        self.assertTrue(result['legacy'])
        self.assertEqual(result['categories'], [])
        self.assertEqual(result['directories'], [])
        self.assertEqual(result['items'][0]['id'], 'mem_1')
        self.assertLessEqual(len(result['items'][0]['preview']), 360)
        self.assertTrue(result['items'][0]['isPinned'])

    def test_legacy_semantic_browse_uses_search(self):
        with patch.object(memory_manager, 'backend', return_value='opencode-mem'), \
             patch.object(memory_manager, 'search',
                          return_value={'items': [{'id': 'mem_9', 'content': 'found'}], 'total': 1}) as search:
            result = memory_manager.browse(query='needle', page_size=10)
        search.assert_called_once()
        self.assertEqual(result['items'][0]['id'], 'mem_9')
        self.assertTrue(result['legacy'])


if __name__ == '__main__':
    unittest.main()
