"""Dynamic Kimi weekly reserve release, demand evidence and native gate tracking.

Policy tests call the same pure helpers production uses: the release curve is
evaluated with an injected timestamp, the demand snapshot is written through the
real bounded store, and the loopback gate runs as a real HTTP server. No real
provider request, private state, credential or deployment is touched.
"""
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import kimi_reserve
import management
import quota
import routing

NOW = time.time()
IDENT = 'cred-kimi-dynamic'
WEEK_5H = {'name': 'window_0', 'valid': True, 'limit': 1, 'remaining': 0.9999,
           'remaining_percent': 99.99, 'duration_minutes': 300, 'resets_at': None}


def profiles(**overrides):
    values = {
        'fallback': {'model': 'deepseek/deepseek-flash', 'variant': 'max', 'label': 'Fallback'},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'variant': 'max', 'label': 'K2.8'},
        'deep-research': {'model': 'bailian-token-plan/qwen3.8-max', 'variant': 'max', 'label': 'Qwen Max'},
        'aliyun-flash': {'model': 'bailian-token-plan/qwen3.8-flash', 'variant': 'max', 'label': 'Qwen Flash'},
        'ultra-reasoning': {'model': 'kimi-for-coding/k3.1-pro', 'variant': 'max', 'label': 'K3.1 Pro'},
    }
    values.update(overrides)
    return values


def config(**overrides):
    value = {'profiles': profiles(), 'kimi_weekly_reserve_percent': 30,
             'kimi_reserve_percent': 0, 'kimi_low_weekly_threshold_percent': 5,
             'kimi_low_weekly_ultra_limit': 1}
    value.update(overrides)
    return value


def weekly_window(percent, resets_at, duration=10080, name='usages.limit_7d'):
    return {'name': name, 'valid': True, 'limit': 1, 'remaining': percent / 100.0,
            'remaining_percent': percent, 'duration_minutes': duration,
            'resets_at': resets_at}


def view(percent, resets_in_hours, now=None, duration=10080, sampled=None,
         account_record=None, stale=False, available=True, state='ok'):
    now = NOW if now is None else now
    window = weekly_window(percent, now + resets_in_hours * 3600, duration=duration)
    record = {'state': state, 'stale': stale, 'sampled_at': now if sampled is None else sampled,
              'available': available, 'windows': [dict(WEEK_5H), window]}
    if account_record is not None:
        record['account'] = account_record
    return {'kimi-for-coding': record}


def account(windows=(), weekly='applicable', binding='verified', sampled_at=None):
    return {'state': 'ok', 'source': 'kimi_code_account',
            'sampled_at': NOW if sampled_at is None else sampled_at,
            'windows': [dict(window) for window in windows],
            'weekly_applicability': weekly,
            'account_binding': {'state': binding, 'method': 'official_identity_match'},
            'available': True}


def month_window(percent):
    return {'name': 'monthTotal', 'kind': 'month_total', 'limit': 1,
            'remaining': percent / 100.0, 'remaining_percent': percent,
            'duration_minutes': None, 'resets_at': None, 'valid': True}


