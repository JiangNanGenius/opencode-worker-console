import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import management
import usage_ledger


def provider_body():
    return {
        'all': [
            {'id': 'deepseek', 'name': 'DeepSeek', 'source': 'api',
             'env': ['DEEPSEEK_API_KEY'], 'options': {'apiKey': 'sk-secret-provider', 'baseURL': 'https://evil.example'},
             'models': {'deepseek-flash': {'id': 'deepseek-flash', 'name': 'Flash', 'limit': {'context': 200000, 'output': 8192},
                                           'variants': {'high': {'temperature': 0.1, 'apiKey': 'sk-secret-variant'},
                                                        'low': {'temperature': 0.9}}}}},
            {'id': 'kimi-for-coding', 'name': 'Kimi', 'env': [], 'options': {'key': 'sk-secret-kimi'},
             'models': {'k3': {'id': 'k3', 'name': 'K3', 'limit': {}, 'variants': []}}},
        ],
        'connected': ['deepseek'],
        'default': {'deepseek': 'deepseek-flash'},
    }


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.write_config(self.base_config())
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(common, 'CONFIG', self.config)]
        for p in self.patchers:
            p.start()
        common.init()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def base_config(self):
        return {'version': 1, 'server_url': 'http://127.0.0.1:1234', 'opencode_binary': '/opt/opencode',
                'console_url': 'http://127.0.0.1:1235', 'max_parallel_per_owner': 4,
                'max_steps': 80, 'kimi_reserve_percent': 20,
                'profiles': {'fallback': {'model': 'deepseek/deepseek-flash', 'label': 'Flash', 'variant': 'high'},
                             'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'label': 'Kimi', 'variant': 'high'}}}

    def write_config(self, value):
        self.config.write_text(json.dumps(value, indent=2))

    def read_config(self):
        return json.loads(self.config.read_text())

    def valid_body(self):
        return {'profiles': {'fallback': {'model': 'deepseek/deepseek-flash', 'label': 'Flash', 'variant': 'high'},
                             'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'label': 'Kimi', 'enabled': True}},
                'max_parallel_per_owner': 6, 'max_steps': 100, 'kimi_reserve_percent': 25,
                'routing': {'fast': 'fallback', 'background': 'senior-code', 'deep': 'senior-code'}}

    def add_task(self, task_id, status, session_id=None, title='Pool task'):
        t = {'id': task_id, 'title': title, 'status': status, 'created_at': time.time()}
        if session_id:
            t['session_id'] = session_id
        common.write_json(common.task_path(task_id), t)
        return t

    @staticmethod
    def dispatcher(routes):
        def api(path, directory=None, method='GET', data=None, timeout=15):
            key = (method, path)
            if key not in routes:
                raise AssertionError('unexpected api call ' + str(key))
            value = routes[key]
            return value(directory, data) if callable(value) else value
        return api

    def test_auto_approve_boolean_validation(self):
        body = self.valid_body()
        for value in (True, False):
            body['auto_approve'] = value
            self.assertIs(management._validate_settings(body)['auto_approve'], value)
        for value in ('false', 0, None):
            body['auto_approve'] = value
            with self.assertRaises(ValueError):
                management._validate_settings(body)

    def test_task_management_deletes_terminal_records_and_retains_work_products(self):
        self.add_task('job-complete', 'completed', 'ses_complete')
        common.update('job-complete', objective='private prompt body', tier='normal', profile='senior-code',
                      actual_models=['kimi-for-coding/k3'],
                      usage={'input': 10, 'output': 3, 'reasoning': 2, 'cache_read': 20,
                             'cache_write': 0, 'total': 35, 'cost': 0.25,
                             'source': 'saved', 'complete': True})
        self.add_task('job-failed', 'failed', 'ses_failed')
        artifact = common.artifact_dir('job-complete') / 'report.json'
        artifact.write_text('{}')
        worktree = self.state / 'worktrees' / 'job-complete'
        worktree.mkdir(parents=True)
        (worktree / 'marker').write_text('retained')

        result = management.delete_tasks({'action': 'delete', 'ids': ['job-complete', 'job-failed']})

        self.assertEqual(result, {'deleted': 2,
                                  'retained': ['usage_ledger', 'sessions', 'artifacts', 'worktrees'],
                                  'usage_retention_days': 365})
        self.assertFalse(common.task_path('job-complete').exists())
        self.assertFalse(common.task_path('job-failed').exists())
        self.assertTrue(artifact.exists())
        self.assertTrue((worktree / 'marker').exists())
        ledger = usage_ledger.summary(include_entries=True)
        self.assertEqual(ledger['count'], 2)
        self.assertEqual(ledger['total_tokens'], 35)
        self.assertEqual(ledger['known_token_count'], 1)
        entry = next(item for item in ledger['entries'] if item['task_id'] == 'job-complete')
        self.assertEqual(entry['actual_models'], ['kimi-for-coding/k3'])
        self.assertEqual(entry['usage']['total'], 35)
        self.assertAlmostEqual(entry['expires_at'] - entry['deleted_at'], 365 * 86400)
        self.assertNotIn('private prompt body', json.dumps(entry))
        self.assertNotIn('entries', usage_ledger.summary())

    def test_task_management_rejects_active_selection_atomically(self):
        self.add_task('job-done', 'completed')
        self.add_task('job-running', 'running')

        with self.assertRaisesRegex(ValueError, 'terminal'):
            management.delete_tasks({'action': 'delete', 'ids': ['job-done', 'job-running']})

        self.assertTrue(common.task_path('job-done').exists())
        self.assertTrue(common.task_path('job-running').exists())

    def test_task_management_clear_completed_only(self):
        self.add_task('job-done-a', 'completed')
        self.add_task('job-done-b', 'completed')
        self.add_task('job-attention', 'needs_attention')
        self.add_task('job-running', 'running')

        result = management.delete_tasks({'action': 'clear_completed'})

        self.assertEqual(result['deleted'], 2)
        self.assertEqual({item['id'] for item in common.tasks()}, {'job-attention', 'job-running'})

    def test_task_management_clear_finished_includes_needs_attention(self):
        self.add_task('job-done', 'completed')
        self.add_task('job-attention', 'needs_attention')
        self.add_task('job-failed', 'failed')
        self.add_task('job-running', 'running')

        result = management.delete_tasks({'action': 'clear_finished'})

        self.assertEqual(result['deleted'], 2)
        self.assertEqual({item['id'] for item in common.tasks()}, {'job-failed', 'job-running'})

    def test_task_management_validates_request_before_deleting(self):
        self.add_task('job-done', 'completed')
        bad_requests = [None, {}, {'action': 'unknown'}, {'action': 'delete', 'ids': []},
                        {'action': 'delete', 'ids': ['job-done', 'job-done']},
                        {'action': 'delete', 'ids': ['job-missing']}]
        for body in bad_requests:
            with self.subTest(body=body), self.assertRaises(ValueError):
                management.delete_tasks(body)
            self.assertTrue(common.task_path('job-done').exists())

    def test_delete_requires_confirmation_and_preserves_task_evidence(self):
        sid = 'ses_delete'
        self.add_task('job-delete', 'completed', sid)
        calls = []
        def api(path, directory=None, method='GET', data=None, **kwargs):
            calls.append((method, path))
            if method == 'DELETE': return True
            if path.endswith('/children'): return []
            if path == '/session/status': return {}
            if path.startswith('/experimental/session'): return []
            return {'id': sid, 'title': 'Disposable', 'directory': str(self.root)}
        with patch.object(common, 'api', side_effect=api):
            with self.assertRaises(ValueError):
                management.update_session(sid, {'action': 'delete'})
            self.assertFalse(any(m == 'DELETE' for m, p in calls))
            result = management.update_session(sid, {'action': 'delete', 'confirm_session_id': sid, 'confirm_title': 'Disposable'})
        self.assertTrue(result['deleted'])
        self.assertTrue(common.task('job-delete')['session_deleted'])
        self.assertEqual(common.task('job-delete')['status'], 'completed')

    def test_delete_refuses_busy_descendant(self):
        sid = 'ses_parent'
        def api(path, directory=None, method='GET', data=None, **kwargs):
            if method == 'DELETE': self.fail('Must not delete busy tree')
            if path == '/session/status': return {'ses_child': {'type': 'busy'}}
            if path == '/session/ses_parent/children': return [{'id': 'ses_child'}]
            if path.startswith('/experimental/session'): return []
            return {'id': sid, 'title': 'Parent', 'directory': str(self.root)}
        with patch.object(common, 'api', side_effect=api), self.assertRaises(ValueError):
            management.update_session(sid, {'action':'delete','confirm_session_id':sid,'confirm_title':'Parent'})

    def test_cross_project_running_status_prevents_config_apply(self):
        def api(path, directory=None, **kwargs):
            if path.startswith('/experimental/session'): return [{'directory': '/other/project'}]
            return {'ses_other': {'type': 'busy'}} if directory else {}
        with patch.object(common, 'api', side_effect=api), self.assertRaises(ValueError):
            management.save_settings(self.valid_body())

    def test_bad_per_owner_limit_and_stale_revision_never_write(self):
        before = self.config.read_bytes()
        for value in [0, -1, True, '2', 17]:
            body = self.valid_body(); body['max_parallel_per_owner'] = value
            with self.assertRaises(ValueError): management.save_settings(body)
        body = self.valid_body(); body['revision'] = 99
        with self.assertRaises(ValueError): management.save_settings(body)
        self.assertEqual(before, self.config.read_bytes())

    def test_legacy_global_cap_fields_are_inert_and_never_exposed(self):
        config = self.base_config()
        config['max_parallel'] = 9
        config['max_kimi_parallel'] = 7
        config['provider_limits'] = {'deepseek': 1}
        del config['max_parallel_per_owner']
        self.write_config(config)
        result = management.settings()
        self.assertEqual(result['max_parallel_per_owner'], 4)
        for leaked in ('max_parallel', 'max_kimi_parallel', 'provider_limits'):
            self.assertNotIn(leaked, result)
        # A legacy global cap on disk must not be interpreted as the owner limit.
        body = self.valid_body(); body['max_parallel_per_owner'] = 2
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
        stored = self.read_config()
        self.assertEqual(stored['max_parallel_per_owner'], 2)
        # Legacy fields remain on disk but are inert, never the owner limit.
        self.assertEqual(stored['max_parallel'], 9)
        self.assertEqual(stored['max_kimi_parallel'], 7)
        self.assertEqual(stored['provider_limits'], {'deepseek': 1})

    def test_settings_defaults_per_owner_limit_when_missing_or_invalid(self):
        config = self.base_config()
        del config['max_parallel_per_owner']
        self.write_config(config)
        self.assertEqual(management.settings()['max_parallel_per_owner'], 4)
        config['max_parallel_per_owner'] = 'junk'
        self.write_config(config)
        self.assertEqual(management.settings()['max_parallel_per_owner'], 4)

    # -- catalog ---------------------------------------------------------
    def test_catalog_whitelists_provider_and_model_fields(self):
        with patch.object(common, 'api', return_value=provider_body()):
            result = management.catalog()
        providers = result['providers']
        self.assertEqual([p['id'] for p in providers], ['deepseek', 'kimi-for-coding'])
        self.assertEqual(set(providers[0].keys()), {'id', 'name', 'connected', 'models'})
        self.assertTrue(providers[0]['connected'])
        self.assertFalse(providers[1]['connected'])
        model = providers[0]['models'][0]
        self.assertEqual(set(model.keys()), {'id', 'name', 'limit', 'variants'})
        self.assertEqual(model['id'], 'deepseek-flash')
        self.assertEqual(model['name'], 'Flash')
        self.assertEqual(model['limit'], {'context': 200000})
        self.assertEqual(model['variants'], ['high', 'low'])

    def test_catalog_never_exposes_credentials_options_or_env(self):
        with patch.object(common, 'api', return_value=provider_body()):
            raw = json.dumps(management.catalog(), ensure_ascii=False)
        for secret in ('sk-secret-provider', 'sk-secret-variant', 'sk-secret-kimi', 'apiKey', 'api_key',
                       'baseURL', 'options', 'env', 'DEEPSEEK_API_KEY', 'source', 'default', 'authorization'):
            self.assertNotIn(secret, raw)

    def test_catalog_handles_list_response_and_missing_connected(self):
        body = [{'id': 'p1', 'name': 'P1', 'models': [{'id': 'm1', 'name': 'M1', 'variants': ['a', 'b']}]}]
        with patch.object(common, 'api', return_value=body):
            result = management.catalog()
        self.assertFalse(result['providers'][0]['connected'])
        self.assertIsNone(result['providers'][0]['models'][0]['limit']['context'])
        self.assertEqual(result['providers'][0]['models'][0]['variants'], ['a', 'b'])

    # -- settings --------------------------------------------------------
    def test_settings_returns_only_editable_fields(self):
        result = management.settings()
        self.assertEqual(set(result.keys()), {'profiles', 'max_parallel_per_owner',
                                              'kimi_reserve_percent', 'revision', 'cleanup', 'auto_approve',
                                              'kimi_monthly_reset', 'economics',
                                              'kimi_low_weekly_threshold_percent',
                                              'kimi_low_weekly_k3_limit',
                                              'fast_bias_runway_percent',
                                              'budget_signals',
                                              'auto_reroute_on_quota_exhaustion'})
        self.assertEqual(result['economics']['afp_cny_per_unit'], 0.002)
        self.assertEqual(result['economics']['kimi_plan_cny'], 699.0)
        self.assertEqual(result['max_parallel_per_owner'], 4)
        self.assertEqual(result['kimi_low_weekly_threshold_percent'], 5)
        self.assertEqual(result['kimi_low_weekly_k3_limit'], 1)
        self.assertEqual(result['fast_bias_runway_percent'], 75)
        self.assertEqual(result['budget_signals'], {})
        self.assertTrue(result['auto_reroute_on_quota_exhaustion'])
        self.assertEqual(result['profiles']['fallback'],
                         {'model': 'deepseek/deepseek-flash', 'label': 'Flash', 'variant': 'high', 'enabled': True})
        # Generic disabled defaults are exported when the schedule was never configured.
        self.assertEqual(result['kimi_monthly_reset'], {'enabled': False, 'day': 1, 'time': '12:00',
                                                        'timezone': 'Asia/Shanghai'})
        self.assertNotIn('max_steps', result)
        for leaked in ('server_url', 'console_url', 'opencode_binary'):
            self.assertNotIn(leaked, result)

    def test_budget_signals_support_manual_windows_monetary_thresholds_and_ignore(self):
        body = self.valid_body()
        body['budget_signals'] = {
            'kimi-for-coding': {'mode': 'manual_window', 'remaining_percent': 23.5,
                                'duration_minutes': 10080,
                                'resets_at': '2026-09-25T12:00:00+08:00'},
            'deepseek': {'mode': 'monetary', 'currency': 'CNY', 'low_balance': 10,
                         'manual_balance': 18.25},
        }
        with patch.object(common, 'api', return_value={}):
            saved = management.save_settings(body)
        self.assertEqual(saved['budget_signals'], body['budget_signals'])
        self.assertEqual(management.settings()['budget_signals'], body['budget_signals'])

        body = self.valid_body()
        body['budget_signals'] = {'deepseek': {'mode': 'ignore'}}
        with patch.object(common, 'api', return_value={}):
            saved = management.save_settings(body)
        self.assertEqual(saved['budget_signals'], {'deepseek': {'mode': 'ignore'}})

    def test_budget_signals_reject_unknown_provider_invalid_thresholds_and_ambiguous_time(self):
        cases = [
            {'missing': {'mode': 'ignore'}},
            {'deepseek': {'mode': 'manual_window', 'remaining_percent': 50,
                          'duration_minutes': 300, 'resets_at': '2026-09-21T12:00:00'}},
            {'deepseek': {'mode': 'manual_window', 'remaining_percent': 101,
                          'duration_minutes': 300, 'resets_at': '2026-09-21T12:00:00+08:00'}},
            {'deepseek': {'mode': 'monetary', 'currency': 'cny', 'low_balance': 1}},
            {'deepseek': {'mode': 'monetary', 'currency': 'CNY', 'low_balance': -1}},
        ]
        before = self.config.read_bytes()
        for signals in cases:
            with self.subTest(signals=signals):
                body = self.valid_body(); body['budget_signals'] = signals
                with self.assertRaises(ValueError):
                    management._validate_settings(body)
        self.assertEqual(self.config.read_bytes(), before)

    def test_legacy_max_steps_is_ignored_dropped_and_never_exported(self):
        # The fixture config still carries an old max_steps: 80.
        self.assertNotIn('max_steps', management.settings())
        for legacy in (80, 200, 0, 'junk', True, None):
            with self.subTest(max_steps=legacy):
                body = self.valid_body()
                body['max_steps'] = legacy
                with patch.object(common, 'api', return_value={}):
                    result = management.save_settings(body)
                self.assertNotIn('max_steps', result)
                stored = self.read_config()
                self.assertNotIn('max_steps', stored)
                self.assertEqual(stored['max_parallel_per_owner'], 6)

    def test_save_settings_accepts_body_without_max_steps(self):
        body = self.valid_body()
        del body['max_steps']
        with patch.object(common, 'api', return_value={}):
            result = management.save_settings(body)
        self.assertNotIn('max_steps', result)
        self.assertNotIn('max_steps', self.read_config())

    def test_monthly_reset_valid_values_persist_and_export_normally(self):
        schedule = {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
        body = self.valid_body()
        body['kimi_monthly_reset'] = schedule
        with patch.object(common, 'api', return_value={}):
            result = management.save_settings(body)
        self.assertEqual(result['kimi_monthly_reset'], schedule)
        self.assertEqual(self.read_config()['kimi_monthly_reset'], schedule)
        self.assertEqual(management.settings()['kimi_monthly_reset'], schedule)

    def test_monthly_reset_preserved_when_old_client_omits_it(self):
        schedule = {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
        config = self.base_config()
        config['kimi_monthly_reset'] = schedule
        self.write_config(config)
        self.assertNotIn('kimi_monthly_reset', self.valid_body())
        with patch.object(common, 'api', return_value={}):
            management.save_settings(self.valid_body())
        self.assertEqual(self.read_config()['kimi_monthly_reset'], schedule)
        self.assertEqual(management.settings()['kimi_monthly_reset'], schedule)

    def test_monthly_reset_invalid_values_rejected_without_writing(self):
        original = self.config.read_text()
        cases = [('not object', 'junk'),
                 ('missing enabled', {'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'}),
                 ('string enabled', {'enabled': 'true', 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'}),
                 ('day low', {'enabled': True, 'day': 0, 'time': '12:00', 'timezone': 'Asia/Shanghai'}),
                 ('day high', {'enabled': True, 'day': 32, 'time': '12:00', 'timezone': 'Asia/Shanghai'}),
                 ('bool day', {'enabled': True, 'day': True, 'time': '12:00', 'timezone': 'Asia/Shanghai'}),
                 ('bad time', {'enabled': True, 'day': 19, 'time': '24:00', 'timezone': 'Asia/Shanghai'}),
                 ('text time', {'enabled': True, 'day': 19, 'time': 'noon', 'timezone': 'Asia/Shanghai'}),
                 ('bad zone', {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Mars/Base'}),
                 ('empty zone', {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': ''})]
        for name, value in cases:
            with self.subTest(name=name):
                body = self.valid_body()
                body['kimi_monthly_reset'] = value
                with patch.object(common, 'api') as api:
                    with self.assertRaises(ValueError):
                        management.save_settings(body)
                    api.assert_not_called()
                self.assertEqual(self.config.read_text(), original)

    def test_monthly_reset_invalid_legacy_record_exports_disabled_defaults(self):
        config = self.base_config()
        config['kimi_monthly_reset'] = {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Mars/Base'}
        self.write_config(config)
        exported = management.settings()['kimi_monthly_reset']
        self.assertFalse(exported['enabled'])
        self.assertEqual(exported['day'], 19)
        self.assertEqual(exported['timezone'], 'Asia/Shanghai')

    def test_save_settings_preserves_other_config_and_sets_revision(self):
        with patch.object(common, 'api', return_value={}) as api:
            result = management.save_settings(self.valid_body())
        self.assertFalse(api.call_args.args[0].startswith('http'))
        self.assertEqual(api.call_args.args[0], '/session/status')
        stored = self.read_config()
        self.assertEqual(stored['server_url'], 'http://127.0.0.1:1234')
        self.assertEqual(stored['opencode_binary'], '/opt/opencode')
        self.assertEqual(stored['console_url'], 'http://127.0.0.1:1235')
        self.assertEqual(stored['max_parallel_per_owner'], 6)
        self.assertNotIn('max_steps', stored)
        self.assertEqual(stored['routing']['deep'], 'senior-code')
        self.assertEqual(stored['revision'], 1)
        self.assertTrue(stored['restart_required'])
        self.assertTrue(result['restart_required'])

    def test_save_settings_increments_existing_revision(self):
        config = self.base_config()
        config['revision'] = 7
        self.write_config(config)
        with patch.object(common, 'api', return_value={}):
            management.save_settings(self.valid_body())
        self.assertEqual(self.read_config()['revision'], 8)

    def test_save_settings_rejects_invalid_without_writing(self):
        original = self.config.read_text()
        cases = []
        bad = self.valid_body(); del bad['kimi_reserve_percent']; cases.append(('missing field', bad))
        bad = self.valid_body(); bad['profiles'] = {}; cases.append(('empty profiles', bad))
        bad = self.valid_body(); bad['profiles']['Bad'] = {'model': 'deepseek/x'}; cases.append(('uppercase id', bad))
        bad = self.valid_body(); bad['profiles']['../evil'] = {'model': 'deepseek/x'}; cases.append(('path id', bad))
        bad = self.valid_body(); bad['profiles']['ok'] = {'model': 'https://evil.example/model'}; cases.append(('url model', bad))
        bad = self.valid_body(); bad['profiles']['ok'] = {'model': 'no-slash'}; cases.append(('bare model', bad))
        bad = self.valid_body(); bad['max_parallel_per_owner'] = 0; cases.append(('per-owner low', bad))
        bad = self.valid_body(); bad['max_parallel_per_owner'] = 17; cases.append(('per-owner high', bad))
        bad = self.valid_body(); bad['max_parallel_per_owner'] = True; cases.append(('bool per-owner', bad))
        bad = self.valid_body(); del bad['max_parallel_per_owner']; cases.append(('per-owner missing', bad))
        bad = self.valid_body(); bad['kimi_reserve_percent'] = 101; cases.append(('reserve high', bad))
        bad = self.valid_body(); del bad['routing']['deep']; cases.append(('routing incomplete', bad))
        bad = self.valid_body(); bad['routing']['fast'] = 'ghost'; cases.append(('routing unknown', bad))
        bad = self.valid_body(); bad['profiles']['senior-code']['enabled'] = False; cases.append(('routing disabled', bad))
        for name, body in cases:
            with self.subTest(name=name):
                with patch.object(common, 'api') as api:
                    with self.assertRaises(ValueError):
                        management.save_settings(body)
                    api.assert_not_called()
                self.assertEqual(self.config.read_text(), original)

    def test_save_settings_ignores_legacy_cap_fields_in_body(self):
        body = self.valid_body()
        body['max_parallel'] = 9
        body['max_kimi_parallel'] = 9
        body['provider_limits'] = {'deepseek': 9}
        with patch.object(common, 'api', return_value={}):
            result = management.save_settings(body)
        for leaked in ('max_parallel', 'max_kimi_parallel', 'provider_limits'):
            self.assertNotIn(leaked, result)
        stored = self.read_config()
        self.assertEqual(stored['max_parallel_per_owner'], 6)
        for leaked in ('max_parallel', 'max_kimi_parallel', 'provider_limits'):
            self.assertNotIn(leaked, stored)

    def test_save_settings_rejects_non_object(self):
        with self.assertRaises(ValueError):
            management.save_settings('not-a-dict')

    def test_save_settings_refuses_while_pool_busy(self):
        for status in ('queued', 'running', 'starting', 'uncertain'):
            with self.subTest(status=status):
                for path in (self.state / 'tasks').glob('*.json'):
                    path.unlink()
                self.add_task('job-' + status, status)
                original = self.config.read_text()
                with patch.object(common, 'api', return_value={}):
                    with self.assertRaises(ValueError):
                        management.save_settings(self.valid_body())
                self.assertEqual(self.config.read_text(), original)

    def test_save_settings_refuses_while_sessions_busy(self):
        original = self.config.read_text()
        with patch.object(common, 'api', return_value={'ses_1': {'type': 'busy'}}):
            with self.assertRaises(ValueError):
                management.save_settings(self.valid_body())
        self.assertEqual(self.config.read_text(), original)

    def test_save_settings_fails_closed_when_status_unavailable(self):
        original = self.config.read_text()
        with patch.object(common, 'api', side_effect=common.HttpFailure(500)):
            with self.assertRaises(ValueError):
                management.save_settings(self.valid_body())
        self.assertEqual(self.config.read_text(), original)

    def test_save_settings_ignores_forbidden_keys(self):
        body = self.valid_body()
        body.update({'server_url': 'http://evil.example', 'opencode_binary': '/tmp/evil', 'opencode_key': 'sk-secret'})
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
        stored = self.read_config()
        self.assertEqual(stored['server_url'], 'http://127.0.0.1:1234')
        self.assertEqual(stored['opencode_binary'], '/opt/opencode')
        self.assertNotIn('opencode_key', stored)

    # -- sessions --------------------------------------------------------
    def raw_sessions(self):
        return [
            {'id': 'ses_1', 'title': 'Alpha task', 'directory': '/repo/a', 'parentID': None, 'projectID': 'p1',
             'time': {'created': 1, 'updated': 2}, 'extra': 'must-not-leak'},
            {'id': 'ses_2', 'title': 'Beta task', 'directory': '/repo/b', 'parentID': 'ses_1', 'projectID': 'p2',
             'time': {'created': 1, 'updated': 3, 'archived': 1700000000000},
             'parts': [{'type': 'text', 'text': 'secret prompt'}]},
        ]

    def test_sessions_maps_fields_filters_and_links_tasks(self):
        self.add_task('job-1', 'completed', session_id='ses_1')
        routes = {('GET', '/experimental/session?limit=500&archived=true'): self.raw_sessions(),
                  ('GET', '/session/status'): {'ses_1': {'type': 'busy'}}}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            result = management.sessions({})
        self.assertEqual(result['truncated'], False)
        self.assertEqual(result['projects'], ['/repo/a', '/repo/b'])
        first = result['sessions'][0]
        self.assertEqual(set(first.keys()), {'id', 'title', 'directory', 'parentID', 'projectID', 'time', 'status', 'task_id'})
        self.assertEqual(first['task_id'], 'job-1')
        self.assertEqual(first['status'], {'type': 'busy'})
        self.assertNotIn('extra', first)
        self.assertNotIn('parts', result['sessions'][1])

    def test_sessions_search_directory_and_archived_filters(self):
        routes = {('GET', '/experimental/session?limit=500&archived=true'): self.raw_sessions(),
                  ('GET', '/session/status'): {}}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            self.assertEqual([s['id'] for s in management.sessions({'search': 'beta'})['sessions']], ['ses_2'])
            self.assertEqual([s['id'] for s in management.sessions({'search': 'SES_1'})['sessions']], ['ses_1'])
            self.assertEqual([s['id'] for s in management.sessions({'directory': '/repo/a'})['sessions']], ['ses_1'])
            self.assertEqual([s['id'] for s in management.sessions({'directory': '/repo'})['sessions']], ['ses_1', 'ses_2'])
            self.assertEqual([s['id'] for s in management.sessions({'archived': True})['sessions']], ['ses_2'])
            self.assertEqual([s['id'] for s in management.sessions({'archived': 'true'})['sessions']], ['ses_2'])
            self.assertEqual([s['id'] for s in management.sessions({'archived': False})['sessions']], ['ses_1'])

    def test_sessions_truncated_at_limit(self):
        raw = [{'id': 'ses_' + str(i), 'directory': '/repo', 'time': {}} for i in range(management.SESSION_LIMIT)]
        routes = {('GET', '/experimental/session?limit=500&archived=true'): raw, ('GET', '/session/status'): {}}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            result = management.sessions({})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['sessions']), management.SESSION_LIMIT)

    # -- update_session --------------------------------------------------
    def idle_routes(self, session, patch_response, directory_seen):
        def patch_call(directory, data):
            directory_seen.append(directory)
            return patch_response
        return {('GET', '/session/ses_1'): session,
                ('GET', '/session/status'): {'ses_1': {'type': 'idle'}},
                ('PATCH', '/session/ses_1'): patch_call}

    def test_archive_and_restore_use_native_millisecond_timestamps(self):
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo', 'time': {'created': 1}}
        calls = []

        def patch_call(directory, data):
            calls.append((directory, data))
            return dict(session, time={'created': 1, 'archived': data['time']['archived']})

        routes = {('GET', '/session/ses_1'): session,
                  ('GET', '/session/status'): {'ses_1': {'type': 'idle'}},
                  ('PATCH', '/session/ses_1'): patch_call}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            before = int(time.time() * 1000)
            archived = management.update_session('ses_1', {'action': 'archive'})
            after = int(time.time() * 1000)
            restored = management.update_session('ses_1', {'action': 'restore'})
        self.assertEqual(calls[0][0], '/repo')
        self.assertTrue(before <= calls[0][1]['time']['archived'] <= after)
        self.assertEqual(archived['time']['archived'], calls[0][1]['time']['archived'])
        self.assertEqual(calls[1][1], {'time': {'archived': 0}})
        self.assertEqual(restored['time']['archived'], 0)

    def test_update_session_ignores_caller_supplied_directory(self):
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo/real', 'time': {}}
        seen = []
        routes = self.idle_routes(session, dict(session, time={'archived': 1}), seen)
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            management.update_session('ses_1', {'action': 'archive', 'directory': '/etc'})
        self.assertEqual(seen, ['/repo/real'])

    def test_update_session_refuses_active_pool_owned_session(self):
        self.add_task('job-1', 'running', session_id='ses_1')
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo', 'time': {}}
        routes = {('GET', '/session/ses_1'): session, ('GET', '/session/status'): {'ses_1': {'type': 'idle'}}}
        for action in ('archive', 'fork', 'abort'):
            with self.subTest(action=action):
                with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
                    with self.assertRaises(ValueError):
                        management.update_session('ses_1', {'action': action})

    def test_update_session_busy_guard_blocks_archive_and_fork_allows_abort(self):
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo', 'time': {}}
        abort_seen = []

        def abort_call(directory, data):
            abort_seen.append((directory, data))
            return dict(session)

        routes = {('GET', '/session/ses_1'): session,
                  ('GET', '/session/status'): {'ses_1': {'type': 'busy'}},
                  ('POST', '/session/ses_1/abort'): abort_call}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            for action in ('archive', 'fork'):
                with self.assertRaises(ValueError):
                    management.update_session('ses_1', {'action': action})
            result = management.update_session('ses_1', {'action': 'abort'})
        self.assertEqual(abort_seen, [('/repo', None)])
        self.assertEqual(result['id'], 'ses_1')

    def test_rename_syncs_pool_task_title_only_after_upstream_success(self):
        self.add_task('job-1', 'queued', session_id='ses_1', title='old title')
        session = {'id': 'ses_1', 'title': 'old title', 'directory': '/repo', 'time': {}}
        routes = self.idle_routes(session, dict(session, title='new title'), [])
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            result = management.update_session('ses_1', {'action': 'rename', 'title': 'new title'})
        self.assertEqual(result['title'], 'new title')
        self.assertEqual(common.task('job-1')['title'], 'new title')

    def test_rename_does_not_sync_when_upstream_fails(self):
        self.add_task('job-1', 'queued', session_id='ses_1', title='old title')
        session = {'id': 'ses_1', 'title': 'old title', 'directory': '/repo', 'time': {}}

        def failing(directory, data):
            raise common.HttpFailure(500)

        routes = {('GET', '/session/ses_1'): session,
                  ('GET', '/session/status'): {'ses_1': {'type': 'idle'}},
                  ('PATCH', '/session/ses_1'): failing}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            with self.assertRaises(common.HttpFailure):
                management.update_session('ses_1', {'action': 'rename', 'title': 'new title'})
        self.assertEqual(common.task('job-1')['title'], 'old title')

    def test_fork_sends_empty_body_and_returns_cleaned_child(self):
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo', 'time': {}}
        seen = []

        def fork_call(directory, data):
            seen.append((directory, data))
            return {'id': 'ses_2', 'title': 'T', 'directory': '/repo', 'parentID': 'ses_1',
                    'time': {}, 'prompt': 'must-not-be-sent'}

        routes = {('GET', '/session/ses_1'): session,
                  ('GET', '/session/status'): {'ses_1': {'type': 'idle'}},
                  ('POST', '/session/ses_1/fork'): fork_call}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            result = management.update_session('ses_1', {'action': 'fork'})
        self.assertEqual(seen, [('/repo', {})])
        self.assertEqual(result['id'], 'ses_2')
        self.assertEqual(result['parentID'], 'ses_1')
        self.assertNotIn('prompt', result)

    def test_update_session_rejects_bad_identifier_and_action(self):
        session = {'id': 'ses_1', 'title': 'T', 'directory': '/repo', 'time': {}}
        routes = {('GET', '/session/ses_1'): session, ('GET', '/session/ses_missing'): {},
                  ('GET', '/session/status'): {}}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            for sid in ('../etc', '', 'ses/1', 42):
                with self.assertRaises(ValueError):
                    management.update_session(sid, {'action': 'abort'})
            for action in ('delete', None, 'truncate'):
                with self.assertRaises(ValueError):
                    management.update_session('ses_1', {'action': action})
            with self.assertRaises(ValueError):
                management.update_session('ses_1', 'not-a-dict')
            with self.assertRaises(ValueError):
                management.update_session('ses_missing', {'action': 'abort'})

    # -- create_session --------------------------------------------------
    def test_create_session_posts_title_only_without_prompt(self):
        directory = self.root / 'project'
        directory.mkdir()
        seen = []

        def create_call(directory_param, data):
            seen.append((directory_param, data))
            return {'id': 'ses_new', 'title': data['title'], 'directory': directory_param, 'time': {}}

        routes = {('POST', '/session'): create_call, ('GET', '/session/status'): {}}
        with patch.object(common, 'api', side_effect=self.dispatcher(routes)):
            result = management.create_session({'directory': str(directory), 'title': '  New session  '})
        self.assertEqual(seen, [(str(directory), {'title': 'New session'})])
        self.assertEqual(result['id'], 'ses_new')
        self.assertEqual(result['title'], 'New session')

    def test_create_session_rejects_bad_directories_and_titles(self):
        directory = self.root / 'project'
        directory.mkdir()
        with patch.object(common, 'api') as api:
            for body in ({'directory': 'relative/path', 'title': 'x'},
                         {'directory': str(self.root / 'missing'), 'title': 'x'},
                         {'directory': str(directory), 'title': '   '},
                         {'directory': '', 'title': 'x'},
                         'not-a-dict'):
                with self.subTest(body=body):
                    with self.assertRaises(ValueError):
                        management.create_session(body)
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
