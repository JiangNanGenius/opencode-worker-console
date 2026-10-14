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

        def fake_request(path, method='GET', data=None, timeout=60):
            return [] if method == 'GET' else {}

        with patch.object(backend, '_memory_files', return_value=self._files()), \
             patch.object(backend, 'request', side_effect=fake_request):
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
            if method == 'GET':
                return []  # verified native pinned-tag listing: nothing pinned there
            return {}

        with patch.object(backend, '_memory_files', return_value=self._files()), \
             patch.object(backend, 'request', side_effect=fake_request), \
             patch.object(backend, '_tombstone'):
            result = backend.maintain(force=True)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(any('old.md' in call for call in deleted))
        state = json.loads((self.state / 'memory-maintenance.json').read_text())
        self.assertEqual(state['deleted'], 1)

    def test_retention_skips_when_under_capacity(self):
        small = [{'uri': 'viking://user/default/memories/experiences/one.md', 'size': 100, 'mtime': 1.0}]
        with patch.object(backend, '_memory_files', return_value=small), \
             patch.object(backend, 'request', side_effect=AssertionError('must not delete')):
            result = backend.maintain(force=True)
        self.assertEqual(result['deleted'], 0)

    def test_native_pinned_listing_failure_fails_closed(self):
        uris = ['viking://user/default/memories/experiences/old1.md',
                'viking://user/default/memories/experiences/old2.md',
                'viking://user/default/memories/experiences/mid.md',
                'viking://user/default/memories/experiences/new.md']
        files = [{'uri': uri, 'size': 400000, 'mtime': float(index + 1)}
                 for index, uri in enumerate(uris)]
        delete_calls = []

        def fake_request(path, method='GET', data=None, timeout=60):
            if method == 'GET':
                raise ValueError('pinned listing transport failure')
            delete_calls.append(path)
            return {}

        with patch.object(backend, '_memory_files', return_value=files), \
             patch.object(backend, 'request', side_effect=fake_request):
            result = backend.maintain(force=True)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['planned'], 2)
        self.assertIn('no records were reclaimed', result['error'])
        self.assertEqual(delete_calls, [])
        state = json.loads((self.state / 'memory-maintenance.json').read_text())
        self.assertEqual(state['deleted'], 0)
        self.assertIn('error', state)

    def test_native_pinned_listing_errors_are_never_silently_empty(self):
        with patch.object(backend, 'request', return_value={}):
            with self.assertRaises(ValueError):
                backend._native_pinned_uris()

    def test_partial_failure_keeps_real_deleted_count_and_bytes(self):
        uris = ['viking://user/default/memories/experiences/old1.md',
                'viking://user/default/memories/experiences/old2.md',
                'viking://user/default/memories/experiences/mid.md',
                'viking://user/default/memories/experiences/new.md']
        files = [{'uri': uri, 'size': 400000, 'mtime': float(index + 1)}
                 for index, uri in enumerate(uris)]
        deleted_calls = []

        def fake_request(path, method='GET', data=None, timeout=60):
            if method == 'GET':
                return []  # verified native pinned-tag listing
            if 'old1.md' in path:
                raise ValueError('simulated transport failure')
            deleted_calls.append(path)
            return {}

        with patch.object(backend, '_memory_files', return_value=files), \
             patch.object(backend, 'request', side_effect=fake_request):
            result = backend.maintain(force=True)
        self.assertEqual(result['planned'], 2)
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['after'], 3)
        self.assertEqual(result['estimated_bytes_after'],
                         result['estimated_bytes_before'] - (400000 + 2048 * 4 + 4096))
        self.assertEqual(len(deleted_calls), 1)
        self.assertIn('old2.md', deleted_calls[0])


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