class StateFixture(unittest.TestCase):
    """Temporary private state/config with the shared identity bindings patched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps(config()))
        self.patchers = [
            patch.object(common, 'STATE', self.state),
            patch.object(quota, 'STATE', self.state),
            patch.object(common, 'CONFIG', self.config_path),
            patch.object(quota, 'credential_identity', lambda provider: IDENT),
            patch.object(kimi_reserve, 'current_identity', lambda: IDENT),
        ]
        for patcher in self.patchers:
            patcher.start()
        common.init()
        kimi_reserve.reset_local_guard()

    def tearDown(self):
        kimi_reserve.reset_local_guard()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def write_demand(self, **fields):
        record = {'version': 1, 'identity': IDENT, 'instance': 'test-pool',
                  'tasks_updated_at': NOW, 'queued_ultra': 0, 'active_ultra': 0,
                  'quiet_since': NOW - 1800, 'native_last_at': None}
        record.update(fields)
        (self.state / kimi_reserve.DEMAND_FILE).write_text(json.dumps(record))
        return record

    def reserve_state(self, c=None, percent=15.0, resets_in_hours=42, now=None, **view_kwargs):
        now = NOW if now is None else now
        q = view(percent, resets_in_hours, now=now, sampled=now, **view_kwargs)
        return quota.kimi_weekly_reserve_state(config(**(c or {})), q, now=now)


class PureCurveTests(unittest.TestCase):
    """The release curve is a pure function of base, window and demand evidence."""

    def state(self, base=30, dynamic=True, remaining_hours=42, demand=None, **extra):
        weekly = {'status': 'known', 'remaining_percent': 15.0, 'source': 'telemetry_limit_7d',
                  'resets_at': NOW + remaining_hours * 3600, 'as_of': NOW,
                  'window': {'name': 'usages.limit_7d', 'duration_minutes': 10080}}
        weekly.update(extra)
        return kimi_reserve.compute_reserve_state(base, dynamic, weekly, demand, NOW)

    def test_curve_boundaries(self):
        quiet = {'state': 'quiet', 'reason': 'quiet'}
        cases = {85: 30.0, 84: 30.0, 42: 17.5, 24: 12.1429, 1: 5.2976}
        for hours, expected in cases.items():
            with self.subTest(hours=hours):
                value = self.state(remaining_hours=hours, demand=quiet)
                self.assertEqual(value['effective_reserve_percent'], expected)
                self.assertEqual(value['released_percent'], round(30 - expected, 4))
                self.assertIn(value['release_state'], ('releasing', 'first_half'))
        near_expiry = self.state(remaining_hours=0.001, demand=quiet)
        self.assertAlmostEqual(near_expiry['effective_reserve_percent'], 5.0, delta=0.01)
        self.assertEqual(self.state(remaining_hours=85, demand=quiet)['release_state'],
                         'first_half')
        self.assertEqual(self.state(remaining_hours=42, demand=quiet)['release_state'],
                         'releasing')
        self.assertEqual(self.state(remaining_hours=42, demand=quiet)['remaining_hours'], 42.0)

    def test_emergency_floor_and_small_base(self):
        quiet = {'state': 'quiet', 'reason': 'quiet'}
        value = self.state(base=3, remaining_hours=1, demand=quiet)
        self.assertEqual(value['emergency_reserve_percent'], 3.0)
        self.assertEqual(value['effective_reserve_percent'], 3.0)
        self.assertEqual(value['released_percent'], 0.0)
        value = self.state(base=0, remaining_hours=1, demand=quiet)
        self.assertEqual(value['effective_reserve_percent'], 0.0)
        value = self.state(base=4, remaining_hours=0.001, demand=quiet)
        self.assertEqual(value['effective_reserve_percent'], 4.0)

    def test_disabled_mode_is_fixed_base(self):
        value = self.state(dynamic=False, remaining_hours=1,
                           demand={'state': 'quiet', 'reason': 'quiet'})
        self.assertEqual(value['release_state'], 'fixed')
        self.assertEqual(value['effective_reserve_percent'], 30.0)
        self.assertFalse(value['dynamic_enabled'])

    def test_untrustworthy_weekly_evidence_holds_base(self):
        quiet = {'state': 'quiet', 'reason': 'quiet'}
        cases = [
            ('expired reset', dict(remaining_hours=-1)),
            ('missing reset', dict(resets_at=None)),
            ('implausible far reset', dict(remaining_hours=8 * 24)),
            ('monthly duration', dict(window={'name': 'usages.limit_7d',
                                              'duration_minutes': 43200})),
            ('five-hour duration', dict(window={'name': 'usages.limit_7d',
                                                'duration_minutes': 300})),
            ('unknown weekly', dict(status='unknown', remaining_percent=None)),
            ('future sample', dict(as_of=NOW + 600)),
            ('missing sample', dict(as_of=None)),
            ('aged sample', dict(as_of=NOW - 901)),
        ]
        for label, extra in cases:
            with self.subTest(label):
                value = self.state(demand=quiet, **extra)
                self.assertEqual(value['effective_reserve_percent'], 30.0, label)
                self.assertIn(value['release_state'],
                              ('weekly_unknown', 'reset_unknown'), label)

    def test_demand_states_pause_release(self):
        cases = {
            'ultra_demand': 'ultra_demand',
            'unknown': 'demand_unknown',
        }
        for demand_state, expected in cases.items():
            with self.subTest(demand_state):
                value = self.state(demand={'state': demand_state, 'reason': 'x'})
                self.assertEqual(value['release_state'], expected)
                self.assertEqual(value['effective_reserve_percent'], 30.0)
        for missing in (None, {}, {'state': 'weird'}):
            with self.subTest(demand=missing):
                self.assertEqual(self.state(demand=missing)['effective_reserve_percent'], 30.0)

    def test_not_applicable_never_fabricates_a_reserve(self):
        weekly = {'status': 'not_applicable', 'remaining_percent': None, 'source': 'account',
                  'resets_at': None, 'window': None, 'as_of': NOW}
        value = kimi_reserve.compute_reserve_state(30, True, weekly, None, NOW)
        self.assertEqual(value['release_state'], 'not_applicable')
        self.assertEqual(value['effective_reserve_percent'], 0.0)
        self.assertEqual(value['released_percent'], 0.0)
        self.assertEqual(value['reserve_percent'], 30.0)
        # The absence of a weekly pool is a property of the account, so a
        # disabled dynamic toggle must not fabricate fixed protection either.
        disabled = kimi_reserve.compute_reserve_state(30, False, weekly, None, NOW)
        self.assertEqual(disabled['release_state'], 'not_applicable')
        self.assertEqual(disabled['effective_reserve_percent'], 0.0)
        self.assertFalse(disabled['dynamic_enabled'])
        self.assertEqual(disabled['reserve_percent'], 30.0)


class ReserveAdmissionTests(StateFixture):
    def test_admission_follows_the_same_effective_threshold(self):
        c = config()
        self.write_demand()
        # 42h left: effective 17.5, exactly as the release curve reports.
        state = quota.kimi_weekly_reserve_state(c, view(17.6, 42, now=NOW, sampled=NOW), now=NOW)
        self.assertEqual(state['effective_reserve_percent'], 17.5)
        self.assertTrue(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c,
            view(17.6, 42, now=NOW, sampled=NOW), now=NOW)[0])
        blocked = quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c,
            view(17.5, 42, now=NOW, sampled=NOW), now=NOW)
        self.assertEqual(blocked, (False, 'kimi_weekly_reserved_for_ultra'))
        self.assertEqual(quota.kimi_request_allowed(
            'kimi-for-coding/k3.1-pro', c, view(5.0, 42, now=NOW, sampled=NOW), now=NOW),
            (True, 'weekly_reserve_beneficiary'))
        # The lowered boundary is real work through the ordinary route too, and an
        # explicit pin cannot bypass either boundary.
        task = {'id': 'job-n', 'requested_profile': 'senior-code', 'tier': 'normal',
                'complexity': 'normal', 'status': 'queued'}
        live = time.time()
        self.write_demand(tasks_updated_at=live, quiet_since=live - 1801)
        self.assertEqual(quota.route(task, c, view(15.0, 42, now=live, sampled=live))[1],
                         'kimi_weekly_reserved_for_ultra')
        self.assertEqual(quota.route(task, c, view(20.0, 42, now=live, sampled=live))[0],
                         'senior-code')

    def test_unknown_or_untrusted_evidence_keeps_base_admission(self):
        c = config()
        # Older than the 15-minute sample horizon: no release, base comparison.
        self.write_demand()
        q = view(17.6, 42, now=NOW, sampled=NOW - 901)
        self.assertEqual(quota.kimi_weekly_reserve_state(c, q, now=NOW)['release_state'],
                         'weekly_unknown')
        self.assertFalse(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c, q, now=NOW)[0])
        # A future sample never releases either.
        q = view(17.6, 42, now=NOW, sampled=NOW + 600)
        self.assertFalse(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c, q, now=NOW)[0])
        # A stale demand snapshot holds the base as well.
        self.write_demand(tasks_updated_at=NOW - 3600)
        q = view(17.6, 42, now=NOW, sampled=NOW)
        self.assertEqual(quota.kimi_weekly_reserve_state(c, q, now=NOW)['effective_reserve_percent'],
                         30.0)

    def test_verified_weekless_plan_keeps_old_admission(self):
        c = config()
        acct = account(windows=[month_window(66.73)], weekly='not_applicable',
                       sampled_at=time.time())
        q = view(50.0, 42, now=NOW, sampled=NOW, stale=True, account_record=acct)
        state = quota.kimi_weekly_reserve_state(c, q, now=NOW)
        self.assertEqual((state['status'], state['release_state'],
                          state['effective_reserve_percent']),
                         ('not_applicable', 'not_applicable', 0.0))
        self.assertEqual(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c, q, now=NOW), (True, 'weekly_not_applicable'))
        # The same truthfulness holds with dynamic release disabled.
        disabled = config(kimi_weekly_reserve_dynamic=False)
        state = quota.kimi_weekly_reserve_state(disabled, q, now=NOW)
        self.assertEqual((state['release_state'], state['effective_reserve_percent']),
                         ('not_applicable', 0.0))
        self.assertEqual(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', disabled, q, now=NOW),
            (True, 'weekly_not_applicable'))

    def test_weekly_unknown_still_fails_closed(self):
        c = config()
        q = view(15.0, 42, now=NOW, sampled=NOW, stale=True)
        state = quota.kimi_weekly_reserve_state(c, q, now=NOW)
        self.assertEqual(state['release_state'], 'weekly_unknown')
        self.assertEqual(quota.kimi_request_allowed(
            'kimi-for-coding/kimi-for-coding', c, q, now=NOW),
            (False, 'kimi_weekly_unknown'))

    def test_iso_account_window_releases_and_weekly_refresh_resets_the_curve(self):
        c = config()
        self.write_demand()
        sample = time.time()
        self.write_demand(tasks_updated_at=sample, quiet_since=sample - 1801)
        iso = datetime.fromtimestamp(sample + 42 * 3600, tz=timezone.utc).isoformat()
        acct = account(windows=[{'name': 'limit7d', 'kind': 'weekly', 'limit': 1,
                                 'remaining': 0.15, 'remaining_percent': 15.0,
                                 'duration_minutes': 10080, 'resets_at': iso,
                                 'valid': True, 'source': 'kimi_code_account'}],
                       sampled_at=sample)
        q = {'kimi-for-coding': {'state': 'unavailable', 'stale': True, 'sampled_at': 0,
                                 'available': None, 'windows': [], 'account': acct}}
        state = quota.kimi_weekly_reserve_state(c, q, now=sample)
        self.assertEqual((state['source'], state['release_state']),
                         ('account_weekly', 'releasing'))
        self.assertAlmostEqual(state['effective_reserve_percent'], 17.5, delta=0.01)
        # A refreshed weekly reading (new reset) starts the curve over.
        later = datetime.fromtimestamp(sample + 100 * 3600, tz=timezone.utc).isoformat()
        acct['windows'][0]['resets_at'] = later
        state = quota.kimi_weekly_reserve_state(c, q, now=sample)
        self.assertEqual((state['release_state'], state['effective_reserve_percent']),
                         ('first_half', 30.0))

    def test_view_uses_the_same_contract_as_admission(self):
        c = config()
        self.write_demand()
        live = time.time()
        self.write_demand(tasks_updated_at=live, quiet_since=live - 1801)
        raw = {'kimi-for-coding': {
            'state': 'ok', 'stale': False, 'sampled_at': live, 'available': True,
            'windows': [dict(WEEK_5H), weekly_window(15.0, live + 42 * 3600)]}}
        viewed = quota.view(raw)
        reserve = viewed['kimi-for-coding']['weekly_reserve']
        self.assertEqual(reserve['release_state'], 'releasing')
        self.assertAlmostEqual(reserve['effective_reserve_percent'], 17.5, delta=0.01)
        for key in ('reserve_percent', 'status', 'remaining_percent', 'source', 'resets_at',
                    'effective_reserve_percent', 'released_percent',
                    'emergency_reserve_percent', 'dynamic_enabled', 'release_state',
                    'remaining_hours', 'demand'):
            self.assertIn(key, reserve)
        direct = quota.kimi_weekly_reserve_state(c, viewed)
        for key in ('reserve_percent', 'status', 'remaining_percent', 'source', 'resets_at',
                    'emergency_reserve_percent', 'dynamic_enabled', 'release_state'):
            self.assertEqual(reserve[key], direct[key], key)
        self.assertAlmostEqual(reserve['effective_reserve_percent'],
                               direct['effective_reserve_percent'], delta=0.01)
        self.assertAlmostEqual(reserve['released_percent'],
                               direct['released_percent'], delta=0.01)
        self.assertEqual(reserve['demand']['state'], direct['demand']['state'])


class DemandEvidenceTests(StateFixture):
    def test_first_publish_is_not_quiet_and_continuous_quiet_is_required(self):
        c = config()
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW, instance='pool-a')
        state = self.reserve_state()
        self.assertEqual(state['release_state'], 'demand_unknown')
        self.assertEqual(state['demand']['reason'], 'quiet_unobserved')
        # Continuous publishes within the freshness gap grow the observation;
        # the release opens only after the whole cooldown was observed quiet.
        stamp = NOW
        while stamp < NOW + 1800:
            stamp += 30
            kimi_reserve.publish_task_demand(c, [], IDENT, now=stamp, instance='pool-a')
        self.assertEqual(self.reserve_state(now=stamp)['release_state'], 'releasing')
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW + 1800, instance='pool-a')
        self.assertEqual(self.reserve_state(now=NOW + 1800)['release_state'], 'releasing')

    def test_restart_sampling_gap_and_rotation_restart_observation(self):
        c = config()
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600)
        self.assertEqual(self.reserve_state()['release_state'], 'releasing')
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW + 2, instance='pool-b')
        self.assertEqual(self.reserve_state(now=NOW + 2)['demand']['reason'],
                         'quiet_unobserved')
        # A gap longer than the freshness horizon resets even with one instance.
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600, instance='pool-a')
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW + 200, instance='pool-a')
        self.assertEqual(self.reserve_state(now=NOW + 200)['demand']['reason'],
                         'quiet_unobserved')
        # Account rotation starts its own observation; the old identity is gone.
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600, identity=IDENT)
        patcher = patch.object(kimi_reserve, 'current_identity', lambda: 'cred-rotated')
        patcher.start()
        self.patchers.append(patcher)
        kimi_reserve.publish_task_demand(c, [], 'cred-rotated', now=NOW + 1,
                                         instance='pool-a')
        patched = patch.object(quota, 'credential_identity', lambda provider: 'cred-rotated')
        patched.start()
        self.patchers.append(patched)
        self.assertEqual(self.reserve_state(now=NOW + 1)['demand']['reason'],
                         'quiet_unobserved')

    def test_clock_rollback_resets_observation_and_holds_base(self):
        c = config()
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600,
                          native_last_at=NOW + 120)
        rolled_back = NOW - 600
        kimi_reserve.publish_task_demand(c, [], IDENT, now=rolled_back, instance='test-pool')
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['quiet_since'], rolled_back)
        self.assertNotIn('native_last_at', raw)
        state = self.reserve_state(now=rolled_back)
        self.assertEqual((state['release_state'], state['demand']['reason']),
                         ('demand_unknown', 'quiet_unobserved'))
        # A heartbeat during the rollback is not allowed to overwrite or revive
        # the future-stamped evidence.
        self.assertFalse(kimi_reserve.record_native_activity(IDENT, now=rolled_back - 100))

    def test_ultra_backlog_and_native_cooldown_pause_release(self):
        c = config()
        self.write_demand()
        self.assertEqual(self.reserve_state()['release_state'], 'releasing')
        self.write_demand(queued_ultra=1)
        self.assertEqual(self.reserve_state()['release_state'], 'ultra_demand')
        self.write_demand(active_ultra=2)
        self.assertEqual(self.reserve_state()['release_state'], 'ultra_demand')
        # Actual configured tier/profile only, never title text.
        self.write_demand(queued_ultra=0, active_ultra=0)
        tasks = [
            {'status': 'queued', 'tier': 'ultra', 'title': 'ordinary'},
            {'status': 'queued', 'tier': 'normal', 'requested_profile': 'ultra-reasoning',
             'title': 'ultra ultra ultra'},
            {'status': 'queued', 'tier': 'normal', 'title': 'ultra'},
            {'status': 'completed', 'tier': 'ultra'},
            {'status': 'queued', 'tier': 'deep', 'requested_profile': 'deep-research'},
        ]
        queued, active = kimi_reserve.task_demand_counts(tasks, c)
        self.assertEqual((queued, active), (2, 0))
        # Native activity inside 30 minutes pauses; just outside resumes.
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW, instance='test-pool')
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600,
                          native_last_at=NOW - 600)
        self.assertEqual(self.reserve_state()['release_state'], 'ultra_demand')
        self.write_demand(tasks_updated_at=NOW, quiet_since=NOW - 3600,
                          native_last_at=NOW - 1800)
        self.assertEqual(self.reserve_state()['release_state'], 'releasing')

    def test_untrustworthy_demand_never_releases(self):
        cases = [
            ('missing', None),
            ('corrupt json', '{not json'),
            ('journal counts', {'queued_ultra': 'one'}),
            ('negative counts', {'active_ultra': -1}),
            ('stale', {'tasks_updated_at': NOW - 3600}),
            ('future', {'tasks_updated_at': NOW + 600}),
            ('future native', {'native_last_at': NOW + 600}),
            ('other identity', {'identity': 'other-account'}),
            ('far old quiet', {'quiet_since': NOW - 8 * 86400}),
        ]
        for label, fields in cases:
            with self.subTest(label):
                path = self.state / kimi_reserve.DEMAND_FILE
                if fields is None:
                    if path.exists():
                        path.unlink()
                elif isinstance(fields, str):
                    path.write_text(fields)
                else:
                    self.write_demand(**fields)
                state = self.reserve_state()
                self.assertEqual(state['release_state'], 'demand_unknown', label)
                self.assertEqual(state['effective_reserve_percent'], 30.0, label)
        # Oversized metadata is never parsed as evidence.
        (self.state / kimi_reserve.DEMAND_FILE).write_text('x' * (70 * 1024))
        self.assertEqual(self.reserve_state()['release_state'], 'demand_unknown')

    def test_merge_preserves_concurrent_gate_and_pool_fields(self):
        c = config()
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW, instance='test-pool')
        self.assertTrue(kimi_reserve.record_native_activity(IDENT, now=NOW - 5))
        kimi_reserve.publish_task_demand(c, [], IDENT, now=NOW + 2, instance='test-pool')
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['native_last_at'], NOW - 5)
        self.assertEqual(raw['tasks_updated_at'], NOW + 2)
        self.assertTrue(kimi_reserve.record_native_activity(IDENT, now=NOW + 3))
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['native_last_at'], NOW + 3)
        self.assertEqual(raw['queued_ultra'], 0)
        # Concurrent writers keep the file parseable and both evidence classes.
        def loop(fn):
            for _ in range(30):
                fn()
        threads = [
            threading.Thread(target=loop, args=(lambda: kimi_reserve.publish_task_demand(
                c, [], IDENT, now=NOW + 4, instance='test-pool'),)),
            threading.Thread(target=loop, args=(lambda: kimi_reserve.record_native_activity(
                IDENT, now=NOW + 4),)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['identity'], IDENT)
        self.assertEqual(raw['tasks_updated_at'], NOW + 4)
        self.assertEqual(raw['native_last_at'], NOW + 4)
        self.assertIn('quiet_since', raw)

    def test_suspended_writer_cannot_overwrite_the_current_account(self):
        c = config()
        self.write_demand(identity='cred-current', native_last_at=NOW - 30,
                          quiet_since=NOW - 3600)
        patcher = patch.object(kimi_reserve, 'current_identity', lambda: 'cred-current')
        patcher.start()
        self.patchers.append(patcher)
        # The old account's request/heartbeat is refused, leaving current evidence.
        self.assertFalse(kimi_reserve.record_native_activity(IDENT, now=NOW))
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['identity'], 'cred-current')
        self.assertEqual(raw['native_last_at'], NOW - 30)
        self.assertEqual(raw['quiet_since'], NOW - 3600)
        kimi_reserve.publish_task_demand(
            c, [{'status': 'queued', 'tier': 'ultra'}], IDENT, now=NOW, instance='old-pool')
        raw = json.loads((self.state / kimi_reserve.DEMAND_FILE).read_text())
        self.assertEqual(raw['queued_ultra'], 0)
        self.assertEqual(raw['native_last_at'], NOW - 30)

    def test_current_identity_from_captured_key_matches_shared_identity(self):
        key = 'test-inference-key'
        expected = hashlib.sha256(key.encode()).hexdigest()
        import kimi_gate
        self.assertEqual(kimi_gate._native_identity(key), expected)
        with patch.object(common, 'config', lambda: {}), \
                patch.object(common, 'auth_key', lambda provider: key):
            self.assertEqual(common.credential_identity('kimi-for-coding'), expected)

    def test_process_local_guard_overrides_quiet_files(self):
        self.write_demand()
        self.assertEqual(self.reserve_state()['release_state'], 'releasing')
        now = time.monotonic()
        kimi_reserve.note_local_native_activity(now=now)
        state = self.reserve_state()
        self.assertEqual((state['release_state'], state['demand']['reason']),
                         ('ultra_demand', 'local_guard'))
        self.assertEqual(state['effective_reserve_percent'], 30.0)
        self.assertEqual(kimi_reserve.local_demand_state(now=now + 1800), None)
        kimi_reserve.reset_local_guard()
        kimi_reserve.note_local_uncertainty(now=now)
        state = self.reserve_state()
        self.assertEqual((state['release_state'], state['demand']['reason']),
                         ('demand_unknown', 'local_guard'))
        self.assertEqual(kimi_reserve.local_demand_state(now=now + 1801), None)


class GateTrackingTests(StateFixture):
    """Real loopback gate over HTTP; the upstream connection is a local stub."""

    PASSWORD = 'test-gate-password'
    KEY = 'test-inference-key'

    def setUp(self):
        super().setUp()
        (self.state / 'server-password').write_text(self.PASSWORD + '\n')
        (self.state / 'server-password').chmod(0o600)
        self.config_path.write_text(json.dumps(config(kimi_gate={'enabled': True, 'port': 51531})))
        live = time.time()
        self.quota_view = quota.view(view(20.0, 42, now=live, sampled=live))
        self.patchers.extend([
            patch.object(common, 'auth_key', lambda provider: self.KEY),
            patch.object(kimi_reserve, 'current_identity',
                         lambda: hashlib.sha256(self.KEY.encode()).hexdigest()),
            patch.object(quota, 'refresh', lambda force=False: self.quota_view),
        ])
        for patcher in self.patchers[-3:]:
            patcher.start()
        import kimi_gate
        self.gate = kimi_gate
        self.server = kimi_gate.create_server(common.config(), port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        kimi_reserve.reset_local_guard()
        super().tearDown()

    def post(self, model):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'x'}],
                               'stream': True})
            connection.request('POST', '/v1/chat/completions', body=body,
                               headers={'Authorization': 'Bearer ' + self.PASSWORD,
                                        'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def fake_upstream(self, chunks=(b'data: one\n\n', b'data: two\n\n')):
        class Response:
            status = 200

            def __init__(self):
                self.chunks = list(chunks)

            def getheader(self, name):
                return 'text/event-stream'

            def read1(self, size):
                return self.chunks.pop(0) if self.chunks else b''

            def close(self):
                return None

        made = []

        def factory(*args, **kwargs):
            class Connection:
                def __init__(self):
                    self.response = Response()

                def request(self, method, path, body=None, headers=None):
                    self.requested = (method, path)

                def getresponse(self):
                    return self.response

                def close(self):
                    return None

            connection = Connection()
            made.append(connection)
            return connection

        return made, factory

    def test_unverified_start_fails_closed_before_upstream(self):
        made, factory = self.fake_upstream()
        with patch('http.client.HTTPSConnection', factory), \
                patch.object(kimi_reserve, 'record_native_activity', lambda identity: False):
            status, body = self.post('k3.1-pro')
        payload = json.loads(body)
        self.assertEqual(status, 503)
        self.assertEqual(payload['error']['code'], 'kimi_demand_unavailable')
        self.assertEqual(made, [])
        self.assertEqual(kimi_reserve.local_demand_state(), 'unknown')
        # That uncertainty also holds the base reserve for later ordinary Kimi.
        status, body = self.post('kimi-for-coding')
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)['error']['code'],
                         'kimi_weekly_reserved_for_ultra')

    def test_start_chunk_and_end_carry_the_admitted_identity(self):
        made, factory = self.fake_upstream()
        calls = []

        def record(identity):
            calls.append(identity)
            return True

        with patch('http.client.HTTPSConnection', factory), \
                patch.object(kimi_reserve, 'record_native_activity', record):
            status, body = self.post('k3.1-pro')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'data: one\n\ndata: two\n\n')
        expected = hashlib.sha256(self.KEY.encode()).hexdigest()
        self.assertTrue(calls)
        self.assertTrue(all(identity == expected for identity in calls), calls)
        self.assertEqual(kimi_reserve.local_demand_state(), 'ultra_demand')

    def test_stream_heartbeats_are_bounded_and_identity_scoped(self):
        made, factory = self.fake_upstream(chunks=[b'a', b'b', b'c', b'd'])
        calls = []

        def record(identity):
            calls.append(identity)
            return True

        with patch('http.client.HTTPSConnection', factory), \
                patch.object(kimi_reserve, 'record_native_activity', record), \
                patch.object(self.gate, 'NATIVE_HEARTBEAT_SECONDS', 0):
            status, body = self.post('k3.1-pro')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'abcd')
        # start + one heartbeat per chunk + end, all under the captured identity.
        self.assertGreaterEqual(len(calls), 4)
        expected = hashlib.sha256(self.KEY.encode()).hexdigest()
        self.assertTrue(all(identity == expected for identity in calls), calls)

    def test_failed_heartbeat_keeps_delivery_and_holds_later_admission(self):
        made, factory = self.fake_upstream(chunks=[b'a', b'b', b'c'])
        calls = []

        def record(identity):
            calls.append(identity)
            # Start accepted; the first stream heartbeat fails.
            return len(calls) != 2

        with patch('http.client.HTTPSConnection', factory), \
                patch.object(kimi_reserve, 'record_native_activity', record), \
                patch.object(self.gate, 'NATIVE_HEARTBEAT_SECONDS', 0):
            status, body = self.post('k3.1-pro')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'abc')
        self.assertEqual(kimi_reserve.local_demand_state(), 'unknown')
        # Ordinary Kimi at 20% weekly would pass the 17.5% released boundary but
        # must hold the 30% base while the Gate's own evidence is unverified.
        status, body = self.post('kimi-for-coding')
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)['error']['code'],
                         'kimi_weekly_reserved_for_ultra')

    def test_ordinary_kimi_admission_never_records_native_ultra(self):
        # Released boundary needs a genuinely quiet, continuously observed pool.
        self.write_demand(tasks_updated_at=time.time(), quiet_since=time.time() - 3600)
        made, factory = self.fake_upstream(chunks=[b'{}'])
        calls = []
        with patch('http.client.HTTPSConnection', factory), \
                patch.object(kimi_reserve, 'record_native_activity',
                             lambda identity: calls.append(identity) or True):
            status, body = self.post('kimi-for-coding')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{}')
        self.assertEqual(calls, [])


class ManagementAndInstallTests(StateFixture):
    def test_settings_expose_validate_and_persist_the_boolean(self):
        base = {'version': 1, 'server_url': 'http://127.0.0.1:1234',
                'console_url': 'http://127.0.0.1:1235', 'opencode_binary': '/opt/opencode',
                'max_parallel_per_owner': 4, 'kimi_reserve_percent': 20,
                'profiles': {'fallback': {'model': 'deepseek/deepseek-flash', 'label': 'Flash'},
                             'senior-code': {'model': 'kimi-for-coding/kimi-for-coding',
                                             'label': 'Kimi'}}}
        self.config_path.write_text(json.dumps(base))
        exposed = management.settings()
        self.assertTrue(exposed['kimi_weekly_reserve_dynamic'])
        stored = dict(base, kimi_weekly_reserve_dynamic=False,
                      routing={'fast': 'fallback', 'background': 'senior-code',
                               'deep': 'fallback'})
        self.config_path.write_text(json.dumps(stored))
        self.assertFalse(management.settings()['kimi_weekly_reserve_dynamic'])
        body = {'profiles': stored['profiles'], 'max_parallel_per_owner': 4,
                'kimi_reserve_percent': 20, 'kimi_weekly_reserve_dynamic': False,
                'routing': {'fast': 'fallback', 'background': 'senior-code',
                            'deep': 'fallback'}}
        candidate = management._validate_settings(body)
        self.assertIs(candidate['kimi_weekly_reserve_dynamic'], False)
        rejected = dict(body, kimi_weekly_reserve_dynamic='yes')
        with self.assertRaises(ValueError):
            management._validate_settings(rejected)
        self.config_path.write_text(json.dumps(stored))
        with patch.object(common, 'api', return_value={}):
            saved = management.save_settings(body)
        written = json.loads(self.config_path.read_text())
        self.assertIs(written['kimi_weekly_reserve_dynamic'], False)
        self.assertEqual(written['revision'], stored.get('revision', 0) + 1)
        self.assertFalse(saved['kimi_weekly_reserve_dynamic'])
        # Re-enabling persists true exactly as given.
        body['kimi_weekly_reserve_dynamic'] = True
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
        self.assertIs(json.loads(self.config_path.read_text())['kimi_weekly_reserve_dynamic'], True)

    def test_account_status_adds_the_same_reserve_contract(self):
        import kimi_account
        sample = time.time()
        resets = datetime.fromtimestamp(sample + 42 * 3600,
                                        tz=timezone.utc).isoformat()
        usage = account(windows=[
            {'name': 'limit7d', 'kind': 'weekly', 'limit': 1, 'remaining': 0.2,
             'remaining_percent': 20.0, 'duration_minutes': 10080,
             'resets_at': resets, 'valid': True}], sampled_at=sample)
        week = weekly_window(20.0, sample + 42 * 3600)
        cache = {'kimi-for-coding': {
            'state': 'ok', 'stale': False, 'sampled_at': sample, 'checked_at': time.time(),
            '_credential': IDENT, 'available': True,
            'windows': [dict(WEEK_5H), week], 'account': usage}}
        (self.state / 'quota.json').write_text(json.dumps(cache))
        self.write_demand(tasks_updated_at=time.time(), quiet_since=time.time() - 3600)
        identity = patch.object(quota, 'credential_identity', lambda provider: IDENT)
        identity.start()
        self.patchers.append(identity)
        with patch.object(kimi_account, 'fetch', return_value=usage) as fetch, \
                patch.object(kimi_account, 'status', return_value={'state': 'ok'}):
            result = management.kimi_account_status()
        self.assertEqual(fetch.call_count, 1)
        reserve = result['weekly_reserve']
        self.assertEqual(reserve['release_state'], 'releasing')
        self.assertAlmostEqual(reserve['effective_reserve_percent'], 17.5, delta=0.01)
        self.assertEqual(reserve['status'], 'known')
        self.assertIn(reserve['source'], ('account_weekly', 'telemetry_limit_7d'))
        self.assertEqual(set(result), {'status', 'usage', 'weekly_reserve'})
        # A cache from another credential is ignored, and the account reading is
        # still the same authoritative helper; no weekly pool is fabricated.
        cache['kimi-for-coding']['_credential'] = 'other-credential'
        (self.state / 'quota.json').write_text(json.dumps(cache))
        other = account(windows=[month_window(66.0)], weekly='not_applicable',
                        sampled_at=time.time())
        display = quota.kimi_weekly_reserve_display(other)
        self.assertEqual(display['release_state'], 'not_applicable')
        self.assertEqual(display['effective_reserve_percent'], 0.0)
        no_weekly = quota.kimi_weekly_reserve_display(account(
            windows=[month_window(66.0)], weekly='unknown', sampled_at=time.time()))
        self.assertEqual(no_weekly['release_state'], 'weekly_unknown')
        self.assertEqual(no_weekly['effective_reserve_percent'], 30.0)

    def test_install_preset_and_idempotent_migration(self):
        import install
        self.assertIs(install.TOP_LEVEL_DEFAULTS['kimi_weekly_reserve_dynamic'], True)
        value = {'profiles': install.default_profiles(None, 'deepseek-kimi'),
                 'routing': {'fast': 'fallback', 'background': 'senior-code',
                             'deep': 'deep-research', 'ultra': install.KIMI_ULTRA_PROFILE}}
        self.assertTrue(install.migrate_kimi_four_tiers(value))
        self.assertIs(value['kimi_weekly_reserve_dynamic'], True)
        self.assertFalse(install.migrate_kimi_four_tiers(value))
        disabled = json.loads(json.dumps(value))
        disabled['kimi_weekly_reserve_dynamic'] = False
        self.assertFalse(install.migrate_kimi_four_tiers(disabled))
        self.assertIs(disabled['kimi_weekly_reserve_dynamic'], False)


if __name__ == '__main__':
    unittest.main()
