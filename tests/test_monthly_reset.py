import calendar
import copy
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import management
import quota
import worker

MONTHLY = ("You've reached your monthly usage limit for this billing cycle. "
           "Your quota will be refreshed in the next cycle. To continue now, purchase extra "
           "usage or upgrade your plan: https://www.kimi.com/membership/subscription?tab=quota")
IDENT = 'cred-kimi'


def boundary_epoch(day, hour, minute, zone, year, month):
    """Expected boundary epoch, clamped to the month's last day, in the schedule's zone."""
    day = min(day, calendar.monthrange(year, month)[1])
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone)).timestamp()


class MonthlyResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.c = {'server_url': 'http://127.0.0.1:1', 'auto_approve': False, 'kimi_reserve_percent': 0,
                  'profiles': {'fast-code': {'model': 'deepseek/deepseek-flash'},
                               'senior-code': {'model': 'kimi-for-coding/kimi-for-coding'},
                               'deep-research': {'model': 'kimi-for-coding/k3'}}}
        self.write_config(self.c)
        self.ident = IDENT
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(quota, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(quota, 'credential_identity',
                                      lambda p: self.ident if p == 'kimi-for-coding' else 'cred-ds')]
        for p in self.patchers:
            p.start()
        common.init()
        self.q = {p: {'state': 'ok', 'stale': False, 'sampled_at': time.time(), 'available': True,
                      'windows': [{'name': 'window_0', 'remaining_percent': 80.0}]}
                  for p in ('deepseek', 'kimi-for-coding')}

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def write_config(self, value):
        self.config.write_text(json.dumps(value))

    def schedule(self, enabled=True, day=19, time_text='12:00', zone='Asia/Shanghai'):
        self.c['kimi_monthly_reset'] = {'enabled': enabled, 'day': day, 'time': time_text, 'timezone': zone}
        self.write_config(self.c)
        return quota.configured_monthly_schedule()

    def open_block(self, opened_at, reason='monthly_usage_limit', credential=None, ids=('m-1',)):
        common.write_json(self.state / 'billing.json', {'kimi-for-coding': {
            'credential': credential or self.ident, 'opened_at': opened_at, 'message': 'monthly limit',
            'message_ids': list(ids), 'reason': reason}})

    def stored_block(self):
        return common.read_json(self.state / 'billing.json', {}).get('kimi-for-coding')

    def recent_boundary(self, now=None):
        sched = quota.configured_monthly_schedule()
        return quota.monthly_boundaries(time.time() if now is None else now, sched)[0]

    def monthly_message(self, message_id='m-x', occurred_at=None):
        return {'source': 'model', 'provider': 'kimi-for-coding', 'billing': True,
                'billing_reason': 'monthly_usage_limit', 'message': MONTHLY,
                'message_id': message_id, 'occurred_at': occurred_at}

    def historical_task(self):
        return {'id': 'job-historical', 'status': 'failed', 'profile': 'senior-code',
                'requested_profile': 'senior-code', 'errors': [self.monthly_message('m-old')]}

    # -- defaults and normalization --------------------------------------
    def test_generic_defaults_are_disabled_and_day19_is_never_implicit(self):
        self.assertEqual(quota.MONTHLY_RESET_DEFAULTS,
                         {'enabled': False, 'day': 1, 'time': '12:00', 'timezone': 'Asia/Shanghai'})
        self.assertEqual(quota.configured_monthly_schedule(), quota.MONTHLY_RESET_DEFAULTS)
        self.assertEqual(management.settings()['kimi_monthly_reset'], quota.MONTHLY_RESET_DEFAULTS)
        # Day 19 is honored only when explicitly stored and enabled; nothing else activates it.
        for legacy in (None, {}, 'junk', {'enabled': True, 'day': '19', 'time': '12:00',
                                          'timezone': 'Asia/Shanghai'}, {'day': 19}):
            with self.subTest(legacy=legacy):
                normalized = quota.normalize_monthly_schedule(legacy)
                self.assertFalse(normalized['enabled'])
                self.assertEqual(normalized['time'], '12:00')
                self.assertEqual(normalized['timezone'], 'Asia/Shanghai')
        self.assertEqual(quota.normalize_monthly_schedule({'day': 19})['day'], 19)
        self.assertEqual(quota.normalize_monthly_schedule('junk')['day'], 1)
        explicit = quota.normalize_monthly_schedule(
            {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'})
        self.assertEqual(explicit, {'enabled': True, 'day': 19, 'time': '12:00',
                                    'timezone': 'Asia/Shanghai'})

    def test_validation_rejects_invalid_schedules_and_accepts_normalized(self):
        valid = {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
        self.assertEqual(quota.validate_monthly_schedule(valid), valid)
        for bad in ({'enabled': 'yes', 'day': 19, 'time': '12:00', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': 0, 'time': '12:00', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': 32, 'time': '12:00', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': True, 'time': '12:00', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': 19, 'time': '25:00', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': 19, 'time': 'noon', 'timezone': 'Asia/Shanghai'},
                    {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Mars/Base'},
                    {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': ''},
                    {'enabled': True, 'day': 19, 'time': '12:00'},
                    'junk'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    quota.validate_monthly_schedule(bad)

    def test_invalid_legacy_schedule_on_disk_fails_closed(self):
        self.c['kimi_monthly_reset'] = {'enabled': True, 'day': 19, 'time': '12:00', 'timezone': 'Mars/Base'}
        self.write_config(self.c)
        self.assertFalse(quota.configured_monthly_schedule()['enabled'])
        exported = management.settings()['kimi_monthly_reset']
        self.assertFalse(exported['enabled'])
        self.assertEqual(exported['timezone'], 'Asia/Shanghai')
        boundary = self.recent_boundary()
        self.open_block(boundary - 10)
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    # -- boundary release semantics --------------------------------------
    def test_release_before_exact_and_after_boundary(self):
        self.schedule()
        boundary, following = quota.monthly_boundaries(time.time(), quota.configured_monthly_schedule())
        self.open_block(boundary - 0.001)
        self.assertTrue(quota.scheduled_release('kimi-for-coding', now=boundary))
        self.assertEqual(self.stored_block()['cleared_at'], boundary)
        self.assertEqual(self.stored_block()['released'], 'scheduled_reset')
        # Exactly at the boundary the block is not "strictly before" it; neither is one opened after.
        self.open_block(boundary)
        self.assertFalse(quota.scheduled_release('kimi-for-coding', now=boundary))
        self.open_block(boundary + 1)
        self.assertFalse(quota.scheduled_release('kimi-for-coding', now=following - 1))
        before = self.stored_block()
        self.assertNotIn('cleared_at', before)
        # The next cycle boundary releases the same-cycle re-block.
        self.assertTrue(quota.scheduled_release('kimi-for-coding', now=following + 1))
        self.assertEqual(self.stored_block()['cleared_at'], following)

    def test_refresh_releases_stale_block_even_on_cache_hit(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 60)
        self.q['kimi-for-coding']['checked_at'] = time.time()
        self.q['kimi-for-coding']['sampled_at'] = time.time()
        self.q['kimi-for-coding']['_credential'] = self.ident
        self.q['deepseek']['checked_at'] = time.time()
        self.q['deepseek']['sampled_at'] = time.time()
        self.q['deepseek']['_credential'] = 'cred-ds'
        common.write_json(self.state / 'quota.json', self.q)
        with patch.object(quota, 'fetch_one', side_effect=AssertionError('cache hit must not fetch')):
            view = quota.refresh()
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        self.assertEqual(self.stored_block()['released'], 'scheduled_reset')
        # The read-only view exposes the same normalized schedule plus the ISO8601 next boundary.
        reset = view['kimi-for-coding']['monthly_reset']
        self.assertEqual(reset['enabled'], True)
        self.assertEqual(reset['day'], 19)
        self.assertEqual(reset['time'], '12:00')
        self.assertEqual(reset['timezone'], 'Asia/Shanghai')
        expected_next = quota.monthly_boundaries(time.time(), quota.configured_monthly_schedule())[1]
        self.assertEqual(reset['next_reset_at'],
                         datetime.fromtimestamp(expected_next, tz=timezone.utc).isoformat())

    def test_no_repeated_clear_loop_within_one_cycle(self):
        self.schedule()
        boundary, following = quota.monthly_boundaries(time.time(), quota.configured_monthly_schedule())
        self.open_block(boundary - 60)
        self.assertTrue(quota.scheduled_release('kimi-for-coding', now=boundary + 5))
        # A genuine new post-boundary error re-blocks and stays blocked for this cycle.
        quota.trip('kimi-for-coding', MONTHLY, 'm-new', boundary + 6, 'monthly_usage_limit')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        self.assertFalse(quota.scheduled_release('kimi-for-coding', now=boundary + 3600))
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        self.assertTrue(quota.scheduled_release('kimi-for-coding', now=following + 1))
        self.assertIsNone(quota.billing_block('kimi-for-coding'))

    def test_delayed_check_uses_boundary_not_check_time_watermark(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 10)
        self.assertTrue(quota.scheduled_release('kimi-for-coding', now=boundary + 90000))
        self.assertEqual(self.stored_block()['cleared_at'], boundary)
        # A genuine error that happened after the boundary but before this late check still counts.
        quota.trip('kimi-for-coding', MONTHLY, 'm-late-real', boundary + 5, 'monthly_usage_limit')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_duplicate_and_stale_messages_never_relatch_after_release(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 120, ids=('m-seen',))
        self.assertTrue(quota.scheduled_release('kimi-for-coding'))
        # Same stored message ID is always ignored.
        quota.trip('kimi-for-coding', MONTHLY, 'm-seen', boundary - 60, 'monthly_usage_limit')
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # A never-seen pre-reset error predates the boundary watermark and is ignored.
        quota.trip('kimi-for-coding', MONTHLY, 'm-stale', boundary - 60, 'monthly_usage_limit')
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # A genuinely new post-reset error re-blocks.
        quota.trip('kimi-for-coding', MONTHLY, 'm-fresh', boundary + 60, 'monthly_usage_limit')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_positive_telemetry_never_clears_monthly_with_or_without_schedule(self):
        blocked = self.recent_boundary()
        # Disabled default: sticky monthly behavior is unchanged.
        self.open_block(blocked - 60)
        with patch.object(quota, 'fetch_one', side_effect=lambda p: {
                'state': 'ok', 'available': True, 'sampled_at': time.time(),
                'windows': [{'name': 'overall', 'remaining_percent': 100.0}], 'balances': []}):
            view = quota.refresh(force=True)
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        self.assertTrue(view['kimi-for-coding']['monthly_plan_exhausted'])
        self.assertEqual(view['kimi-for-coding']['monthly_reset']['enabled'], False)
        self.assertIsNone(view['kimi-for-coding']['monthly_reset']['next_reset_at'])
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        # Enabled schedule with a block opened after the boundary: telemetry still cannot clear it.
        self.schedule()
        self.open_block(self.recent_boundary() + 60)
        with patch.object(quota, 'fetch_one', side_effect=lambda p: {
                'state': 'ok', 'available': True, 'sampled_at': time.time(),
                'windows': [{'name': 'overall', 'remaining_percent': 100.0}], 'balances': []}):
            quota.refresh(force=True)
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_release_preserves_binding_ids_and_watermark(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 30, ids=('a', 'b'))
        self.assertTrue(quota.scheduled_release('kimi-for-coding'))
        stored = self.stored_block()
        self.assertEqual(stored['credential'], self.ident)
        self.assertEqual(stored['message_ids'], ['a', 'b'])
        self.assertEqual(stored['reason'], 'monthly_usage_limit')
        self.assertEqual(stored['opened_at'], boundary - 30)
        self.assertEqual(stored['cleared_at'], boundary)
        self.assertEqual(stored['released'], 'scheduled_reset')

    def test_release_skips_rotated_credential_and_non_monthly_blocks(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 30, credential='old-cred')
        self.ident = 'new-cred'
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        # Other reasons are never touched by the monthly schedule.
        self.ident = IDENT
        self.open_block(boundary - 30, reason='insufficient_balance')
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        with patch.object(quota, 'fetch_one', side_effect=lambda p: {
                'state': 'ok', 'available': True, 'sampled_at': time.time(),
                'windows': [{'name': 'overall', 'remaining_percent': 100.0}], 'balances': []}):
            quota.refresh(force=True)
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        self.assertEqual(self.stored_block()['released'], 'quota')

    # -- verified model success ------------------------------------------
    def test_observe_model_success_guards_and_atomic_clear(self):
        opened = time.time() - 100
        self.open_block(opened, ids=('m-err',))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', None, opened + 1, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', '', opened + 1, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', 'other', opened + 1, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, opened, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, opened - 1, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, opened + 2, opened + 1))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, None, opened + 2))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, 'x', opened + 2))
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))
        self.assertTrue(quota.observe_model_success('kimi-for-coding', self.ident, opened + 1, opened + 5))
        stored = self.stored_block()
        self.assertEqual(stored['released'], 'model_success')
        self.assertEqual(stored['cleared_at'], opened + 5)
        self.assertEqual(stored['credential'], self.ident)
        self.assertEqual(stored['message_ids'], ['m-err'])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))

    def test_model_success_watermark_reblocks_new_errors_only(self):
        opened = time.time() - 100
        self.open_block(opened)
        self.assertTrue(quota.observe_model_success('kimi-for-coding', self.ident, opened + 1, opened + 10))
        quota.trip('kimi-for-coding', MONTHLY, 'm-stale', opened + 5, 'monthly_usage_limit')
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        quota.trip('kimi-for-coding', MONTHLY, 'm-new', opened + 20, 'monthly_usage_limit')
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_model_success_never_clears_other_reasons(self):
        self.open_block(time.time() - 100, reason='insufficient_balance')
        self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident,
                                                     time.time() - 90, time.time() - 80))
        self.assertIsNotNone(quota.billing_block('kimi-for-coding'))

    def test_model_success_requires_matching_current_credential(self):
        opened = time.time() - 100
        self.open_block(opened, credential=IDENT)
        # Rotated credential: neither the old request identity nor the new one may clear.
        self.ident = 'rotated-cred'
        self.assertFalse(quota.observe_model_success('kimi-for-coding', IDENT, opened + 1, opened + 5))
        self.assertFalse(quota.observe_model_success('kimi-for-coding', 'rotated-cred', opened + 1, opened + 5))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        # Removed credential (identity unknown) can never clear either.
        self.ident = None
        self.assertFalse(quota.observe_model_success('kimi-for-coding', IDENT, opened + 1, opened + 5))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        self.ident = IDENT
        self.assertTrue(quota.observe_model_success('kimi-for-coding', IDENT, opened + 1, opened + 5))

    def test_helpers_reject_non_finite_and_non_positive_timestamps(self):
        nan, inf = float('nan'), float('inf')
        opened = time.time() - 100
        self.open_block(opened)
        for started, completed in ((nan, opened + 1), (opened, nan), (inf, opened + 1),
                                   (opened, inf), (nan, nan), (0, opened + 1), (opened, 0),
                                   (-1, opened + 1), (opened, -1), (True, opened + 1)):
            with self.subTest(started=started, completed=completed):
                self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident, started, completed))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        # Malformed legacy block state with non-finite/non-positive/non-numeric opened_at
        # fails closed for both helpers instead of bypassing chronology.
        self.schedule()
        for bad in (nan, inf, 0, -5, 'yesterday', None, True):
            with self.subTest(opened=bad):
                self.open_block(bad)
                self.assertFalse(quota.observe_model_success('kimi-for-coding', self.ident,
                                                             time.time() - 1, time.time()))
                self.assertFalse(quota.scheduled_release('kimi-for-coding'))
                self.assertIsNone(self.stored_block().get('cleared_at'))
        with patch.object(quota, 'monthly_boundaries', return_value=(nan, nan)):
            self.open_block(opened)
            self.assertFalse(quota.scheduled_release('kimi-for-coding'))

    def test_scheduled_release_requires_matching_current_credential(self):
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 30)
        self.ident = None
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        self.ident = 'rotated-cred'
        self.assertFalse(quota.scheduled_release('kimi-for-coding'))
        self.assertIsNone(self.stored_block().get('cleared_at'))
        self.ident = IDENT
        self.assertTrue(quota.scheduled_release('kimi-for-coding'))
        self.assertEqual(self.stored_block()['released'], 'scheduled_reset')

    def test_verified_success_makes_provider_routable_again(self):
        self.open_block(time.time() - 100)
        auto = {'profile': 'auto', 'requested_profile': 'auto', 'complexity': 'normal'}
        self.assertEqual(quota.route(auto, self.c, self.q)[1], 'provider_billing_blocked')
        self.assertTrue(quota.observe_model_success('kimi-for-coding', self.ident,
                                                    time.time() - 90, time.time() - 80))
        self.assertEqual(quota.route(auto, self.c, self.q)[0], 'senior-code')

    # -- advisory exhaustion vs auto routing ------------------------------
    def test_explicit_profile_may_retry_while_auto_and_alternatives_avoid(self):
        self.open_block(time.time() - 100)
        auto = {'profile': 'auto', 'requested_profile': 'auto', 'complexity': 'normal'}
        self.assertIsNone(quota.route(auto, self.c, self.q)[0])
        self.assertEqual(quota.route(auto, self.c, self.q)[1], 'provider_billing_blocked')
        explicit = {'profile': 'senior-code', 'requested_profile': 'senior-code', 'complexity': 'normal'}
        self.assertEqual(quota.route(explicit, self.c, self.q),
                         ('senior-code', 'explicit_profile_monthly_retry'))
        deep = {'profile': 'deep-research', 'requested_profile': 'deep-research', 'complexity': 'deep'}
        self.assertEqual(quota.route(deep, self.c, self.q)[0], 'deep-research')
        # Candidate alternatives still avoid the exhausted provider.
        self.assertEqual([a['profile'] for a in quota.alternatives(explicit, self.c, self.q)], ['fast-code'])
        # A non-monthly block stays an absolute prohibition even for an explicit profile.
        common.write_json(self.state / 'billing.json', {'kimi-for-coding': {
            'credential': self.ident, 'opened_at': time.time() - 100, 'message': 'Insufficient Balance',
            'message_ids': ['m-bal'], 'reason': 'insufficient_balance'}})
        self.assertIsNone(quota.route(explicit, self.c, self.q)[0])
        self.assertEqual(quota.route(explicit, self.c, self.q)[1], 'provider_billing_blocked')

    def test_explicit_retry_never_overrides_endpoint_exhaustion(self):
        self.open_block(time.time() - 100)
        for profile in ('senior-code', 'deep-research'):
            task = {'requested_profile': profile, 'complexity': 'deep'}
            for stale in (False, True):
                q = copy.deepcopy(self.q)
                q['kimi-for-coding'].update(available=False, stale=stale,
                    sampled_at=time.time() - (3600 if stale else 0),
                    windows=[{'valid': True, 'remaining': 0, 'remaining_percent': 0}])
                for view in (q, quota.view(q)):
                    self.assertEqual(quota.route(task, self.c, view),
                                     (None, 'quota_exhausted_or_account_unavailable'))
        no_alternatives = copy.deepcopy(q)
        no_alternatives['deepseek']['available'] = False
        rec = quota.guidance(self.historical_task(), self.c, quota.view(no_alternatives))
        self.assertFalse(rec['explicit_retry_allowed'])
        self.assertTrue(rec['autonomous_next_action']['wait_for_quota'])
        self.assertNotIn('explicit requested_profile', rec['autonomous_next_action']['instruction'])
        # Recovery of the hidden monthly flag does not erase authoritative empty windows.
        quota.observe_model_success('kimi-for-coding', self.ident, time.time()-90, time.time()-80)
        self.assertEqual(quota.route(task, self.c, quota.view(q))[0], None)
        self.assertEqual(quota.route(task, self.c, self.q)[0], 'deep-research')

    def test_explicit_retry_preserves_endpoint_auth_and_rate_limit(self):
        self.open_block(time.time() - 100)
        task = {'requested_profile': 'senior-code'}
        for state, reason in (('auth_error', 'credential_rejected'),
                              ('rate_limited', 'quota_endpoint_rate_limited')):
            q = copy.deepcopy(self.q)
            q['kimi-for-coding']['state'] = state
            self.assertEqual(quota.route(task, self.c, quota.view(q)), (None, reason))

    def test_worker_success_clears_monthly_flag_end_to_end(self):
        now = time.time()
        self.open_block(now - 100)
        task = {'_billing_dispatch': {'provider': 'kimi-for-coding', 'identity': self.ident, 'at': now-50}}
        message = {'info': {'role': 'assistant', 'providerID': 'kimi-for-coding',
                    'time': {'created': (now-40)*1000, 'completed': (now-30)*1000},
                    'finish': 'stop'}, 'parts': [{'type': 'text', 'text': 'Completed'}]}
        self.assertTrue(worker.record_billing_success(task, [message]))
        self.assertEqual(self.stored_block()['released'], 'model_success')
        self.assertIsNone(quota.billing_block('kimi-for-coding'))

    # -- boundaries across months, years, leap days and DST ---------------
    def test_day_29_to_31_clamps_to_last_day(self):
        self.assertEqual(quota._boundary_epoch({'day': 31, 'time': '12:00', 'timezone': 'Asia/Shanghai'}, 2024, 2),
                         boundary_epoch(29, 12, 0, 'Asia/Shanghai', 2024, 2))
        self.assertEqual(quota._boundary_epoch({'day': 31, 'time': '12:00', 'timezone': 'Asia/Shanghai'}, 2023, 2),
                         boundary_epoch(28, 12, 0, 'Asia/Shanghai', 2023, 2))
        self.assertEqual(quota._boundary_epoch({'day': 31, 'time': '00:00', 'timezone': 'Asia/Shanghai'}, 2025, 4),
                         boundary_epoch(30, 0, 0, 'Asia/Shanghai', 2025, 4))

    def test_year_rollover_and_host_independent_zone(self):
        sched = {'day': 1, 'time': '12:00', 'timezone': 'Asia/Shanghai'}
        now = datetime(2026, 12, 20, 3, 0, tzinfo=timezone.utc).timestamp()
        recent, following = quota.monthly_boundaries(now, sched)
        self.assertEqual(recent, boundary_epoch(1, 12, 0, 'Asia/Shanghai', 2026, 12))
        self.assertEqual(following, boundary_epoch(1, 12, 0, 'Asia/Shanghai', 2027, 1))

    def test_dst_zone_offsets_are_handled_by_the_configured_zone(self):
        sched = {'day': 15, 'time': '12:00', 'timezone': 'America/New_York'}
        winter = quota._boundary_epoch(sched, 2024, 1)
        summer = quota._boundary_epoch(sched, 2024, 7)
        self.assertEqual(winter, datetime(2024, 1, 15, 17, 0, tzinfo=timezone.utc).timestamp())  # EST
        self.assertEqual(summer, datetime(2024, 7, 15, 16, 0, tzinfo=timezone.utc).timestamp())  # EDT
        # An elapsing boundary near the autumn fold is deterministic (fold=0), not host-dependent.
        fold = quota._boundary_epoch({'day': 3, 'time': '01:30', 'timezone': 'America/New_York'}, 2024, 11)
        self.assertEqual(fold, datetime(2024, 11, 3, 1, 30,
                                        tzinfo=ZoneInfo('America/New_York')).timestamp())

    # -- view, guidance and settings contract ------------------------------
    def test_view_exposes_monthly_reset_for_ui(self):
        view = quota.view(common.read_json(self.state / 'quota.json', {}))
        self.assertEqual(view, {})
        common.write_json(self.state / 'quota.json', {'kimi-for-coding': {
            'state': 'ok', 'sampled_at': time.time(), 'available': True, 'windows': [], 'balances': []}})
        disabled = quota.view(common.read_json(self.state / 'quota.json', {}))['kimi-for-coding']['monthly_reset']
        self.assertEqual(disabled['enabled'], False)
        self.assertEqual(disabled['day'], 1)
        self.assertEqual(disabled['time'], '12:00')
        self.assertEqual(disabled['timezone'], 'Asia/Shanghai')
        self.assertIsNone(disabled['next_reset_at'])
        self.schedule(enabled=True, day=19)
        enabled = quota.view(common.read_json(self.state / 'quota.json', {}))['kimi-for-coding']['monthly_reset']
        self.assertEqual(enabled['enabled'], True)
        self.assertEqual(enabled['day'], 19)
        parsed = datetime.fromisoformat(enabled['next_reset_at'])
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertGreater(parsed.timestamp(), time.time())
        self.assertEqual([p for p in quota.view({'deepseek': {'state': 'ok'}}) if p == 'deepseek'], ['deepseek'])

    def test_guidance_honors_scheduled_and_success_releases(self):
        t = self.historical_task()
        rec = quota.guidance(t, self.c, self.q)
        self.assertEqual(rec['billing_reason'], 'monthly_usage_limit')
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])
        # A recorded scheduled release is recognized historical recovery.
        self.schedule()
        boundary = self.recent_boundary()
        self.open_block(boundary - 30)
        self.assertTrue(quota.scheduled_release('kimi-for-coding'))
        rec = quota.guidance(t, self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']],
                         ['fast-code', 'senior-code', 'deep-research'])
        # A verified model success is recognized the same way.
        self.open_block(time.time() - 30)
        quota.observe_model_success('kimi-for-coding', self.ident, time.time() - 20, time.time() - 10)
        rec = quota.guidance(t, self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']],
                         ['fast-code', 'senior-code', 'deep-research'])
        # An unrelated balance release is not proof for monthly evidence.
        quota.trip('kimi-for-coding', 'Insufficient Balance', 'm-bal', reason='insufficient_balance')
        quota.clear('kimi-for-coding', evidence='quota')
        rec = quota.guidance(t, self.c, self.q)
        self.assertEqual([a['profile'] for a in rec['alternatives']], ['fast-code'])


if __name__ == '__main__':
    unittest.main()