class UriValidationTests(BackendTestCase):
    def test_rejects_traversal_encoded_escapes_and_foreign_namespaces(self):
        invalid = [
            'viking://user/default/memories/../../etc/passwd',
            'viking://user/default/memories/%2e%2e/secret.md',
            'viking://user/default/memories/experiences/..%2fsecret.md',
            'viking://user/default/sessions/some.md',
            'viking://user/default/memoriesX/other.md',
            'viking://user/other/memories/a.md',
            'viking://user/default/memories/a.md?raw=true',
            'viking://user/default/memories/a.md#fragment',
            'viking://user/default/memories/a\\..\\b.md',
            'viking://user/default/memories/',
            '/Users/somewhere/notes.md',
            'viking://user/default/memories',
            'viking://user/default/memories/experiences/\x01bad.md',
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    backend.validate_memory_uri(value)
                with self.assertRaises(ValueError):
                    backend.validate_memory_file(value)

    def test_accepts_only_real_memory_files(self):
        uri = 'viking://user/default/memories/experiences/migrated-01.md'
        self.assertEqual(backend.validate_memory_file(uri), uri)
        with self.assertRaises(ValueError):
            backend.validate_memory_file('viking://user/default/memories/experiences')
        with self.assertRaises(ValueError):
            backend.validate_memory_file('viking://user/default/memories/experiences/.overview.md')
        self.assertEqual(backend.validate_memory_uri('viking://user/default/memories', allow_root=True),
                         'viking://user/default/memories')

    def test_management_actions_validate_before_any_native_request(self):
        calls = []
        bad = 'viking://user/default/sessions/steal.md'
        with patch.object(backend, 'request', side_effect=lambda *a, **k: calls.append(a)):
            for action in (lambda: backend.update(bad, 'new'),
                           lambda: backend.delete(bad),
                           lambda: backend.pin(bad, True),
                           lambda: backend.detail(bad)):
                with self.assertRaises(ValueError):
                    action()
        self.assertEqual(calls, [])


class DeepNativeTreeTests(BackendTestCase):
    def test_date_organized_events_are_visible_and_protected_in_every_inventory(self):
        from urllib.parse import parse_qs, urlsplit
        uri = backend.MEMORY_ROOT + '/events/2026/10/13/decision.md'
        entry = {'uri': uri, 'rel_path': 'events/2026/10/13/decision.md',
                 'isDir': False, 'size': 120, 'modTime': '2026-10-13T10:00:00Z',
                 'tags': ['pinned=true']}

        def native_listing(path, **kwargs):
            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            # fs/ls and a depth-3 tree omit this real native layout.
            if parsed.path != '/api/v1/fs/tree' or int(query.get('level_limit', ['3'])[0]) < 5:
                return []
            return [entry]

        with patch.object(backend, 'request', side_effect=native_listing):
            files = backend._memory_files(use_cache=False)
            index = backend._memory_index(use_cache=False)
            pins = backend._native_pinned_uris()
        self.assertEqual([row['uri'] for row in files], [uri])
        self.assertEqual([row['uri'] for row in index['files']], [uri])
        self.assertIn(uri, pins)
        self.assertIn(uri, index['pinned'])


class BrowseTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.files = [
            {'uri': 'viking://user/default/memories/preferences/default/pin.md', 'rel': 'preferences/default/pin.md',
             'name': 'pin.md', 'category': 'preferences', 'folder': 'preferences/default',
             'size': 100, 'mtime': 4.0, 'tags': ['project=opencode_project_x']},
            {'uri': 'viking://user/default/memories/experiences/a.md', 'rel': 'experiences/a.md',
             'name': 'a.md', 'category': 'experiences', 'folder': 'experiences',
             'size': 200, 'mtime': 3.0, 'tags': ['project=opencode_project_x']},
            {'uri': 'viking://user/default/memories/experiences/sub/b.md', 'rel': 'experiences/sub/b.md',
             'name': 'b.md', 'category': 'experiences', 'folder': 'experiences/sub',
             'size': 300, 'mtime': 2.0, 'tags': []},
            {'uri': 'viking://user/default/memories/events/c.md', 'rel': 'events/c.md',
             'name': 'c.md', 'category': 'events', 'folder': 'events',
             'size': 400, 'mtime': 1.0, 'tags': []},
        ]
        self.index = {
            'at': 0.0, 'files': self.files,
            'dirs': [
                {'uri': 'viking://user/default/memories/experiences', 'rel': 'experiences',
                 'name': 'experiences', 'depth': 1},
                {'uri': 'viking://user/default/memories/experiences/sub', 'rel': 'experiences/sub',
                 'name': 'sub', 'depth': 2},
                {'uri': 'viking://user/default/memories/events', 'rel': 'events', 'name': 'events', 'depth': 1},
                {'uri': 'viking://user/default/memories/preferences', 'rel': 'preferences',
                 'name': 'preferences', 'depth': 1},
                {'uri': 'viking://user/default/memories/preferences/default', 'rel': 'preferences/default',
                 'name': 'default', 'depth': 2},
            ],
            'journal': {
                'viking://user/default/memories/experiences/sub/b.md':
                    {'project_tag': 'opencode_project_y', 'pinned': False, 'origin': 'legacy-migration',
                     'id': 'mem_b', 'project_path': '/Volumes/b', 'project_name': 'B project'},
            },
            'pinned': {'viking://user/default/memories/preferences/default/pin.md'},
        }

    def test_pagination_and_newest_first_order(self):
        with patch.object(backend, '_memory_index', return_value=self.index), \
             patch.object(backend, '_preview', return_value='preview'):
            first = backend.browse(page=1, page_size=2)
            second = backend.browse(page=2, page_size=2)
        self.assertEqual((first['total'], first['totalPages']), (4, 2))
        self.assertEqual([item['rel'] for item in first['items']],
                         ['preferences/default/pin.md', 'experiences/a.md'])
        self.assertEqual([item['rel'] for item in second['items']],
                         ['experiences/sub/b.md', 'events/c.md'])
        self.assertTrue(first['items'][0]['isPinned'])

    def test_category_and_project_filters_intersect_with_faceted_counts(self):
        with patch.object(backend, '_memory_index', return_value=self.index), \
             patch.object(backend, '_preview', return_value='preview'):
            scoped = backend.browse(category='experiences', project='opencode_project_x', page_size=10)
            no_category = backend.browse(project='opencode_project_x', page_size=10)
            folder = backend.browse(folder='experiences/sub', page_size=10)
        self.assertEqual([item['rel'] for item in scoped['items']], ['experiences/a.md'])
        self.assertEqual(scoped['total'], 1)
        # Faceted counts apply the project scope to the category facet.
        self.assertEqual({entry['name']: entry['count'] for entry in scoped['categories']},
                         {'experiences': 1, 'preferences': 1})
        # The journal fallback attributes a legacy record without a native tag.
        self.assertEqual({entry['tag']: entry['count'] for entry in no_category['projects']},
                         {'opencode_project_x': 2, 'opencode_project_y': 1})
        self.assertTrue(any(entry['rel'] == 'experiences/sub' and entry['count'] == 1
                            for entry in folder['directories']))
        self.assertEqual([item['rel'] for item in folder['items']], ['experiences/sub/b.md'])
        self.assertEqual(folder['items'][0]['projectTag'], 'opencode_project_y')
        self.assertEqual(folder['items'][0]['projectName'], 'B project')

    def test_previews_are_read_only_for_the_visible_page(self):
        calls = []

        def fake_preview(uri, **kwargs):
            calls.append(uri)
            return 'p'

        with patch.object(backend, '_memory_index', return_value=self.index), \
             patch.object(backend, '_preview', side_effect=fake_preview):
            result = backend.browse(page=2, page_size=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item['preview'] for item in result['items']], ['p', 'p'])

    def test_unknown_sort_and_bad_category_are_rejected(self):
        with patch.object(backend, '_memory_index', return_value=self.index):
            with self.assertRaises(ValueError):
                backend.browse(sort='random')
            with self.assertRaises(ValueError):
                backend.browse(category='../experiences')
            with self.assertRaises(ValueError):
                backend.browse(folder='experiences/../../etc')


