"""Migration protocol: idempotent journal, resumable chunks, tombstones, verification."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import openviking_backend as backend
import openviking_migrate as migrate


def legacy_item(memory_id='mem_1', project='/Users/example/project'):
    return {'id': memory_id, 'type': 'memory', 'content': '## Request\n\nKeep the SQLite journal.',
            'memoryType': 'technical', 'tags': "['ios', 'testing']",
            'metadata': json.dumps({'source': 'auto-capture', 'sessionID': 'ses_abc'}),
            'createdAt': '2026-09-01T10:00:00.000Z', 'updatedAt': '2026-09-02T10:00:00.000Z',
            'displayName': 'Worker Desk', 'projectPath': project, 'projectName': 'project',
            'isPinned': True}


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'memory': {
            'enabled': True, 'backend': 'opencode-mem',
            'storage_path': str(self.root / 'legacy'),
            'api_url': 'http://127.0.0.1:4747',
            'openviking': {'url': 'http://127.0.0.1:1933'}}}))
        self.state = self.root / 'state'
        self.ov_home = self.root / 'ov'
        self.legacy = self.root / 'legacy'
        self.legacy.mkdir(parents=True)
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


class RecordTests(MigrationTestCase):
    def test_record_maps_project_category_and_provenance(self):
        project = self.root / 'project'
        project.mkdir()
        with patch.object(migrate.memory_manager, 'project_info',
                          return_value={'tag': 'opencode_project_abc', 'projectPath': str(project),
                                        'projectName': 'project'}):
            record = migrate._record(legacy_item(project=str(project)))
        self.assertTrue(record['uri'].startswith(
            'viking://user/default/memories/experiences/'))
        self.assertEqual(record['project_tag'], 'opencode_project_abc')
        self.assertIn('project=opencode_project_abc', record['tags'])
        self.assertIn('pinned=true', record['tags'])
        self.assertIn('origin=legacy-migration', record['tags'])
        self.assertIn('legacy_type=technical', record['tags'])
        self.assertIn('tag=ios', record['tags'])
        content, fields = backend.parse_memory(record['content'])
        self.assertEqual(content, '## Request\n\nKeep the SQLite journal.')
        self.assertEqual(fields['legacy_id'], 'mem_1')
        self.assertEqual(fields['session_id'], 'ses_abc')
        self.assertTrue(fields['pinned'])

    def test_record_keeps_external_project_when_directory_missing(self):
        record = migrate._record(legacy_item(project='/Volumes/gone/project'))
        self.assertIsNone(record['project_tag'])
        self.assertEqual(record['project_path'], '/Volumes/gone/project')
        content, fields = backend.parse_memory(record['content'])
        self.assertEqual(fields['project_path'], '/Volumes/gone/project')


class PlanTests(MigrationTestCase):
    def test_plan_skips_mapped_and_tombstoned(self):
        backend._journal_upsert({'id': 'mem_1', 'uri': 'viking://user/default/memories/experiences/a.md',
                                 'origin': 'legacy-migration'})
        backend._tombstone('mem_2', 'viking://user/default/memories/experiences/b.md')
        items = [legacy_item('mem_1'), legacy_item('mem_2'), legacy_item('mem_3')]
        with patch.object(migrate, 'legacy_items', return_value=items), \
             patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}):
            plan = migrate.plan()
        self.assertEqual([item['id'] for item in plan['items']], ['mem_3'])
        self.assertEqual(plan['mapped'], 1)
        self.assertEqual(plan['tombstoned'], 1)


class OfflineDeltaTests(MigrationTestCase):
    def test_consistent_backup_contains_wal_data_without_old_wal_files(self):
        db = self.legacy / 'memories.db'
        writer = sqlite3.connect(db)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE records(id)')
        writer.execute('INSERT INTO records VALUES(42)')
        writer.commit()
        destination = self.root / 'backup'
        try:
            migrate.backup(dest=destination)
            copy = sqlite3.connect(destination / 'memories.db')
            try:
                self.assertEqual(copy.execute('SELECT id FROM records').fetchall(), [(42,)])
            finally:
                copy.close()
            self.assertFalse((destination / 'memories.db-wal').exists())
            self.assertFalse((destination / 'memories.db-shm').exists())
        finally:
            writer.close()

    def test_backup_failure_never_reports_a_consistent_manifest(self):
        (self.legacy / 'broken.db').write_bytes(b'not sqlite')
        destination = self.root / 'broken-backup'
        with self.assertRaises(ValueError):
            migrate.backup(dest=destination)
        self.assertFalse((destination / 'manifest.json').exists())

    def test_stopped_store_reads_wal_and_preserves_provenance(self):
        db = self.legacy / 'project_shard_0.db'
        writer = sqlite3.connect(db)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE memories(id,content,type,tags,created_at,updated_at,'
                       'metadata,display_name,project_path,project_name,is_pinned,container_tag)')
        writer.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('mem_1', 'keep me', 'technical', '["ios"]', 1790000000000, 1790000000001,
                        '{"sessionID":"ses_abc"}', 'project', '/Volumes/project', 'project', 1,
                        'opencode_project_abc'))
        writer.commit()
        try:
            with patch.object(migrate.memory_manager, 'legacy_request',
                              side_effect=AssertionError('retired API must not be contacted')):
                items = migrate.legacy_items(offline=True)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['id'], 'mem_1')
            fields = backend.parse_memory(migrate._record(items[0])['content'])[1]
            self.assertTrue(fields['pinned'])
            self.assertEqual(fields['session_id'], 'ses_abc')
            self.assertEqual(fields['project_path'], '/Volumes/project')
        finally:
            writer.close()

    def test_unreadable_shard_cannot_be_treated_as_empty(self):
        (self.legacy / 'broken.db').write_bytes(b'not sqlite')
        with self.assertRaises(ValueError):
            migrate.reconcile_deletions(offline=True)

    def test_no_shards_cannot_be_treated_as_empty(self):
        with self.assertRaises(ValueError):
            migrate.legacy_items(offline=True)


class RunTests(MigrationTestCase):
    def _run(self, items):
        calls = {'batch': [], 'tag': [], 'write': []}

        def fake_request(path, method='GET', data=None, timeout=60):
            if 'batch-write' in path:
                calls['batch'].append(data)
                return {}
            if 'set_tags' in path:
                calls['tag'].append(data)
                return {}
            raise AssertionError('unexpected request ' + path)

        with patch.object(migrate, 'legacy_items', return_value=items), \
             patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(backend, 'request', side_effect=fake_request), \
             patch.object(backend, 'write', side_effect=lambda *a, **k: calls['write'].append(a) or {}), \
             patch.object(migrate.memory_manager, 'project_info',
                          return_value={'tag': 'opencode_project_abc', 'projectPath': '/Users/example/project',
                                        'projectName': 'project'}):
            first = migrate.run(batch=2)
            second = migrate.run(batch=2)
        return first, second, calls

    def test_run_is_idempotent(self):
        project = self.root / 'project'
        project.mkdir()
        items = [legacy_item('mem_1', str(project)), legacy_item('mem_2', str(project)),
                 legacy_item('mem_3', str(project))]
        first, second, calls = self._run(items)
        self.assertEqual(first['imported'], 3)
        self.assertEqual(first['pending_after'], 0)
        self.assertEqual(second['imported'], 0)
        self.assertEqual(len(calls['batch']), 2)
        self.assertEqual(len(calls['tag']), 3)
        with backend.journal() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM records WHERE origin='legacy-migration'")
                             .fetchone()[0], 3)

    def test_run_falls_back_to_single_writes_when_batch_fails(self):
        project = self.root / 'project'
        project.mkdir()
        items = [legacy_item('mem_1', str(project)), legacy_item('mem_2', str(project))]

        def failing_batch(path, method='GET', data=None, timeout=60):
            if 'batch-write' in path:
                raise ValueError('HTTP 400: batch rejected')
            return {}

        with patch.object(migrate, 'legacy_items', return_value=items), \
             patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(backend, 'request', side_effect=failing_batch), \
             patch.object(backend, 'write', return_value={}) as single, \
             patch.object(migrate.memory_manager, 'project_info',
                          return_value={'tag': 't', 'projectPath': '/p', 'projectName': 'p'}):
            report = migrate.run(batch=2)
        self.assertEqual(report['imported'], 2)
        self.assertEqual(single.call_count, 2)
        self.assertEqual(report['pending_after'], 0)

    def test_run_resumes_after_partial_limit(self):
        project = self.root / 'project'
        project.mkdir()
        items = [legacy_item('mem_1', str(project)), legacy_item('mem_2', str(project)),
                 legacy_item('mem_3', str(project))]
        with patch.object(migrate, 'legacy_items', return_value=items), \
             patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(backend, 'request', return_value={}), \
             patch.object(migrate.memory_manager, 'project_info',
                          return_value={'tag': 't', 'projectPath': '/p', 'projectName': 'p'}):
            partial = migrate.run(limit=1, batch=1)
            rest = migrate.run(batch=3)
        self.assertEqual(partial['imported'], 1)
        self.assertEqual(rest['imported'], 2)

    def test_failed_record_is_reported_not_fatal(self):
        items = [legacy_item('mem_1'), legacy_item('mem_bad')]

        def failing_batch(path, method='GET', data=None, timeout=60):
            if 'batch-write' in path:
                raise ValueError('batch rejected')
            return {}

        def flaky_write(uri, content, **kwargs):
            if 'bad' in content or 'bad' in uri:
                raise ValueError('HTTP 500')
            return {}

        with patch.object(migrate, 'legacy_items', return_value=items), \
             patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(backend, 'request', side_effect=failing_batch), \
             patch.object(backend, 'write', side_effect=flaky_write), \
             patch.object(migrate.memory_manager, 'project_info',
                          return_value={'tag': 't', 'projectPath': '/p', 'projectName': 'p'}):
            report = migrate.run(batch=2)
        self.assertEqual(report['imported'], 1)
        self.assertEqual(len(report['failed']), 1)
        self.assertIn('mem_bad', json.dumps(report['failed']))


class VerifyTests(MigrationTestCase):
    def _mapped(self):
        backend._journal_upsert({'id': 'mem_1', 'uri': 'viking://user/default/memories/experiences/a.md',
                                 'origin': 'legacy-migration', 'project_tag': 'opencode_project_abc',
                                 'content_hash': 'x'})

    def test_verify_ok_for_matching_content(self):
        self._mapped()
        with patch.object(migrate, 'legacy_items', return_value=[legacy_item('mem_1')]), \
             patch.object(backend, 'read', return_value=('## Request\n\nKeep the SQLite journal.', {})):
            report = migrate.verify(sample=1)
        self.assertTrue(report['ok'])
        self.assertEqual(report['checked'], 1)
        self.assertEqual(report['mismatched'], [])

    def test_verify_flags_missing_and_mismatched(self):
        self._mapped()
        with patch.object(migrate, 'legacy_items', return_value=[legacy_item('mem_1'), legacy_item('mem_2')]), \
             patch.object(backend, 'read', return_value=('different body', {})):
            report = migrate.verify(sample=1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['unmigrated'], 1)
        self.assertEqual(len(report['mismatched']), 1)


if __name__ == '__main__':
    unittest.main()


class CutoverTests(MigrationTestCase):
    def test_apply_flips_backend_preserving_other_fields(self):
        configured = json.loads(self.config.read_text())
        configured['memory'].update({'enabled': True, 'rolling': {'enabled': True, 'max_storage_mb': 1024}})
        configured['unrelated'] = {'keep': True}
        self.config.write_text(json.dumps(configured))
        with patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(migrate, 'reconcile_deletions', return_value={'count': 0}), \
             patch.object(migrate, 'run', return_value={'imported': 0}), \
             patch.object(migrate, 'verify', return_value={'ok': True}):
            result = migrate.cutover(apply=True)
        self.assertTrue(result['changed'])
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved['memory']['backend'], 'openviking')
        self.assertEqual(saved['memory']['rolling']['max_storage_mb'], 1024)
        self.assertEqual(saved['unrelated'], {'keep': True})
        self.assertTrue(saved['restart_required'])

    def test_rollback_flips_back_without_delta(self):
        configured = json.loads(self.config.read_text())
        configured['memory']['backend'] = 'openviking'
        self.config.write_text(json.dumps(configured))
        with patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(migrate, 'run', side_effect=AssertionError('rollback must not import')):
            result = migrate.cutover(rollback=True)
        self.assertEqual(json.loads(self.config.read_text())['memory']['backend'], 'opencode-mem')
        self.assertTrue(result['changed'])

    def test_apply_refuses_when_service_not_ready(self):
        with patch.object(migrate.openviking_service, 'probe', return_value={'ready': False}):
            with self.assertRaises(ValueError):
                migrate.cutover(apply=True)
        self.assertEqual(json.loads(self.config.read_text())['memory'].get('backend'), 'opencode-mem')

    def test_apply_refuses_when_verification_fails(self):
        with patch.object(migrate.openviking_service, 'probe', return_value={'ready': True}), \
             patch.object(migrate, 'reconcile_deletions', return_value={'count': 0}), \
             patch.object(migrate, 'run', return_value={'imported': 0}), \
             patch.object(migrate, 'verify', return_value={'ok': False}):
            with self.assertRaises(ValueError):
                migrate.cutover(apply=True)
        self.assertEqual(json.loads(self.config.read_text())['memory'].get('backend'), 'opencode-mem')
