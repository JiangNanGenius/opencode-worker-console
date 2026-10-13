"""OpenViking memory backend: journal, tags, deletion and retention semantics."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import openviking_backend as backend


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'memory': {
            'enabled': True, 'backend': 'openviking', 'embedding_dimensions': 2048,
            'rolling': {'enabled': True, 'max_storage_mb': 1, 'max_memories': 10,
                        'target_percent': 90, 'check_interval_hours': 24},
            'openviking': {'url': 'http://127.0.0.1:1933'}}}))
        self.state = self.root / 'state'
        self.ov_home = self.root / 'ov'
        import openviking_service
        self.patchers = [patch.object(common, 'CONFIG', self.config),
                         patch.object(common, 'STATE', self.state),
                         patch.object(openviking_service, 'OV_HOME', self.ov_home)]
        for patcher in self.patchers:
            patcher.start()
        backend._invalidate()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()


class ParseTests(BackendTestCase):
    def test_status_is_json_serializable_for_console_api(self):
        with patch.object(backend.openviking_service, 'probe',
                          return_value={'healthy': True, 'ready': True, 'health': {'auth_mode': 'api_key'}}), \
             patch.object(backend, 'stats', return_value={'total': 0}):
            result = backend.status()
        self.assertIsInstance(result['storage_path'], str)
        json.dumps(result)

    def test_item_reads_migrated_iso_timestamps(self):
        item = backend._item('viking://user/default/memories/experiences/a.md',
                             {'created_at': '2026-09-01T10:00:00+00:00',
                              'updated_at': '2026-09-02T10:00:00+00:00'}, 'body')
        self.assertEqual(item['createdAt'], '2026-09-01T10:00:00+00:00')
        self.assertEqual(item['updatedAt'], '2026-09-02T10:00:00+00:00')

    def test_memory_fields_roundtrip(self):
        fields = {'memory_type': 'experiences', 'legacy_id': 'mem_1', 'pinned': True,
                  'created_at': '2026-01-01T00:00:00+00:00'}
        body = backend.compose('# Title\n\nBody text.', fields)
        content, parsed = backend.parse_memory(body)
        self.assertEqual(content, '# Title\n\nBody text.')
        self.assertEqual(parsed['legacy_id'], 'mem_1')
        self.assertTrue(parsed['pinned'])

    def test_plain_content_reads_as_is(self):
        content, parsed = backend.parse_memory('no footer here')
        self.assertEqual(content, 'no footer here')
        self.assertEqual(parsed, {})

    def test_tags_are_strict_and_scoped(self):
        self.assertEqual(backend.sanitize_tag('ios'), 'tag=ios')
        self.assertEqual(backend.sanitize_tag('project=opencode_project_abc'), 'project=opencode_project_abc')
        self.assertIsNone(backend.sanitize_tag('  '))
        merged = backend.merge_tags(['team=core', 'bad value'], ['project=x'])
        self.assertIn('team=core', merged)
        self.assertIn('project=x', merged)
        self.assertNotIn('bad value', merged)

    def test_item_marks_pinned_from_tags(self):
        item = backend._item('viking://user/default/memories/experiences/a.md', {},
                             'text', {'tags': ['pinned=true', 'project=opencode_project_x']})
        self.assertTrue(item['isPinned'])
        self.assertIn('project=opencode_project_x', item['tags'])


class JournalTests(BackendTestCase):
    def test_unpin_updates_retrieval_tag_and_journal_without_replacing_body(self):
        uri = 'viking://user/default/memories/experiences/pinned.md'
        backend._journal_upsert({'id': 'legacy_id', 'uri': uri, 'pinned': 1})
        requests = []
        with patch.object(backend, 'attrs', return_value={'tags': ['pinned=true', 'project=test']}), \
             patch.object(backend, 'read', return_value=('body', {'pinned': True})), \
             patch.object(backend, 'request', side_effect=lambda *args, **kwargs: requests.append(args) or {}), \
             patch.object(backend, 'write') as write:
            backend.pin(uri, False)
        write.assert_not_called()
        self.assertNotIn('pinned=true', requests[0][2]['tags'])
        with backend.journal() as con:
            self.assertEqual(con.execute('SELECT pinned FROM records WHERE id=?', ('legacy_id',)).fetchone()[0], 0)

    def test_update_uses_native_body_only_replace_and_journal_metadata(self):
        uri = 'viking://user/default/memories/experiences/a.md'
        backend._journal_upsert({'id': 'legacy_id', 'uri': uri, 'origin': 'legacy-migration', 'pinned': 0})
        data = {'body': 'old'}
        native_fields = {'pinned': True, 'created_at': '2026-09-01T10:00:00+00:00'}
        def request(path, method='GET', payload=None, **kwargs):
            if '/content/read?' in path:
                return backend.compose(data['body'], native_fields)
            if '/fs/attrs?' in path:
                return {'attrs': {'tags': []}}
            if path == '/api/v1/content/write':
                self.assertNotIn('MEMORY_FIELDS', payload['content'])
                data['body'] = payload['content']
                return {}
            raise AssertionError(path)
        with patch.object(backend, 'request', side_effect=request):
            backend.update(uri, 'new')
            content, fields = backend.read(uri)
        self.assertEqual(content, 'new')
        self.assertFalse(fields['pinned'])
        self.assertEqual(fields['legacy_id'], 'legacy_id')

    def test_native_pin_is_retained_by_capacity_maintenance(self):
        uri = 'viking://user/default/memories/experiences/native.md'
        with patch.object(backend, 'attrs', return_value={'tags': []}), \
             patch.object(backend, 'read', return_value=('body', {})), \
             patch.object(backend, 'request', return_value={}), \
             patch.object(backend, 'write', return_value={}):
            backend.pin(uri, True)
        with backend.journal() as con:
            self.assertEqual(con.execute('SELECT pinned FROM records WHERE uri=?', (uri,)).fetchone()[0], 1)

    def test_delete_tombstones_legacy_id(self):
        backend._journal_upsert({'id': 'mem_legacy_1', 'uri': 'viking://user/default/memories/experiences/x.md',
                                 'category': 'experiences', 'origin': 'legacy-migration'})
        calls = []
        with patch.object(backend, 'request', side_effect=lambda *args, **kwargs: calls.append(args) or {}):
            backend.delete('viking://user/default/memories/experiences/x.md')
        self.assertTrue(backend.is_tombstoned('mem_legacy_1'))
        with backend.journal() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        self.assertEqual(calls[0][1], 'DELETE')

    def test_bulk_delete_collects_errors(self):
        def fake_delete(memory_id):
            if 'bad' in memory_id:
                raise ValueError('nope')
            return {'deleted': memory_id}
        with patch.object(backend, 'delete', side_effect=fake_delete):
            result = backend.bulk_delete(['viking://user/default/memories/experiences/ok.md',
                                          'viking://user/default/memories/experiences/bad.md'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(len(result['errors']), 1)

    def test_add_composes_footer_and_journals(self):
        wrote = {}

        def fake_write(uri, content, **kwargs):
            wrote['uri'], wrote['content'] = uri, content
            return {}

        def fake_read(uri):
            return backend.parse_memory(wrote['content'])

        with patch.object(backend, 'write', side_effect=fake_write), \
             patch.object(backend, 'read', side_effect=fake_read), \
             patch.object(backend, 'uuid4', None, create=True):
            result = backend.add('Remember the retention rule.', None, tags=['team=core'], memory_type='experiences')
        self.assertTrue(result['uri'].startswith('viking://user/default/memories/experiences/'))
        self.assertIn('MEMORY_FIELDS', wrote['content'])
        fields = json.loads(wrote['content'].split('MEMORY_FIELDS', 1)[1].rsplit('-->', 1)[0])
        self.assertEqual(fields['memory_type'], 'experiences')
        with backend.journal() as con:
            row = con.execute('SELECT origin, memory_type FROM records WHERE uri=?', (result['uri'],)).fetchone()
        self.assertEqual(row, ('bridge', 'experiences'))


class ListingTests(BackendTestCase):
    def test_management_read_requests_raw_memory_fields(self):
        from urllib.parse import urlsplit, parse_qs
        seen = []
        with patch.object(backend, 'request', side_effect=lambda path, **kwargs: seen.append(path) or backend.compose('body', {'pinned': True})):
            content, fields = backend.read('viking://user/default/memories/experiences/a.md')
        self.assertEqual(content, 'body')
        self.assertTrue(fields['pinned'])
        self.assertEqual(parse_qs(urlsplit(seen[0]).query)['raw'], ['true'])

    def test_project_scope_is_encoded_as_an_openviking_key_value_tag(self):
        from urllib.parse import urlsplit, parse_qs
        seen = []
        with patch.object(backend, 'request', side_effect=lambda path, **kwargs: seen.append(path) or []):
            backend._memory_files(tag='opencode_project_x', use_cache=False)
        self.assertEqual(parse_qs(urlsplit(seen[0]).query)['tags'], ['project=opencode_project_x'])

    def test_search_keeps_project_filter_and_migrated_provenance(self):
        fields = {'legacy_id': 'mem_old', 'session_id': 'ses_old',
                  'project_path': '/Volumes/project', 'created_at': '2026-09-01T10:00:00+00:00'}
        response = {'memories': [{'uri': 'viking://user/default/memories/experiences/a.md',
                                  'content': backend.compose('body', fields)}]}
        seen = []
        with patch.object(backend, 'request', side_effect=lambda *args, **kwargs: seen.append(args[2]) or response):
            result = backend.search('topic', tag='opencode_project_x')
        self.assertEqual(seen[0]['tags'], ['project=opencode_project_x'])
        self.assertEqual(result['items'][0]['legacyId'], 'mem_old')
        self.assertEqual(result['items'][0]['sessionId'], 'ses_old')
        self.assertEqual(result['items'][0]['projectPath'], '/Volumes/project')

    def test_listing_retries_when_native_node_limit_truncates(self):
        from urllib.parse import urlsplit, parse_qs
        limits = []
        def request(path, **kwargs):
            limit = int(parse_qs(urlsplit(path).query)['node_limit'][0])
            limits.append(limit)
            return [{'uri': 'viking://user/default/memories/experiences/%d.md' % n}
                    for n in range(min(limit, 5001))]
        with patch.object(backend, 'request', side_effect=request):
            files = backend._memory_files(use_cache=False)
        self.assertEqual(len(files), 5001)
        self.assertEqual(limits, [5000, 10000])

    def test_list_scopes_with_tag_argument(self):
        seen = {}

        def fake_files(tag=None, use_cache=True):
            seen['tag'] = tag
            return [{'uri': 'viking://user/default/memories/experiences/a.md', 'size': 10, 'mtime': 1.0}]

        with patch.object(backend, '_memory_files', side_effect=fake_files), \
             patch.object(backend, 'read', return_value=('body', {'memory_type': 'experiences'})), \
             patch.object(backend, 'attrs', return_value={'tags': ['project=opencode_project_x']}):
            result = backend.list_memories(tag='opencode_project_x')
        self.assertEqual(seen['tag'], 'opencode_project_x')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['content'], 'body')

    def test_search_rejects_empty_query(self):
        with self.assertRaises(ValueError):
            backend.search('   ')


class MaintenanceTests(BackendTestCase):
    def test_retention_tombstones_legacy_source_id(self):
        uri = self._files()[0]['uri']
        backend._journal_upsert({'id': 'legacy_old', 'uri': uri, 'origin': 'legacy-migration'})
        with patch.object(backend, '_memory_files', return_value=self._files()), \
             patch.object(backend, 'request', return_value={}):
            result = backend.maintain(force=True)
        self.assertTrue(backend.is_tombstoned('legacy_old'))
        self.assertEqual(result['deleted'], 1)

    def _files(self):
        return [{'uri': 'viking://user/default/memories/experiences/old.md', 'size': 400000, 'mtime': 1.0},
                {'uri': 'viking://user/default/memories/experiences/pinned.md', 'size': 400000, 'mtime': 2.0},
                {'uri': 'viking://user/default/memories/experiences/new.md', 'size': 400000, 'mtime': 3.0}]

    def test_retention_reclaims_oldest_unpinned_and_respects_pins(self):
        backend._journal_upsert({'id': 'viking://user/default/memories/experiences/pinned.md',
                                 'uri': 'viking://user/default/memories/experiences/pinned.md',
                                 'pinned': 1, 'origin': 'bridge'})
        deleted = []

        def fake_request(path, method='GET', data=None, timeout=60):
            deleted.append(path)
            return {}

        with patch.object(backend, '_memory_files', return_value=self._files()), \
             patch.object(backend, 'request', side_effect=fake_request), \
             patch.object(backend, '_tombstone'):
            result = backend.maintain(force=True)
        self.assertEqual(result['deleted'], 1)
        self.assertIn('old.md', deleted[0])
        state = json.loads((self.state / 'memory-maintenance.json').read_text())
        self.assertEqual(state['deleted'], 1)

    def test_retention_skips_when_under_capacity(self):
        small = [{'uri': 'viking://user/default/memories/experiences/one.md', 'size': 100, 'mtime': 1.0}]
        with patch.object(backend, '_memory_files', return_value=small), \
             patch.object(backend, 'request', side_effect=AssertionError('must not delete')):
            result = backend.maintain(force=True)
        self.assertEqual(result['deleted'], 0)


if __name__ == '__main__':
    unittest.main()


class AuthHeaderTests(BackendTestCase):
    def test_requests_carry_the_data_key_when_present(self):
        import openviking_service
        seen = {}

        def fake_request(url, method='GET', data=None, headers=None, timeout=15):
            seen['headers'] = headers
            return {'status': 'ok', 'result': []}

        with patch.object(openviking_service, 'api_key', return_value='secret-data-key'), \
             patch.object(common, 'request', side_effect=fake_request):
            backend.request('/api/v1/fs/ls')
        self.assertEqual(seen['headers'], {'X-API-Key': 'secret-data-key'})

    def test_requests_omit_auth_header_without_a_key(self):
        import openviking_service
        seen = {}

        def fake_request(url, method='GET', data=None, headers=None, timeout=15):
            seen['headers'] = headers
            return {'status': 'ok', 'result': []}

        with patch.object(openviking_service, 'api_key', return_value=None), \
             patch.object(common, 'request', side_effect=fake_request):
            backend.request('/api/v1/fs/ls')
        self.assertIsNone(seen['headers'])