class DetailTests(BackendTestCase):
    def test_detail_unwraps_native_attrs_and_preserves_source_id(self):
        uri = 'viking://user/default/memories/entities/tool/openviking.md'
        levels = {'l0': {'available': False, 'text': None, 'uri': uri.rsplit('/', 1)[0],
                         'scope': 'directory', 'aggregate': True, 'reason': 'not ready'},
                  'l1': {'available': True, 'text': 'Overview', 'uri': uri.rsplit('/', 1)[0],
                         'scope': 'directory', 'aggregate': True}}
        with patch.object(backend, 'read', return_value=('body', {})), \
             patch.object(backend, 'attrs', return_value={
                 'tags': ['memory_type=entities', 'pinned=true'],
                 'memory': {'category': 'tool', 'name': 'openviking',
                            'source_extraction_id': 'extract-1', 'version': 2, 'memory_type': 'entities'}}), \
             patch.object(backend, '_directory_levels', return_value=levels):
            detail = backend.detail(uri)
        self.assertEqual(detail['tags'], ['memory_type=entities', 'pinned=true'])
        self.assertEqual(detail['native']['memory']['source_extraction_id'], 'extract-1')
        self.assertEqual(detail['sourceId'], 'extract-1')
        self.assertFalse(detail['levels']['l0']['available'])
        self.assertIsNone(detail['levels']['l0']['text'])
        self.assertEqual(detail['levels']['l2']['chars'], 4)
        self.assertEqual(detail['origin'], 'openviking')

    def test_detail_reports_missing_native_metadata_without_inventing_it(self):
        uri = 'viking://user/default/memories/experiences/a.md'
        with patch.object(backend, 'read', return_value=('body', {'legacy_id': 'mem_1', 'session_id': 'ses_1',
                                                                  'origin': 'legacy-migration'})), \
             patch.object(backend, 'attrs', side_effect=ValueError('native attrs unavailable')), \
             patch.object(backend, '_directory_levels', return_value={
                 'l0': {'available': False, 'text': None, 'reason': 'not ready'},
                 'l1': {'available': False, 'text': None, 'reason': 'not ready'}}):
            detail = backend.detail(uri)
        self.assertFalse(detail['native']['available'])
        self.assertEqual(detail['tags'], [])
        self.assertIsNone(detail['sourceId'])
        self.assertEqual(detail['legacyId'], 'mem_1')
        self.assertEqual(detail['sessionId'], 'ses_1')

    def test_directory_detail_uses_native_levels_and_counts_subtree(self):
        index = {'files': [
            {'uri': 'viking://user/default/memories/entities/a.md', 'rel': 'entities/a.md', 'name': 'a.md',
             'category': 'entities', 'folder': 'entities', 'size': 10, 'mtime': 1.0, 'tags': []},
            {'uri': 'viking://user/default/memories/entities/sub/b.md', 'rel': 'entities/sub/b.md', 'name': 'b.md',
             'category': 'entities', 'folder': 'entities/sub', 'size': 10, 'mtime': 2.0, 'tags': []},
        ], 'dirs': [], 'journal': {}, 'pinned': set()}
        levels = {'l0': {'available': True, 'text': 'Abstract',
                         'uri': 'viking://user/default/memories/entities',
                         'scope': 'directory', 'aggregate': True},
                  'l1': {'available': True, 'text': 'Overview',
                         'uri': 'viking://user/default/memories/entities',
                         'scope': 'directory', 'aggregate': True}}
        with patch.object(backend, '_memory_index', return_value=index), \
             patch.object(backend, '_directory_levels', return_value=levels):
            detail = backend.detail('viking://user/default/memories/entities')
        self.assertEqual(detail['kind'], 'directory')
        self.assertEqual(detail['records'], 2)
        self.assertTrue(detail['levels']['l0']['available'])
        self.assertTrue(detail['levels']['l1']['aggregate'])
        self.assertFalse(detail['levels']['l2']['available'])


