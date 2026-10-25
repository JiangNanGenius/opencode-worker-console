"""Codex metadata adapter: no user content, network, sensors or live databases."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import host_telemetry as ht


class CodexTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'sessions').mkdir()
        (self.root / 'tasks').mkdir()
        self.now = 200000
        self.meta = sqlite3.connect(self.root / 'state_5.sqlite')
        self.meta.executescript('''CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,title TEXT,cwd TEXT,
            rollout_path TEXT,updated_at INTEGER,archived INTEGER,model TEXT,reasoning_effort TEXT,source TEXT,thread_source TEXT);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);''')
        self.history = sqlite3.connect(self.root / 'thread_history_1.sqlite')
        self.history.execute('CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER,rollout_ordinal INTEGER,turn_id TEXT,rollout_end_byte_offset INTEGER)')

    def tearDown(self):
        self.meta.close()
        self.history.close()
        self.tmp.cleanup()

    def thread(self, id='parent', status='inProgress', updated=None, complete=None):
        updated = self.now if updated is None else updated
        path = self.root / 'sessions' / (id + '.jsonl')
        path.write_text(json.dumps({'type':'event_msg','timestamp':self.now-10,
            'payload':{'type':'task_complete','turn_id':'old-turn'}})+'\n')
        self.meta.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (id, 'Build task', 'private first message', '/private/project', str(path), updated, 0, 'model', 'high', 'exec', 'user'))
        self.history.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)', (id,status,self.now-20,complete,1,id+'-turn',None))
        self.meta.commit(); self.history.commit()

    def test_old_rollout_completion_cannot_finish_latest_turn(self):
        self.thread()
        item = ht.CodexTasks(self.root).collect(self.now)['items'][0]
        self.assertEqual(item['status'], 'running')
        self.assertEqual(item['turn_status'], 'inProgress')
        self.assertNotIn('rollout_path', item)
        self.assertNotIn('/private', json.dumps(item))
        self.assertNotIn('private first message', json.dumps(item))
        self.assertNotIn('progress', item)

    def append_event(self, id, kind, at, **payload):
        with (self.root/'sessions'/(id+'.jsonl')).open('a') as handle:
            handle.write(json.dumps({'type':kind,'timestamp':at,'payload':payload})+'\n')

    def test_new_rollout_turn_overrides_stale_terminal_projection_and_model(self):
        self.thread('parent','interrupted',complete=self.now-15)
        self.append_event('parent','event_msg',self.now-8,type='task_started',turn_id='new-turn')
        self.append_event('parent','turn_context',self.now-7,turn_id='new-turn',model='current-model',effort='low')
        # Lifecycle lies beyond the former 256 KiB bootstrap tail.
        self.append_event('parent','response_item',self.now-1,text='x'*400000)
        adapter = ht.CodexTasks(self.root)
        item = adapter.collect(self.now)['items'][0]
        self.assertEqual(item['status'],'running')
        self.assertEqual(item['model'],'current-model')
        self.assertEqual(item['effort'],'low')
        self.assertEqual(item['started_at'],self.now-8)
        self.append_event('parent','event_msg',self.now+2,type='task_complete',turn_id='new-turn')
        item = adapter.collect(self.now+3)['items'][0]
        self.assertEqual(item['status'],'completed')
        self.assertEqual(item['ended_at'],self.now+2)

    def test_new_start_without_context_does_not_reuse_previous_model(self):
        self.thread('parent','interrupted',complete=self.now-15)
        self.append_event('parent','event_msg',self.now-8,type='task_started',turn_id='new-turn')
        item=ht.CodexTasks(self.root).collect(self.now)['items'][0]
        self.assertEqual(item['status'],'running')
        self.assertIsNone(item['model'])
        self.assertIsNone(item['effort'])

    def test_old_matching_start_does_not_reopen_completed_db_turn(self):
        self.thread('parent','completed',complete=self.now-5)
        self.append_event('parent','event_msg',self.now-20,type='task_started',turn_id='parent-turn')
        item=ht.CodexTasks(self.root).collect(self.now)['items'][0]
        self.assertEqual(item['status'],'completed')

    def test_unattached_subagents_do_not_become_main_tasks(self):
        self.thread('guardian','completed',complete=self.now-2)
        self.meta.execute('UPDATE threads SET source=?,thread_source=? WHERE id=?',
            (json.dumps({'subagent':{'other':'guardian'}}),'subagent','guardian'))
        self.meta.commit()
        self.assertEqual(ht.CodexTasks(self.root).collect(self.now)['items'],[])

    def test_projection_offset_after_compaction_cannot_assert_old_terminal_state(self):
        self.thread('parent','interrupted',complete=self.now-5)
        self.meta.execute('UPDATE threads SET updated_at=?',(self.now,))
        self.meta.commit()
        self.history.execute('UPDATE thread_turns SET rollout_end_byte_offset=999999999')
        self.history.commit()
        item=ht.CodexTasks(self.root).collect(self.now)['items'][0]
        self.assertEqual(item['status'],'unknown')

    def test_owner_lookup_outside_window_and_subagent_edge(self):
        self.thread('parent', 'completed', self.now-90000, self.now-90000)
        self.thread('child')
        self.meta.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('parent','child','running'))
        self.meta.commit()
        items = ht.CodexTasks(self.root).collect(self.now, owner_ids=['parent'])['items']
        rows = {item['id']:item for item in items}
        self.assertEqual(rows['parent']['status'], 'completed')
        self.assertEqual(rows['child']['parent_thread_id'], 'parent')

    def test_finished_child_is_retained_under_active_parent_without_new_board_exposure(self):
        self.thread('parent')
        self.thread('child', 'completed', self.now-90000, self.now-90000)
        self.meta.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)', ('parent','child','completed'))
        self.meta.commit()
        items = ht.CodexTasks(self.root).collect(self.now)['items']
        rows = {item['id']:item for item in items}
        self.assertEqual(rows['child']['status'], 'completed')
        self.assertEqual(rows['child']['parent_thread_id'], 'parent')
        self.assertFalse(rows['child']['_board_visible'])

    def test_exact_owner_links_and_missing_owner_are_web_only(self):
        self.thread()
        for id, owner in [('job-1','parent'), ('job-2','missing')]:
            common.write_json(self.root/'tasks'/(id+'.json'), {'id':id,'owner_thread_id':owner,
                'group_id':'unrelated','title':'Worker','status':'running','updated_at':self.now,
                'objective':'secret content','source_dir':'/private/project'})
        common.write_json(self.root/'tasks'/'unowned.json', {'id':'unowned','group_id':'parent',
            'title':'Unowned','status':'running','updated_at':self.now})
        collector = ht.Collector(state_dir=self.root, codex_root=self.root)
        with patch.object(ht,'STATE',self.root), patch.object(ht,'cached_worker_quotas',side_effect=lambda value:value):
            tasks = collector.collect_tasks_once(self.now)
            self.assertEqual([w['id'] for w in tasks['codex']['items'][0]['workers']], ['job-1'])
            self.assertEqual(tasks['codex']['missing_owners'][0]['status'], 'unknown')
            self.assertNotIn('secret content',json.dumps(tasks))
            self.assertNotIn('/private',json.dumps(tasks))
            board = collector.board_payload()['tasks']['codex']
            self.assertNotIn('missing_owners',board)
            self.assertNotIn('workers',board['items'][0])
            self.assertNotIn('parent_thread_id',board['items'][0])
            self.assertNotIn('turn_status',board['items'][0])

    def test_failed_codex_source_preserves_other_source_and_ages(self):
        collector = ht.Collector(state_dir=self.root, codex_root=self.root)
        collector.latest_tasks['codex'] = {'state':'ok','sampled_at':self.now-100,'items':[], 'active':0}
        with patch.object(ht,'STATE',self.root), patch.object(collector.codex,'collect',side_effect=sqlite3.OperationalError('private details')), patch.object(ht,'cached_worker_quotas',side_effect=lambda value:value):
            tasks = collector.collect_tasks_once(self.now)
            self.assertEqual(tasks['opencode']['state'],'ok')
            self.assertEqual(tasks['codex']['sampled_at'],self.now-100)
            self.assertEqual(tasks['codex']['error'],'collection_failed')
            self.assertTrue(collector.snapshot(self.now)['tasks']['codex']['stale'])
            self.assertNotIn('private details',json.dumps(tasks))

    def test_missing_stores_and_tail_path_outside_sessions(self):
        adapter = ht.CodexTasks(self.root/'missing')
        self.assertEqual(adapter.collect(self.now)['state'],'unavailable')
        self.assertEqual(adapter.activity(self.root/'state_5.sqlite',self.now)['at'],0)

    # --- live failure regressions (2026-10-21: collection_failed from
    # oversized JSONL records and malformed non-dict records) ---------------

    def test_align_skip_lands_just_after_newline(self):
        import io
        # Newline near the start of a 64 KiB chunk.
        handle = io.BytesIO(b'x' * 100 + b'\n' + b'{"after":1}\n')
        handle.seek(50)
        ht._align_skip(handle, 65536)
        self.assertEqual(handle.tell(), 101)
        self.assertEqual(handle.read(10), b'{"after":1')
        # Newline as the first byte of the chunk: advance exactly one.
        handle = io.BytesIO(b'q\nrest')
        handle.seek(0)
        ht._align_skip(handle, 65536)
        self.assertEqual(handle.tell(), 2)
        # Newline as the last byte of the chunk: cursor already follows it.
        handle = io.BytesIO(b'y' * 65535 + b'\n' + b'z')
        handle.seek(0)
        ht._align_skip(handle, 65536)
        self.assertEqual(handle.tell(), 65536)
        # Newline near the end of the second chunk: no region is rescanned
        # and no bytes beyond the newline are skipped.
        handle = io.BytesIO(b'w' * 96851 + b'\n' + b'{"keep":1}')
        handle.seek(0)
        ht._align_skip(handle, 65536 * 2)
        self.assertEqual(handle.tell(), 96852)
        self.assertEqual(handle.read(10), b'{"keep":1}')
        # Oversized record: the scan stops at the budget, never past it.
        handle = io.BytesIO(b'z' * 4096)
        handle.seek(0)
        ht._align_skip(handle, 1024)
        self.assertEqual(handle.tell(), 1024)

    def test_bootstrap_boundary_giant_record_preserves_next_events(self):
        # The bootstrap tail cuts a giant record ~95 KiB before its newline;
        # alignment must resume at the record boundary so the task_started
        # and turn_context right after it are never skipped.
        self.thread('parent', 'inProgress')
        events = (json.dumps({'type':'event_msg','timestamp':self.now-5,
                              'payload':{'type':'task_started','turn_id':'big-turn'}})+'\n'
                  + json.dumps({'type':'turn_context','timestamp':self.now-4,
                                'payload':{'turn_id':'big-turn','model':'ctx2','effort':'high'}})+'\n').encode()
        pad_line = (json.dumps({'type':'response_item','timestamp':self.now-3,
                                'payload':{'type':'noise'}})+'\n').encode()
        padding = (pad_line*(2_000_000//len(pad_line)+1))[:2_000_000]
        path = self.root/'sessions'/'parent.jsonl'
        path.write_bytes(b'{"type":"response_item","payload":{"blob":"' + b'g'*(2_999_956) + b'"}\n'
                         + events + padding)
        item = ht.CodexTasks(self.root).collect(self.now)['items'][0]
        self.assertEqual(item['status'], 'running')
        self.assertEqual(item['started_at'], self.now-5)
        self.assertEqual(item['model'], 'ctx2')
        self.assertEqual(item['effort'], 'high')

    def test_huge_line_crossing_backfill_end_is_bounded_and_recovers(self):
        # Live repro: a single >1 MiB record straddles both the 2 MiB bootstrap
        # tail boundary and the 1 MiB backfill window end. The unbounded
        # readline() alignment let handle.tell() pass the window end and the
        # next read raised ValueError, killing every Codex row. The bounded
        # skip stops at the window edge and each sample still advances.
        self.thread('parent', 'inProgress')
        head = (json.dumps({'type':'event_msg','timestamp':self.now-15,
                            'payload':{'type':'task_started','turn_id':'ev-turn'}})+'\n'
                + json.dumps({'type':'turn_context','timestamp':self.now-14,
                              'payload':{'turn_id':'ev-turn','model':'ctx-model','effort':'minimal'}})+'\n').encode()
        base = json.dumps({'type':'response_item','timestamp':self.now-13,
                           'payload':{'type':'raw','text':''}}).encode()
        giant = json.dumps({'type':'response_item','timestamp':self.now-13,
                            'payload':{'type':'raw','text':'x'*(3_500_000-len(base))}}).encode()+b'\n'
        pad_line = (json.dumps({'type':'response_item','timestamp':self.now-12,
                                'payload':{'type':'noise'}})+'\n').encode()
        padding = (pad_line*(2_000_000//len(pad_line)+1))[:2_000_000]
        (self.root/'sessions'/'parent.jsonl').write_bytes(head+giant+padding)
        size = len(head)+len(giant)+len(padding)
        before = size-2097152
        giant_end = len(head)+len(giant)
        self.assertGreater(before, len(head)+1048576+10)   # window end inside giant record
        self.assertLess(before, giant_end-10)              # both cut points...
        self.assertGreater(before-1048576, len(head))      # ...are mid-record
        self.assertGreater(giant_end-(before-1048576), 1048576)  # newline is beyond the skip budget
        adapter = ht.CodexTasks(self.root)
        item = adapter.collect(self.now)['items'][0]
        self.assertEqual(item['status'], 'running')        # DB projection still serves the board
        self.assertEqual(item['model'], 'model')
        for i in range(1, 6):                              # bounded backfill finds the turn start
            item = adapter.collect(self.now+i)['items'][0]
        self.assertEqual(item['started_at'], self.now-15)
        self.assertEqual(item['model'], 'ctx-model')
        self.assertEqual(item['effort'], 'minimal')
        self.assertEqual(item['status'], 'running')

    def test_oversized_record_and_garbage_scalars_alongside_valid_thread(self):
        # A no-newline record larger than both alignment budgets must stay
        # bounded, and valid JSON scalars/lists/strs (including a truncated
        # record head) must be ignored before any event.get() call.
        self.thread('good')
        self.thread('junk')
        self.thread('huge')
        (self.root/'sessions'/'junk.jsonl').write_bytes(
            b'123\n"task_started"\n[{"type":"event_msg"}]\nnull\n{"type":"event_msg","timestamp":')
        (self.root/'sessions'/'huge.jsonl').write_bytes(
            b'{"type":"turn_context","payload":{"turn_id":"t","model":"never","effort":"never":'
            + b'z'*3_000_000)
        adapter = ht.CodexTasks(self.root)
        for i in range(3):
            out = adapter.collect(self.now+i)
        rows = {item['id']: item for item in out['items']}
        self.assertEqual(out['state'], 'ok')
        self.assertEqual(rows['good']['status'], 'running')
        self.assertEqual(rows['junk']['status'], 'running')
        self.assertEqual(rows['junk']['model'], 'model')   # DB value, garbage produced no events
        self.assertEqual(rows['huge']['status'], 'running')
        self.assertEqual(rows['huge']['model'], 'model')   # truncated head never parsed
        self.assertNotIn('never', json.dumps(out))

    def test_activity_failure_degrades_only_that_thread(self):
        self.thread('bad')
        self.thread('good')
        real = ht.CodexTasks.activity
        def flaky(self_a, path, now):
            if 'bad' in str(path):
                raise RuntimeError('private crash details')
            return real(self_a, path, now)
        with patch.object(ht.CodexTasks, 'activity', flaky):
            out = ht.CodexTasks(self.root).collect(self.now)
        rows = {item['id']: item for item in out['items']}
        self.assertEqual(out['state'], 'ok')
        self.assertEqual(sorted(rows), ['bad', 'good'])
        self.assertEqual(rows['bad']['status'], 'running')  # keeps the DB projection
        self.assertEqual(rows['bad']['model'], 'model')
        self.assertNotIn('private crash', json.dumps(out))

    def test_missing_thread_turns_table_keeps_event_derived_items(self):
        self.thread('parent')
        self.history.execute('DROP TABLE thread_turns')
        self.history.commit()
        self.append_event('parent', 'event_msg', self.now-3, type='task_started', turn_id='live-turn')
        out = ht.CodexTasks(self.root).collect(self.now)
        rows = {item['id']: item for item in out['items']}
        self.assertEqual(out['state'], 'ok')
        self.assertEqual(rows['parent']['status'], 'running')
        self.assertEqual(rows['parent']['started_at'], self.now-3)

    def test_absent_threads_table_reports_unavailable_without_raising(self):
        self.thread()
        self.meta.execute('DROP TABLE threads')
        self.meta.commit()
        self.assertEqual(ht.CodexTasks(self.root).collect(self.now)['state'], 'unavailable')

    def test_threads_without_optional_columns_still_collects(self):
        self.thread()
        for column in ('model', 'reasoning_effort', 'source', 'thread_source'):
            self.meta.execute('ALTER TABLE threads DROP COLUMN '+column)
        self.meta.commit()
        out = ht.CodexTasks(self.root).collect(self.now)
        self.assertEqual(out['state'], 'ok')
        item = out['items'][0]
        self.assertIsNone(item['model'])
        self.assertIsNone(item['effort'])
        self.assertFalse(item['is_subagent'])
        self.assertEqual(item['status'], 'running')