class CapacityTests(BackendTestCase):
    def _over_index(self):
        files = [
            {'uri': 'viking://user/default/memories/experiences/old.md', 'rel': 'experiences/old.md',
             'name': 'old.md', 'category': 'experiences', 'folder': 'experiences',
             'size': 400000, 'mtime': 1.0, 'tags': []},
            {'uri': 'viking://user/default/memories/experiences/mid.md', 'rel': 'experiences/mid.md',
             'name': 'mid.md', 'category': 'experiences', 'folder': 'experiences',
             'size': 400000, 'mtime': 2.0, 'tags': []},
            {'uri': 'viking://user/default/memories/experiences/native-pin.md',
             'rel': 'experiences/native-pin.md', 'name': 'native-pin.md', 'category': 'experiences',
             'folder': 'experiences', 'size': 400000, 'mtime': 0.5, 'tags': ['pinned=true']},
        ]
        return {'at': 0.0, 'files': files, 'dirs': [], 'journal': {},
                'pinned': {'viking://user/default/memories/experiences/native-pin.md'}}

    def test_capacity_preview_is_non_mutating_and_protects_native_pins(self):
        with patch.object(backend, '_memory_index', return_value=self._over_index()), \
             patch.object(backend, '_workspace_bytes', return_value={'path': '/tmp/ws', 'bytes': 5, 'files': 1}), \
             patch.object(backend, 'request', side_effect=AssertionError('preview must not call native APIs')), \
             patch.object(common, 'write_json', side_effect=AssertionError('preview must not write state')):
            result = backend.capacity()
        self.assertTrue(result['over_capacity'])
        self.assertEqual(result['preview']['pinned'], 1)
        self.assertEqual(result['preview']['candidates'], 1)
        self.assertEqual(result['preview']['candidate_bytes'], 400000 + 2048 * 4 + 4096)
        self.assertEqual(result['logical']['records'], 3)

    def test_retention_plan_never_reclaims_native_pinned_or_journal_pinned(self):
        files = self._over_index()['files']
        pinned = {'viking://user/default/memories/experiences/native-pin.md',
                  'viking://user/default/memories/experiences/mid.md'}
        policy = {'max_storage_mb': 1, 'max_memories': 100000, 'target_percent': 90,
                  'check_interval_hours': 24}
        plan = backend._retention_plan(files, pinned, policy, 2048)
        self.assertTrue(plan['over'])
        self.assertEqual([item['rel'] for item in plan['remove']], ['experiences/old.md'])

    def test_pinned_uris_union_journal_and_native_tags(self):
        backend._journal_upsert({'id': 'a',
                                 'uri': 'viking://user/default/memories/experiences/a.md', 'pinned': 1})
        files = [{'uri': 'viking://user/default/memories/experiences/b.md', 'tags': ['pinned=true']},
                 {'uri': 'viking://user/default/memories/experiences/c.md', 'tags': []}]
        pinned = backend._pinned_uris(files)
        self.assertEqual(pinned, {'viking://user/default/memories/experiences/a.md',
                                  'viking://user/default/memories/experiences/b.md'})

    def test_workspace_measurement_excludes_paths_outside_the_workspace(self):
        import openviking_service
        workspace = self.root / 'ov-workspace'
        (workspace / 'nested').mkdir(parents=True)
        (workspace / 'nested/one.bin').write_bytes(b'x' * 10)
        (self.root / 'migration-backup').mkdir()
        (self.root / 'migration-backup/huge.bin').write_bytes(b'y' * 1000)
        with patch.object(openviking_service, '_settings', return_value={'workspace': str(workspace)}):
            measured = backend._workspace_bytes()
        self.assertEqual(measured['bytes'], 10)
        self.assertEqual(measured['files'], 1)
        self.assertEqual(measured['path'], str(workspace))


class ActivityTests(BackendTestCase):
    def test_activity_reports_truthful_queue_session_and_index_counts(self):
        tasks = [
            {'task_id': 't1', 'task_type': 'session_commit', 'status': 'pending', 'resource_id': 's1',
             'updated_at': 10.0, 'stage': 'queued', 'result': {}},
            {'task_id': 't2', 'task_type': 'session_commit', 'status': 'running', 'resource_id': 's2',
             'updated_at': 20.0, 'stage': 'extracting', 'result': {}},
            {'task_id': 't3', 'task_type': 'session_commit', 'status': 'failed', 'resource_id': 's3',
             'updated_at': 30.0, 'stage': 'failed', 'error': 'extraction failed', 'result': {}},
            {'task_id': 't4', 'task_type': 'session_commit', 'status': 'completed', 'resource_id': 's4',
             'updated_at': 40.0, 'stage': 'completed',
             'result': {'memories_extracted': {'memory_write': 2, 'memory_edit': 1}}},
            {'task_id': 't5', 'task_type': 'other', 'status': 'failed', 'updated_at': 50.0},
        ]

        def fake_request(path, method='GET', data=None, timeout=60):
            if path.startswith('/api/v1/tasks?'):
                return tasks
            if path == '/api/v1/sessions':
                return [{'session_id': 's1'}, {'session_id': 's2'}]
            if path == '/api/v1/stats/memories':
                return {'total_memories': 22, 'by_category': {'events': 22}}
            raise AssertionError(path)

        with patch.object(backend, 'request', side_effect=fake_request):
            result = backend.activity()
        self.assertTrue(result['available'])
        self.assertEqual(result['queue']['pending'], 1)
        self.assertEqual(result['queue']['running'], 1)
        self.assertEqual(result['queue']['failed'], 1)
        self.assertEqual(result['queue']['completed'], 1)
        self.assertEqual(result['queue']['total'], 4)
        self.assertEqual(result['sessions']['registered'], 2)
        self.assertEqual(result['indexed']['total_memories'], 22)
        self.assertEqual(result['recent'][0]['memories'], 3)
        self.assertIsNone(result['recent'][0]['error'])

    def test_activity_degrades_without_fabricating_counts(self):
        with patch.object(backend, 'request', side_effect=ValueError('task queue offline')):
            result = backend.activity()
        self.assertFalse(result['available'])
        self.assertIn('offline', result['error'])
        self.assertEqual(result['recent'], [])
        self.assertEqual(result['sessions']['registered'], 0)
        self.assertIsNone(result['indexed'])
