"""Ordered fallback and weighted K3 admission routing, plus 5-hour window recovery."""
import itertools
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import common
import delegate
import diagnostics
import management
import quota
import routing
import worker

WINDOW_5H = ("You've reached your 5-hour usage limit for this usage window. "
             "Your quota will be refreshed in the next cycle of the window.")
MONTHLY_MESSAGE = ("You've reached your monthly usage limit for this billing cycle. "
                   "Your quota will be refreshed in the next cycle. To continue now, purchase extra "
                   "usage or upgrade your plan.")
COUNTERS = 'routing.json'

BACKGROUND_POLICY = [
    [{'profile': 'senior-code', 'weight': 1}],
    [{'profile': 'ark-auto', 'weight': 1}],
    [{'profile': 'fallback', 'weight': 1}],
]
DEEP_POLICY = [
    [{'profile': 'deep-research', 'weight': 2}, {'profile': 'ark-k3', 'weight': 1}],
    [{'profile': 'fallback', 'weight': 1}],
]


def profiles():
    return {
        'fallback': {'model': 'deepseek/deepseek-flash', 'enabled': True},
        'senior-code': {'model': 'kimi-for-coding/kimi-for-coding', 'enabled': True},
        'deep-research': {'model': 'kimi-for-coding/k3', 'enabled': True},
        'ark-auto': {'model': 'ark/ark-code-latest', 'enabled': True, 'variant': 'max'},
        'ark-k3': {'model': 'ark/kimi-k3', 'enabled': True, 'variant': 'max'},
        'disabled': {'model': 'ark/other', 'enabled': False},
    }


class RoutingPolicyTests(unittest.TestCase):
    """Strict settings validation and fail-soft dispatch reads."""

    def test_valid_policy_round_trips_in_order(self):
        value = {'background': BACKGROUND_POLICY, 'deep': DEEP_POLICY}
        self.assertEqual(routing.validate_policy(value, profiles()), value)
        self.assertEqual(routing.normalize_policy(value, profiles()), value)

    def test_subset_of_tiers_is_valid(self):
        value = {'deep': [[{'profile': 'ark-k3', 'weight': 3}]]}
        self.assertEqual(routing.validate_policy(value, profiles()), value)

    def test_adaptive_ladder_is_explicit_generic_and_bounded(self):
        policy = {'deep': [[{'profile': 'deep-research', 'weight': 2},
                            {'profile': 'ark-k3', 'weight': 1}],
                           [{'profile': 'fallback', 'weight': 1}]]}
        value = {'deep': {'0': {'ladder': [[3, 1], [2, 1], [1, 1]]}}}
        self.assertEqual(routing.validate_dynamics(value, policy, profiles()), value)
        config = {'profiles': profiles(), 'routing_policy': policy, 'routing_dynamics': value}
        self.assertEqual(routing.dynamic_stage(config, 'deep', 0, policy), value['deep']['0'])
        self.assertIsNone(routing.dynamic_stage(config, 'deep', 1, policy))

    def test_adaptive_ladder_rejects_implicit_or_unsafe_shapes(self):
        policy = {'deep': [[{'profile': 'deep-research', 'weight': 2},
                            {'profile': 'ark-k3', 'weight': 1}]]}
        cases = [
            {'deep': {'1': {'ladder': [[2, 1], [1, 1]]}}},
            {'deep': {'0': {'ladder': [[3, 1], [1, 1]]}}},  # baseline missing
            {'deep': {'0': {'ladder': [[1, 1], [2, 1]]}}},  # wrong order
            {'deep': {'0': {'ladder': [[4, 2], [2, 1]]}}},  # duplicate reduced ratio
            {'deep': {'0': {'ladder': [[2, 1]]}}},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                routing.validate_dynamics(value, policy, profiles())

    def test_malformed_policies_are_rejected_and_fail_closed(self):
        cases = [
            ('none', None), ('empty', {}), ('list', []), ('text', 'deep'),
            ('unknown tier', {'urgent': [[{'profile': 'ark-k3', 'weight': 1}]]}),
            ('empty stages', {'deep': []}),
            ('empty stage', {'deep': [[]]}),
            ('non-list stage', {'deep': {'profile': 'ark-k3', 'weight': 1}}),
            ('non-object entry', {'deep': [['ark-k3']]}),
            ('missing profile', {'deep': [[{'weight': 1}]]}),
            ('unknown profile', {'deep': [[{'profile': 'ghost', 'weight': 1}]]}),
            ('disabled profile', {'deep': [[{'profile': 'disabled', 'weight': 1}]]}),
            ('missing weight', {'deep': [[{'profile': 'ark-k3'}]]}),
            ('zero weight', {'deep': [[{'profile': 'ark-k3', 'weight': 0}]]}),
            ('bool weight', {'deep': [[{'profile': 'ark-k3', 'weight': True}]]}),
            ('text weight', {'deep': [[{'profile': 'ark-k3', 'weight': '2'}]]}),
            ('weight over limit', {'deep': [[{'profile': 'ark-k3', 'weight': routing.MAX_WEIGHT + 1}]]}),
            ('duplicate profile', {'deep': [[{'profile': 'ark-k3', 'weight': 1}],
                                            [{'profile': 'ark-k3', 'weight': 1}]]}),
        ]
        for name, value in cases:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    routing.validate_policy(value, profiles())
                self.assertIsNone(routing.normalize_policy(value, profiles()))

    def test_stage_and_entry_limits_are_enforced(self):
        entry = [[{'profile': 'ark-k3', 'weight': 1}]]
        too_many_stages = {'deep': entry * (routing.MAX_STAGES + 1)}
        with self.assertRaises(ValueError):
            routing.validate_policy(too_many_stages, profiles())
        too_many_entries = {'deep': [[{'profile': 'ark-k3', 'weight': 1}] * (routing.MAX_STAGE_ENTRIES + 1)]}
        with self.assertRaises(ValueError):
            routing.validate_policy(too_many_entries, profiles())

    def test_runtime_policy_drops_degraded_references_but_keeps_order(self):
        stored = {'background': [[{'profile': 'ghost', 'weight': 1},
                                  {'profile': 'fallback', 'weight': 1}],
                                 [{'profile': 'disabled', 'weight': 1}],
                                 [{'profile': 'ark-auto', 'weight': 1}]],
                  'deep': [[{'profile': 'ark-k3', 'weight': 1}]]}
        runtime = routing.runtime_policy(stored, profiles())
        self.assertEqual(runtime['background'], [[{'profile': 'fallback', 'weight': 1}],
                                                 [{'profile': 'ark-auto', 'weight': 1}]])
        self.assertEqual(runtime['deep'], [[{'profile': 'ark-k3', 'weight': 1}]])
        # A structurally malformed policy still routes as if unset.
        self.assertIsNone(routing.runtime_policy({'deep': []}, profiles()))
        self.assertIsNone(routing.runtime_policy(
            {'deep': [[{'profile': 'ark-k3', 'weight': 0}]]}, profiles()))


class WeightedAdmissionTests(unittest.TestCase):
    """Deterministic 2:1 advance, bounded credits, restart durability and no debt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        common.init()
        self.patcher = patch.object(routing, 'STATE', self.state)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_advance_distributes_two_to_one_in_policy_order(self):
        entries = [{'profile': 'a', 'weight': 2}, {'profile': 'b', 'weight': 1}]
        credits, sequence = {}, []
        for _ in range(6):
            chosen, credits = routing.advance(entries, credits)
            sequence.append(chosen)
        self.assertEqual(sequence, ['a', 'b', 'a', 'a', 'b', 'a'])
        self.assertEqual(sequence.count('a'), 4)
        self.assertEqual(sequence.count('b'), 2)

    def test_uniform_weights_rotate_in_policy_order(self):
        entries = [{'profile': name, 'weight': 1} for name in ('a', 'b', 'c')]
        credits, sequence = {}, []
        for _ in range(6):
            chosen, credits = routing.advance(entries, credits)
            sequence.append(chosen)
        self.assertEqual(sequence, ['a', 'b', 'c', 'a', 'b', 'c'])

    def test_outage_never_accrues_catch_up_debt(self):
        policy = routing.validate_policy({'deep': DEEP_POLICY}, profiles())
        stage0 = policy['deep'][0]
        admissions = routing.Admissions(policy)
        available = [entry for entry in stage0 if entry['profile'] != 'deep-research']
        for _ in range(50):
            chosen, credits = routing.advance(available, admissions.credits_for('deep'))
            self.assertEqual(chosen, 'ark-k3')
            admissions.credits['deep'].update(credits)
        # The single remaining candidate paid its own weight each time: no debt.
        self.assertEqual(admissions.credits['deep'].get('ark-k3', 0), 0)
        self.assertNotIn('fallback', admissions.credits.get('deep', {}))
        # The provider returns and immediately gets its configured 2:1 share.
        sequence = []
        for _ in range(6):
            chosen, credits = routing.advance(stage0, admissions.credits_for('deep'))
            admissions.credits['deep'].update(credits)
            sequence.append(chosen)
        self.assertEqual(sequence, ['deep-research', 'ark-k3', 'deep-research',
                                    'deep-research', 'ark-k3', 'deep-research'])

    def test_credits_stay_bounded_and_survive_restart(self):
        policy = routing.validate_policy({'deep': DEEP_POLICY}, profiles())
        stage0 = policy['deep'][0]
        admissions = routing.Admissions(policy)
        sequence = []
        for _ in range(6):
            chosen, credits = routing.advance(stage0, admissions.credits_for('deep'))
            admissions.propose('deep', credits)
            admissions.commit()
            sequence.append(chosen)
        self.assertEqual(sequence, ['deep-research', 'ark-k3', 'deep-research',
                                    'deep-research', 'ark-k3', 'deep-research'])
        self.assertTrue(admissions.save())
        stored = json.loads((self.state / COUNTERS).read_text())
        self.assertEqual(stored['version'], 1)
        for value in stored['credits']['deep'].values():
            self.assertLess(abs(value), 10)
        # A fresh process loads the same bounded credits and keeps the same cadence.
        restarted = routing.Admissions.load(policy)
        for _ in range(994):
            chosen, credits = routing.advance(stage0, restarted.credits_for('deep'))
            restarted.propose('deep', credits)
            restarted.commit()
        for value in restarted.credits['deep'].values():
            self.assertLess(abs(value), 10)
        restarted.save()
        stored = json.loads((self.state / COUNTERS).read_text())
        for value in stored['credits']['deep'].values():
            self.assertLess(abs(value), 10)

    def test_discarded_proposal_never_moves_credits(self):
        policy = routing.validate_policy({'deep': DEEP_POLICY}, profiles())
        admissions = routing.Admissions(policy)
        chosen, credits = routing.advance(policy['deep'][0], admissions.credits_for('deep'))
        admissions.propose('deep', credits)
        admissions.discard()
        self.assertEqual(admissions.credits.get('deep', {}), {})
        self.assertFalse(admissions.save())
        self.assertFalse((self.state / COUNTERS).exists())

    def test_save_prunes_profiles_no_longer_policy_routed(self):
        policy = routing.validate_policy({'deep': DEEP_POLICY}, profiles())
        admissions = routing.Admissions(policy, {'deep': {'ark-k3': 1, 'ghost': 5}})
        admissions.dirty = True
        admissions.save()
        stored = json.loads((self.state / COUNTERS).read_text())
        self.assertEqual(stored['credits'], {'deep': {'ark-k3': 1}})


class RoutePolicyTests(unittest.TestCase):
    """quota.route policy behavior: stages, weights, pins, previews and legacy fallback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.task_ids = itertools.count(1)
        self.c = {
            'auto_approve': False, 'kimi_reserve_percent': 20,
            'profiles': profiles(),
            'routing': {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'},
            'routing_policy': {'background': BACKGROUND_POLICY, 'deep': DEEP_POLICY},
        }
        self.config.write_text(json.dumps(self.c))
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(quota, 'STATE', self.state),
                         patch.object(delegate, 'STATE', self.state), patch.object(routing, 'STATE', self.state),
                         patch.object(worker, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config), patch.object(delegate, 'CONFIG', self.config),
                         patch.object(quota, 'credential_identity', lambda p: 'cred-' + str(p))]
        for p in self.patchers:
            p.start()
        common.init()
        self.q = {p: {'state': 'ok', 'stale': False, 'sampled_at': time.time(), 'available': True,
                      'windows': [{'name': 'window_0', 'remaining_percent': 80.0}]}
                  for p in ('deepseek', 'kimi-for-coding', 'ark')}

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def task(self, **kw):
        t = {'id': 'job-%d' % next(self.task_ids), 'title': 'routing task', 'status': 'queued',
             'mode': 'read', 'workspace': 'shared', 'source_dir': str(self.root), 'scopes': [],
             'targets': [], 'resources': [], 'requested_profile': 'auto', 'urgency': 'background',
             'complexity': 'normal', 'created_at': time.time()}
        t.update(kw)
        return t

    def test_first_stage_with_available_candidate_wins(self):
        profile, why = quota.route(self.task(), self.c, self.q)
        self.assertEqual(profile, 'senior-code')
        self.assertTrue(why.startswith('routing_policy:background:stage0:'))
        self.assertEqual(quota.route(self.task(urgency='fast'), self.c, self.q)[0], 'fallback')

    def test_stage_falls_through_when_preferred_provider_is_unavailable(self):
        self.q['kimi-for-coding']['available'] = False
        profile, why = quota.route(self.task(), self.c, self.q)
        self.assertEqual(profile, 'ark-auto')
        self.assertTrue(why.startswith('routing_policy:background:stage1:'))
        self.q['ark']['available'] = False
        profile, why = quota.route(self.task(), self.c, self.q)
        self.assertEqual(profile, 'fallback')
        self.assertTrue(why.startswith('routing_policy:background:stage2:'))

    def test_deep_stage_advances_by_weight(self):
        entries = routing.configured(self.c)['deep'][0]
        chosen, credits = routing.advance(entries, {})
        self.assertEqual(chosen, 'deep-research')
        chosen, credits = routing.advance(entries, credits)
        self.assertEqual(chosen, 'ark-k3')
        chosen, credits = routing.advance(entries, credits)
        self.assertEqual(chosen, 'deep-research')

    def test_route_preview_never_consumes_credits(self):
        t = self.task(complexity='deep')
        first = quota.route(t, self.c, self.q)[0]
        second = quota.route(t, self.c, self.q)[0]
        self.assertEqual(first, second)
        self.assertFalse((self.state / COUNTERS).exists())

    def test_route_uses_persisted_credits_without_mutating_them(self):
        policy = routing.configured(self.c)
        admissions = routing.Admissions(policy)
        _, credits = routing.advance(policy['deep'][0], {})
        admissions.propose('deep', credits)
        admissions.commit()
        admissions.save()
        before = (self.state / COUNTERS).read_bytes()
        t = self.task(complexity='deep')
        # deep-research already took its turn: the next preview is K3.
        self.assertEqual(quota.route(t, self.c, self.q)[0], 'ark-k3')
        self.assertEqual((self.state / COUNTERS).read_bytes(), before)

    def test_explicit_profile_pins_and_retains_monthly_retry(self):
        quota.trip('kimi-for-coding', MONTHLY_MESSAGE, 'm-monthly', reason=quota.MONTHLY_REASON)
        explicit = self.task(requested_profile='senior-code')
        self.assertEqual(quota.route(explicit, self.c, self.q),
                         ('senior-code', 'explicit_profile_monthly_retry'))
        auto = self.task()
        profile, why = quota.route(auto, self.c, self.q)
        self.assertEqual(profile, 'ark-auto')  # the blocked preferred stage is skipped
        self.assertTrue(why.startswith('routing_policy:background:stage1:'))

    def test_existing_circuits_still_block_policy_candidates(self):
        quota.trip('kimi-for-coding', 'Insufficient Balance', 'm-balance', reason=quota.BALANCE_REASON)
        quota.trip('ark', 'Insufficient Balance', 'm-ark', reason=quota.BALANCE_REASON)
        self.q['deepseek']['available'] = False  # every stage is now blocked
        profile, why = quota.route(self.task(complexity='deep'), self.c, self.q)
        self.assertIsNone(profile)
        self.assertEqual(why, 'routing_policy:provider_billing_blocked')
        # An explicit pin still reports the circuit instead of silently switching.
        explicit = self.task(requested_profile='senior-code')
        self.assertEqual(quota.route(explicit, self.c, self.q), (None, 'provider_billing_blocked'))

    def test_omitted_policy_preserves_legacy_routing(self):
        c = dict(self.c)
        c.pop('routing_policy')
        self.assertEqual(quota.route(self.task(), c, self.q), ('senior-code', 'background:quota_available'))
        self.assertEqual(quota.route(self.task(urgency='fast'), c, self.q),
                         ('fallback', 'fast:quota_available'))
        self.assertEqual(quota.route(self.task(complexity='deep'), c, self.q),
                         ('deep-research', 'deep:quota_available'))

    def test_malformed_stored_policy_falls_back_to_legacy(self):
        c = dict(self.c, routing_policy={'background': [], 'deep': [[{'profile': 'ghost', 'weight': 1}]]})
        self.assertEqual(quota.route(self.task(), c, self.q)[0], 'senior-code')
        self.assertEqual(quota.route(self.task(complexity='deep'), c, self.q)[0], 'deep-research')

    def test_degraded_policy_reference_skips_only_that_candidate(self):
        c = json.loads(json.dumps(self.c))
        del c['profiles']['senior-code']
        self.assertEqual(quota.route(self.task(), c, self.q)[0], 'ark-auto')

    def test_recovery_alternatives_follow_policy_order(self):
        t = self.task(complexity='deep', requested_profile='deep-research')
        alts = quota.alternatives(t, self.c, self.q, exclude='kimi-for-coding')
        # Deep tier first (Ark K3, then its DeepSeek fallback), then the background tier.
        self.assertEqual([a['profile'] for a in alts],
                         ['ark-k3', 'fallback'])

    def test_window_recovery_prefers_policy_order_and_excludes_provider(self):
        t = self.task(requested_profile='deep-research', complexity='deep')
        rec = quota.guidance(dict(t, status='failed', reason='provider_usage_window_limit',
                                  errors=[{'source': 'model', 'message': WINDOW_5H,
                                           'usage_window': True, 'usage_window_reason': quota.WINDOW_REASON}]),
                             self.c, self.q)
        self.assertTrue(rec['usage_window_exhausted'])
        self.assertIsNone(rec['billing_reason'])
        self.assertEqual(rec['blocked_provider'], 'kimi-for-coding')
        self.assertEqual([a['profile'] for a in rec['alternatives']],
                         ['ark-k3', 'fallback'])
        self.assertEqual((rec['autonomous_next_action']['tier'],
                          rec['autonomous_next_action']['profile']), ('deep', 'auto'))
        self.assertFalse(rec['autonomous_next_action']['wait_for_quota'])
        # Read-only: no durable circuit was armed for a self-resolving window.
        self.assertIsNone(quota.billing_block('kimi-for-coding'))

    def test_window_recovery_without_alternatives_never_blocks_permanently(self):
        c = json.loads(json.dumps(self.c))
        for name in ('fallback', 'ark-auto', 'ark-k3'):
            c['profiles'][name]['enabled'] = False
        t = dict(self.task(requested_profile='deep-research', complexity='deep'), status='failed',
                 reason='provider_usage_window_limit', errors=[])
        rec = quota.guidance(t, c, self.q)
        self.assertEqual(rec['alternatives'], [])
        self.assertEqual(rec['suggested_action'], 'retry_or_reselect_after_usage_window')
        self.assertFalse(rec['autonomous_next_action']['wait_for_quota'])
        for invented in ('resets_at', 'next_reset_at', 'retry_after'):
            self.assertNotIn(invented, json.dumps(rec))


class ChooseReadyAdmissionTests(RoutePolicyTests):
    """Credits advance only for real admissions, in correct within-batch order."""

    def active(self, owner, scope):
        return dict(self.task(), id='job-active-' + scope, status='running', mode='write',
                    owner_thread_id=owner, group_id=owner, scopes=[scope], profile='senior-code')

    def batch(self, c=None):
        c = c if c is not None else self.c
        return routing.Admissions.load(routing.configured(c))

    def admit(self, queued, c=None, q=None, admissions=None):
        c = c if c is not None else self.c
        q = q if q is not None else self.q
        admissions = admissions if admissions is not None else self.batch(c)
        choices = delegate.choose_ready(queued, c, q, admissions)
        admissions.save()
        return choices

    def stored_credits(self):
        raw = common.read_json(self.state / COUNTERS, {})
        return raw.get('credits', {})

    def test_same_batch_distributes_by_weight(self):
        queued = [self.task(owner_thread_id='t%d' % i, group_id='t%d' % i, scopes=['f%d' % i],
                            complexity='deep') for i in range(6)]
        choices = self.admit(queued)
        self.assertEqual([c[1] for c in choices],
                         ['deep-research', 'ark-k3', 'deep-research',
                          'deep-research', 'ark-k3', 'deep-research'])
        for choice in choices:
            self.assertTrue(choice[2].startswith('routing_policy:deep:stage0:'))
        for value in self.stored_credits()['deep'].values():
            self.assertLess(abs(value), 10)

    def test_admissions_survive_restart_and_keep_cadence(self):
        sequence = []
        for i in range(6):
            queued = [self.task(owner_thread_id='t%d' % i, group_id='t%d' % i, scopes=['f%d' % i],
                                complexity='deep')]
            sequence.append(self.admit(queued)[0][1])
        self.assertEqual(sequence, ['deep-research', 'ark-k3', 'deep-research',
                                    'deep-research', 'ark-k3', 'deep-research'])

    def test_long_outage_then_recovery_does_not_flood_returning_provider(self):
        self.q['kimi-for-coding']['available'] = False
        for i in range(50):
            queued = [self.task(owner_thread_id='t%d' % i, group_id='t%d' % i, scopes=['f%d' % i],
                                complexity='deep')]
            self.assertEqual(self.admit(queued)[0][1], 'ark-k3')
        self.assertEqual(self.stored_credits()['deep'].get('ark-k3', 0), 0)
        self.q['kimi-for-coding']['available'] = True
        sequence = []
        for i in range(6):
            queued = [self.task(owner_thread_id='r%d' % i, group_id='r%d' % i, scopes=['r%d' % i],
                                complexity='deep')]
            sequence.append(self.admit(queued)[0][1])
        self.assertEqual(sequence, ['deep-research', 'ark-k3', 'deep-research',
                                    'deep-research', 'ark-k3', 'deep-research'])

    def test_separate_stage_fallback_keeps_independent_credits(self):
        self.q['kimi-for-coding']['available'] = False
        self.q['ark']['available'] = False
        for i in range(20):
            queued = [self.task(owner_thread_id='t%d' % i, group_id='t%d' % i, scopes=['f%d' % i],
                                complexity='deep')]
            self.assertEqual(self.admit(queued)[0][1], 'fallback')
        self.assertEqual(self.stored_credits()['deep'].get('fallback', 0), 0)
        self.assertEqual(self.stored_credits()['deep'].get('deep-research', 0), 0)
        self.q['kimi-for-coding']['available'] = True
        self.q['ark']['available'] = True
        sequence = []
        for i in range(6):
            queued = [self.task(owner_thread_id='r%d' % i, group_id='r%d' % i, scopes=['r%d' % i],
                                complexity='deep')]
            sequence.append(self.admit(queued)[0][1])
        self.assertEqual(sequence, ['deep-research', 'ark-k3', 'deep-research',
                                    'deep-research', 'ark-k3', 'deep-research'])

    def test_read_only_callers_never_advance_or_write_credits(self):
        queued = [self.task(complexity='deep')]
        first = delegate.choose_ready(queued, self.c, self.q)
        second = delegate.choose_ready(queued, self.c, self.q)
        self.assertEqual(first[0][1], 'deep-research')
        self.assertEqual(second[0][1], 'deep-research')
        self.assertFalse((self.state / COUNTERS).exists())

    def test_explicit_pin_interleaved_with_auto_never_consumes(self):
        admissions = self.batch()
        queued = [self.task(requested_profile='deep-research', complexity='deep'),
                  self.task(complexity='deep'),
                  self.task(requested_profile='ark-k3', complexity='deep'),
                  self.task(complexity='deep')]
        choices = delegate.choose_ready(queued, self.c, self.q, admissions)
        self.assertEqual([c[1] for c in choices],
                         ['deep-research', 'deep-research', 'ark-k3', 'ark-k3'])
        admissions.save()
        self.assertEqual(self.stored_credits()['deep'],
                         {'deep-research': 1, 'ark-k3': -1})

    def test_owner_cap_blocks_do_not_consume_credits(self):
        actives = [self.active('thread-a', 'a%d' % i) for i in range(4)]
        queued = self.task(owner_thread_id='thread-a', group_id='thread-a', scopes=['x'])
        choices = self.admit(actives + [queued])
        self.assertEqual(choices[-1][2], 'owner_at_capacity')
        self.assertEqual(self.stored_credits(), {})

    def test_scope_conflict_blocks_do_not_consume_credits(self):
        active = self.active('thread-a', 'shared')
        queued = self.task(owner_thread_id='thread-b', group_id='thread-b', mode='write', scopes=['shared'])
        choices = self.admit([active, queued])
        self.assertEqual(choices[0][2], 'scope_or_resource_in_use')
        self.assertEqual(self.stored_credits(), {})

    def test_blocked_task_does_not_shift_the_batch_distribution(self):
        # Seed the deep stage so the next policy pick is Ark K3; a scope-blocked task
        # must consume nothing, so the first admitted task still receives Ark K3.
        admissions = self.batch()
        _, credits = routing.advance(routing.configured(self.c)['deep'][0], {})
        admissions.propose('deep', credits)
        admissions.commit()
        active = self.active('thread-a', 'shared')
        blocked = self.task(owner_thread_id='thread-b', group_id='thread-b', mode='write',
                            scopes=['shared'], complexity='deep')
        admitted = self.task(owner_thread_id='thread-c', group_id='thread-c', scopes=['free'],
                             complexity='deep')
        choices = delegate.choose_ready([active, blocked, admitted], self.c, self.q, admissions)
        self.assertEqual(len(choices), 2)
        self.assertEqual((choices[0][1], choices[0][2]), (None, 'scope_or_resource_in_use'))
        self.assertEqual(choices[1][1], 'ark-k3')
        self.assertTrue(choices[1][2].startswith('routing_policy:deep:stage0:'))

    def test_quota_blocked_tasks_do_not_consume_credits(self):
        for provider in ('kimi-for-coding', 'ark', 'deepseek'):
            self.q[provider]['available'] = False
        choices = self.admit([self.task(complexity='deep')])
        self.assertIsNone(choices[0][1])
        self.assertEqual(self.stored_credits(), {})

    def test_admitted_work_is_pinned_and_never_reconsidered(self):
        queued = [self.task(complexity='deep')]
        admitted = self.admit(queued)[0]
        self.assertEqual(admitted[1], 'deep-research')
        starting = dict(queued[0], profile=admitted[1], status='starting')
        c = dict(self.c, routing_policy={'deep': [[{'profile': 'ark-k3', 'weight': 1}]]})
        self.assertEqual(delegate.choose_ready([starting], c, self.q), [])
        self.assertEqual(starting['profile'], 'deep-research')

    def test_no_policy_writes_no_counter_file(self):
        c = dict(self.c)
        c.pop('routing_policy')
        queued = [self.task(owner_thread_id='thread-a', group_id='thread-a', scopes=['a'])]
        choices = delegate.choose_ready(queued, c, self.q, routing.Admissions.load(None))
        self.assertEqual(choices[0][1], 'senior-code')
        self.assertFalse((self.state / COUNTERS).exists())


class ManagementPolicyTests(unittest.TestCase):
    """Opt-in policy exposure, preservation for old clients and strict rejection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        base = {'version': 1, 'server_url': 'http://127.0.0.1:1', 'max_parallel_per_owner': 4,
                'kimi_reserve_percent': 20, 'profiles': profiles(),
                'routing': {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'}}
        self.config.write_text(json.dumps(base))
        self.patchers = [patch.object(common, 'STATE', self.root / 'state'),
                         patch.object(common, 'CONFIG', self.config)]
        for p in self.patchers:
            p.start()
        common.init()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def body(self):
        return {'profiles': profiles(),
                'max_parallel_per_owner': 4, 'kimi_reserve_percent': 20,
                'routing': {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'}}

    def stored(self):
        return json.loads(self.config.read_text())

    def test_policy_saves_exports_and_round_trips(self):
        body = self.body()
        body['routing_policy'] = {'background': BACKGROUND_POLICY, 'deep': DEEP_POLICY}
        with patch.object(common, 'api', return_value={}):
            result = management.save_settings(body)
        self.assertEqual(result['routing_policy'], body['routing_policy'])
        self.assertEqual(self.stored()['routing_policy'], body['routing_policy'])
        self.assertEqual(management.settings()['routing_policy'], body['routing_policy'])

    def test_policy_dynamics_save_export_and_round_trip(self):
        body = self.body()
        body['routing_policy'] = {'deep': DEEP_POLICY}
        body['routing_dynamics'] = {
            'deep': {'0': {'ladder': [[3, 1], [2, 1], [1, 1]]}}}
        with patch.object(common, 'api', return_value={}):
            result = management.save_settings(body)
        self.assertEqual(result['routing_dynamics'], body['routing_dynamics'])
        self.assertEqual(self.stored()['routing_dynamics'], body['routing_dynamics'])
        self.assertEqual(management.settings()['routing_dynamics'], body['routing_dynamics'])

    def test_old_client_preserves_valid_dynamics(self):
        body = self.body()
        body['routing_policy'] = {'deep': DEEP_POLICY}
        body['routing_dynamics'] = {
            'deep': {'0': {'ladder': [[3, 1], [2, 1], [1, 1]]}}}
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
            result = management.save_settings(self.body())
        self.assertEqual(result['routing_dynamics'], body['routing_dynamics'])

    def test_clearing_policy_also_clears_dynamics(self):
        body = self.body()
        body['routing_policy'] = {'deep': DEEP_POLICY}
        body['routing_dynamics'] = {
            'deep': {'0': {'ladder': [[3, 1], [2, 1], [1, 1]]}}}
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
            clear = self.body()
            clear['routing_policy'] = {}
            result = management.save_settings(clear)
        self.assertNotIn('routing_policy', result)
        self.assertNotIn('routing_dynamics', result)
        self.assertNotIn('routing_dynamics', self.stored())

    def test_invalid_dynamics_are_rejected_without_writing(self):
        original = self.config.read_text()
        body = self.body()
        body['routing_policy'] = {'deep': DEEP_POLICY}
        body['routing_dynamics'] = {
            'deep': {'0': {'ladder': [[4, 1], [3, 1], [1, 1]]}}}
        with patch.object(common, 'api') as api:
            with self.assertRaises(ValueError):
                management.save_settings(body)
            api.assert_not_called()
        self.assertEqual(self.config.read_text(), original)

    def test_old_client_body_preserves_stored_policy(self):
        body = self.body()
        body['routing_policy'] = {'deep': DEEP_POLICY}
        with patch.object(common, 'api', return_value={}):
            management.save_settings(body)
        self.assertNotIn('routing_policy', self.body())
        with patch.object(common, 'api', return_value={}):
            management.save_settings(self.body())
        self.assertEqual(self.stored()['routing_policy'], {'deep': DEEP_POLICY})
        self.assertEqual(management.settings()['routing_policy'], {'deep': DEEP_POLICY})

    def test_null_or_empty_object_clears_policy(self):
        for clear_value in (None, {}):
            with self.subTest(value=clear_value):
                body = self.body()
                body['routing_policy'] = {'deep': DEEP_POLICY}
                with patch.object(common, 'api', return_value={}):
                    management.save_settings(body)
                clear = self.body()
                clear['routing_policy'] = clear_value
                with patch.object(common, 'api', return_value={}):
                    result = management.save_settings(clear)
                self.assertNotIn('routing_policy', self.stored())
                self.assertNotIn('routing_policy', result)

    def test_invalid_policy_rejected_without_writing(self):
        original = self.config.read_text()
        cases = {
            'unknown profile': {'deep': [[{'profile': 'ghost', 'weight': 1}]]},
            'disabled profile': {'deep': [[{'profile': 'disabled', 'weight': 1}]]},
            'zero weight': {'deep': [[{'profile': 'ark-k3', 'weight': 0}]]},
            'text weight': {'deep': [[{'profile': 'ark-k3', 'weight': '1'}]]},
            'empty stage': {'deep': [[]]},
            'unknown tier': {'urgent': [[{'profile': 'ark-k3', 'weight': 1}]]},
            'duplicate profile': {'deep': [[{'profile': 'ark-k3', 'weight': 1}],
                                           [{'profile': 'ark-k3', 'weight': 1}]]},
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                body = self.body()
                body['routing_policy'] = value
                with patch.object(common, 'api') as api:
                    with self.assertRaises(ValueError):
                        management.save_settings(body)
                    api.assert_not_called()
                self.assertEqual(self.config.read_text(), original)

    def test_settings_shows_effective_policy_when_reference_degrades(self):
        config = self.stored()
        config['routing_policy'] = {'deep': [[{'profile': 'ark-k3', 'weight': 1}],
                                             [{'profile': 'disabled', 'weight': 1}]]}
        self.config.write_text(json.dumps(config))
        self.assertEqual(management.settings()['routing_policy'],
                         {'deep': [[{'profile': 'ark-k3', 'weight': 1}]]})

    def test_omitted_policy_with_broken_reference_is_rejected(self):
        original = self.config.read_text()
        body = self.body()
        body['routing_policy'] = {'deep': [[{'profile': 'disabled', 'weight': 1}]]}
        # The strict API cannot even store that policy, so write the broken record directly.
        config = self.stored()
        config['routing_policy'] = {'deep': [[{'profile': 'disabled', 'weight': 1}]]}
        self.config.write_text(json.dumps(config))
        original = self.config.read_text()
        with patch.object(common, 'api', return_value={}):
            with self.assertRaises(ValueError):
                management.save_settings(self.body())
        self.assertEqual(self.config.read_text(), original)

    def test_settings_omits_malformed_stored_policy(self):
        config = self.stored()
        config['routing_policy'] = {'deep': []}
        self.config.write_text(json.dumps(config))
        self.assertNotIn('routing_policy', management.settings())


class WindowClassificationTests(unittest.TestCase):
    """Explicit windows and model 429s are capacity stops, distinct from billing."""

    def test_explicit_5h_window_classifies(self):
        for message in ["You've reached your 5-hour usage limit for this window.",
                        'Five-hour usage quota exhausted.',
                        'Hourly usage limit reached, please retry later.',
                        'weekly usage limit exceeded']:
            self.assertEqual(diagnostics.usage_window_kind(403, message),
                             diagnostics.WINDOW_REASON, message)
        raw = {'name': 'APIError', 'data': {'statusCode': 403, 'message': WINDOW_5H}}
        self.assertEqual(diagnostics.window_kind(raw), diagnostics.WINDOW_REASON)
        self.assertIsNone(diagnostics.billing_kind(raw))

    def test_monthly_never_classifies_as_window(self):
        self.assertIsNone(diagnostics.usage_window_kind(403, MONTHLY_MESSAGE))
        self.assertEqual(diagnostics.billing_kind({'data': {'statusCode': 403, 'message': MONTHLY_MESSAGE}}),
                         diagnostics.MONTHLY_REASON)

    def test_429_is_capacity_stop_while_auth_and_ambiguous_text_are_preserved(self):
        self.assertEqual(diagnostics.usage_window_kind(429, 'Rate limit exceeded'),
                         diagnostics.WINDOW_REASON)
        self.assertIsNone(diagnostics.usage_window_kind(401, WINDOW_5H))
        for message in ['Rate limit exceeded, retry later',
                        'Rate limit exceeded, retry in 5 hours',
                        'You do not have permission to view 5-hour quota',
                        'Your 5-hour quota is 100 requests',
                        '5-hour window status unavailable']:
            self.assertIsNone(diagnostics.usage_window_kind(403, message), message)

    def test_from_messages_marks_window_without_billing(self):
        message = {'info': {'id': 'msg_window', 'role': 'assistant', 'providerID': 'kimi-for-coding',
                            'time': {'created': 1700000000000, 'completed': 1700000010000},
                            'error': {'name': 'APIError', 'data': {'message': WINDOW_5H,
                                                                   'statusCode': 403,
                                                                   'isRetryable': True}}},
                   'parts': []}
        error = diagnostics.from_messages([message])[0]
        self.assertTrue(error['usage_window'])
        self.assertEqual(error['usage_window_reason'], diagnostics.WINDOW_REASON)
        self.assertNotIn('billing', error)
        self.assertFalse(error['retryable'])
        self.assertEqual(error['suggested_action'], 'inspect_partial_work_and_resubmit_same_tier_auto')


class KimiWindowNormalizationTests(unittest.TestCase):
    """Live endpoint regression: exhausted window omits remaining; usages ratios fall back."""

    def real_payload(self):
        return {
            'usage': {'limit': '100', 'used': '75', 'remaining': '25',
                      'resetTime': '2026-09-20T13:00:00Z'},
            'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'},
                        'detail': {'limit': '100', 'used': '100',
                                   'resetTime': '2026-09-20T13:00:00Z'}}],
            'usages': {'limit_5h': {'used_ratio': 1, 'reset_time': '2026-09-20T13:00:00Z'},
                       'limit_7d': {'used_ratio': 0.750402, 'reset_time': '2026-09-27T00:00:00Z'}},
        }

    def test_exhausted_window_without_remaining_blocks_dispatch(self):
        result = quota.normalize('kimi-for-coding', self.real_payload())
        windows = {w['name']: w for w in result['windows']}
        self.assertTrue(windows['window_0']['valid'])
        self.assertEqual(windows['window_0']['remaining'], 0)
        self.assertEqual(windows['window_0']['remaining_percent'], 0)
        # Both ratios duplicate a window already present: the 5h limits row and the
        # authoritative overall weekly aggregate. One card each, no duplicates.
        self.assertNotIn('usages.limit_5h', windows)
        self.assertNotIn('usages.limit_7d', windows)
        self.assertEqual(windows['overall']['remaining'], 25)
        self.assertFalse(result['available'])
        view = dict(result, state='ok', stale=False)
        self.assertEqual(quota.allowed('kimi-for-coding', {'kimi-for-coding': view}, 'normal'),
                         (False, 'quota_exhausted_or_account_unavailable'))

    def test_remaining_derived_from_limit_minus_used(self):
        result = quota.normalize('kimi-for-coding', {'limits': [
            {'detail': {'limit': '100', 'used': '40'}}]})
        self.assertTrue(result['windows'][0]['valid'])
        self.assertEqual(result['windows'][0]['remaining'], 60)
        self.assertEqual(result['windows'][0]['remaining_percent'], 60)

    def test_malformed_used_stays_unknown(self):
        result = quota.normalize('kimi-for-coding', {'limits': [
            {'detail': {'limit': '100', 'used': 'junk'}}]})
        self.assertFalse(result['windows'][0]['valid'])
        self.assertIsNone(result['windows'][0]['remaining'])
        self.assertIsNone(result['available'])

    def test_used_over_limit_reads_exhausted(self):
        result = quota.normalize('kimi-for-coding', {'limits': [
            {'detail': {'limit': '100', 'used': '140'}}]})
        self.assertTrue(result['windows'][0]['valid'])
        self.assertEqual(result['windows'][0]['remaining'], 0)

    def test_usage_ratio_fallback_without_limits(self):
        exhausted = quota.normalize('kimi-for-coding', {'usages': {'limit_5h': {'used_ratio': 1}}})
        self.assertFalse(exhausted['available'])
        self.assertEqual(exhausted['windows'][0]['duration_minutes'], 300)
        healthy = quota.normalize('kimi-for-coding', {'usages': {'limit_7d': {'used_ratio': 0.25}}})
        self.assertTrue(healthy['available'])
        self.assertEqual(healthy['windows'][0]['remaining_percent'], 75.0)
        self.assertEqual(healthy['windows'][0]['duration_minutes'], 10080)
        malformed = quota.normalize('kimi-for-coding',
                                    {'usages': {'limit_5h': {'used_ratio': 'junk'}}})
        self.assertIsNone(malformed['available'])

    def test_valid_overall_weekly_is_authoritative_over_limit_7d(self):
        # A ratio claiming full weekly use must not override a valid overall aggregate.
        result = quota.normalize('kimi-for-coding', {
            'usage': {'limit': 100, 'remaining': 25},
            'usages': {'limit_7d': {'used_ratio': 1}}})
        names = [w['name'] for w in result['windows']]
        self.assertEqual(names, ['overall'])
        self.assertTrue(result['available'])

    def test_limit_7d_fallback_used_when_overall_is_unusable(self):
        result = quota.normalize('kimi-for-coding', {
            'usage': {'limit': 'junk'},
            'usages': {'limit_7d': {'used_ratio': 1}}})
        windows = {w['name']: w for w in result['windows']}
        self.assertTrue(windows['usages.limit_7d']['valid'])
        self.assertEqual(windows['usages.limit_7d']['duration_minutes'], 10080)
        self.assertEqual(windows['usages.limit_7d']['remaining'], 0)
        self.assertFalse(result['available'])
        view = dict(result, state='ok', stale=False)
        self.assertEqual(quota.allowed('kimi-for-coding', {'kimi-for-coding': view}, 'normal'),
                         (False, 'quota_exhausted_or_account_unavailable'))

    def test_malformed_overall_without_proof_stays_unknown(self):
        result = quota.normalize('kimi-for-coding', {'usage': {'limit': 'junk'}})
        self.assertEqual(len(result['windows']), 1)
        self.assertFalse(result['windows'][0]['valid'])
        self.assertIsNone(result['available'])
        view = dict(result, state='ok', stale=False)
        self.assertTrue(quota.allowed('kimi-for-coding', {'kimi-for-coding': view}, 'normal')[0])

    def test_unknown_keys_and_overage_ratios_are_not_guessed_windows(self):
        result = quota.normalize('kimi-for-coding', {'usages': {
            'limit_1h': {'used_ratio': 1},
            'limit_5h': {'used_ratio': 4.2},
            'limit_7d': {'used_ratio': -0.5},
        }})
        self.assertEqual(result['windows'], [])
        self.assertIsNone(result['available'])


class WorkerWindowRecoveryTests(unittest.TestCase):
    """The native retry loop stops on explicit window exhaustion; generic retries continue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.json'
        self.c = {'auto_approve': False, 'kimi_reserve_percent': 0, 'profiles': profiles(),
                  'routing': {'fast': 'fallback', 'background': 'senior-code', 'deep': 'deep-research'},
                  'routing_policy': {'background': BACKGROUND_POLICY, 'deep': DEEP_POLICY}}
        self.config.write_text(json.dumps(self.c))
        self.patchers = [patch.object(common, 'STATE', self.state), patch.object(worker, 'STATE', self.state),
                         patch.object(quota, 'STATE', self.state), patch.object(routing, 'STATE', self.state),
                         patch.object(common, 'CONFIG', self.config),
                         patch.object(quota, 'credential_identity', lambda p: 'cred-' + str(p))]
        for p in self.patchers:
            p.start()
        common.init()
        self.write_task()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def write_task(self, **fields):
        t = {'id': 'job-window', 'title': 'window task', 'profile': 'deep-research', 'status': 'running',
             'mode': 'read', 'scopes': [], 'directory': str(self.root), 'session_id': 'ses_window',
             'message_id': 'msg_test', 'started_at': time.time() - 10, 'complexity': 'deep',
             'dispatch_attempted_at': time.time() - 10}
        t.update(fields)
        common.write_json(common.task_path(t['id']), t)
        return t

    def test_5h_retry_status_stops_and_requests_needs_attention(self):
        status = {'ses_window': {'type': 'retry', 'message': WINDOW_5H, 'attempt': 3}}
        with patch.object(worker, 'call', return_value=[]) as call, \
             patch.object(worker, 'api', return_value=status), \
             patch.object(worker, 'stop', return_value=True) as stop, \
             patch.object(worker, 'reroute_after_capacity_stop', return_value=False), \
             patch.object(worker, 'finish', return_value={'done': True}) as finish:
            worker.run_task('job-window', threading.Event())
        stop.assert_called_once()
        self.assertEqual(finish.call_args.args[2], 'needs_attention')
        self.assertEqual(finish.call_args.args[3], 'provider_usage_window_limit')
        self.assertFalse(any(c.args[1] == '/prompt_async' for c in call.call_args_list))
        errors = common.task('job-window').get('errors') or []
        self.assertTrue(any(e.get('usage_window') for e in errors))

    def test_unconfirmed_abort_retains_ownership_without_replay(self):
        status = {'ses_window': {'type': 'retry', 'message': WINDOW_5H, 'attempt': 1}}
        shutdown = threading.Event()
        with patch.object(worker, 'call', return_value=[]) as call, \
             patch.object(worker, 'api', return_value=status), \
             patch.object(worker, 'stop', return_value=False), \
             patch.object(worker, 'finish', return_value={'done': True}) as finish:
            threading.Timer(0.4, shutdown.set).start()
            worker.run_task('job-window', shutdown)
        self.assertFalse(finish.called)
        self.assertFalse(any(c.args[1] == '/prompt_async' for c in call.call_args_list))
        self.assertEqual(common.task('job-window')['status'], 'uncertain')

    def test_429_retry_stops_and_enters_capacity_reroute(self):
        shutdown = threading.Event()
        status = {'ses_window': {'type': 'retry', 'statusCode': 429, 'message': WINDOW_5H}}
        with patch.object(worker, 'call', return_value=[]), \
             patch.object(worker, 'api', return_value=status), \
             patch.object(worker, 'stop', return_value=True) as stop, \
             patch.object(worker, 'reroute_after_capacity_stop',
                          side_effect=lambda *args: shutdown.set() or True) as reroute, \
             patch.object(worker, 'finish', return_value={'done': True}) as finish:
            worker.run_task('job-window', shutdown)
        stop.assert_called_once()
        reroute.assert_called_once()
        self.assertFalse(finish.called)

    def test_capacity_reroute_keeps_session_and_selects_same_tier_peer(self):
        common.write_json(self.state / 'quota.json', {})
        with patch.object(worker, 'call', return_value=[]) as call:
            self.assertTrue(worker.reroute_after_capacity_stop(
                common.task('job-window'), 'kimi-for-coding', 'provider_usage_or_rate_limit'))
        task = common.task('job-window')
        self.assertEqual(task['session_id'], 'ses_window')
        self.assertEqual(task['profile'], 'ark-k3')
        self.assertEqual(task['excluded_providers'], ['kimi-for-coding'])
        self.assertEqual(task['route_history'][-1]['to_profile'], 'ark-k3')
        prompt_call = next(c for c in call.call_args_list if c.args[1] == '/prompt_async')
        self.assertEqual(prompt_call.args[3]['model']['providerID'], 'ark')
        self.assertIn('Do not repeat completed', prompt_call.args[3]['parts'][0]['text'])

    def test_capacity_reroute_respects_explicit_model_pin(self):
        common.update('job-window', requested_profile='deep-research')
        with patch.object(worker, 'call') as call:
            self.assertFalse(worker.reroute_after_capacity_stop(
                common.task('job-window'), 'kimi-for-coding', 'provider_usage_or_rate_limit'))
        call.assert_not_called()
        self.assertEqual(common.task('job-window')['profile'], 'deep-research')

    def test_generic_retry_keeps_waiting(self):
        status = {'ses_window': {'type': 'retry', 'message': 'Rate limit exceeded, retry later'}}
        shutdown = threading.Event()
        with patch.object(worker, 'call', return_value=[]) as call, \
             patch.object(worker, 'api', return_value=status), \
             patch.object(worker, 'stop') as stop, \
             patch.object(worker, 'finish', return_value={'done': True}) as finish:
            threading.Timer(0.4, shutdown.set).start()
            worker.run_task('job-window', shutdown)
        self.assertFalse(stop.called)
        self.assertFalse(finish.called)
        self.assertFalse(any(c.args[1] == '/prompt_async' for c in call.call_args_list))
        errors = common.task('job-window').get('errors') or []
        self.assertTrue(any(e.get('code') == 'retrying' and e.get('suggested_action') == 'wait'
                            for e in errors))

    def test_finish_on_window_model_error_attaches_ordered_recovery(self):
        message = {'info': {'id': 'msg_window', 'role': 'assistant',
                            'providerID': 'kimi-for-coding', 'modelID': 'k3',
                            'time': {'created': 1700000000000, 'completed': 1700000010000},
                            'error': {'name': 'APIError', 'data': {'message': WINDOW_5H,
                                                                   'statusCode': 403}}},
                   'parts': []}
        result = worker.finish(common.task('job-window'), [message])
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['reason'], 'provider_usage_window_limit')
        self.assertTrue(result['recovery']['usage_window_exhausted'])
        self.assertEqual([a['profile'] for a in result['recovery']['alternatives']],
                         ['ark-k3', 'fallback'])
        self.assertIsNone(quota.billing_block('kimi-for-coding'))
        self.assertFalse(any(e.get('billing') for e in result['errors']))

    def test_wait_result_reports_window_next_action(self):
        task = {'id': 'job-window', 'profile': 'deep-research', 'requested_profile': 'deep-research',
                'complexity': 'deep', 'status': 'needs_attention',
                'reason': 'provider_usage_window_limit',
                'errors': [{'source': 'model', 'message': WINDOW_5H, 'usage_window': True}]}
        rec = quota.window_failure_recovery(task, self.c, {})
        waited = delegate.wait_result(dict(task, recovery=rec), 5)
        self.assertEqual(waited['next_action'], 'resubmit_same_tier_auto')
        self.assertTrue(waited['attention'])
        self.assertEqual(waited['recovery']['blocked_provider'], 'kimi-for-coding')


class DynamicRunwayTests(unittest.TestCase):
    deep_adaptive = {'ladder': [[3, 1], [2, 1], [1, 1]]}
    mid_adaptive = {'ladder': [[2, 1], [1, 1], [1, 2]]}

    def sample(self, remaining_percent):
        return {'state': 'ok', 'available': True, 'stale': False,
                'windows': [{'valid': True, 'remaining_percent': remaining_percent}]}

    def test_equal_runway_preserves_deep_two_to_one_and_mid_one_to_one(self):
        q = {'kimi-for-coding': self.sample(50), 'volcengine-agent-plan': self.sample(50)}
        deep = [{'profile': 'native', 'weight': 2}, {'profile': 'ark', 'weight': 1}]
        effective, reason, info = routing.dynamics(
            deep, {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.deep_adaptive)
        self.assertEqual(effective, deep)
        self.assertEqual(reason, 'baseline_balanced')
        self.assertAlmostEqual(info['native']['share'], 2 / 3)
        mid = [{'profile': 'native', 'weight': 1}, {'profile': 'ark', 'weight': 1}]
        effective, _, _ = routing.dynamics(
            mid, {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.mid_adaptive)
        self.assertEqual([e['weight'] for e in effective], [1, 1])

    def test_kimi_pressure_moves_only_one_bounded_step_toward_ark(self):
        q = {'kimi-for-coding': self.sample(20), 'volcengine-agent-plan': self.sample(100)}
        deep, reason, _ = routing.dynamics(
            [{'profile': 'native', 'weight': 2}, {'profile': 'ark', 'weight': 1}],
            {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.deep_adaptive)
        self.assertEqual([e['weight'] for e in deep], [1, 1])
        self.assertEqual(reason, 'runway_shift_right')
        mid, _, _ = routing.dynamics(
            [{'profile': 'native', 'weight': 1}, {'profile': 'ark', 'weight': 1}],
            {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.mid_adaptive)
        self.assertEqual([e['weight'] for e in mid], [1, 2])

    def test_ark_pressure_moves_only_one_bounded_step_toward_native_kimi(self):
        q = {'kimi-for-coding': self.sample(100), 'volcengine-agent-plan': self.sample(20)}
        deep, _, _ = routing.dynamics(
            [{'profile': 'native', 'weight': 2}, {'profile': 'ark', 'weight': 1}],
            {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.deep_adaptive)
        self.assertEqual([e['weight'] for e in deep], [3, 1])
        mid, _, _ = routing.dynamics(
            [{'profile': 'native', 'weight': 1}, {'profile': 'ark', 'weight': 1}],
            {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.mid_adaptive)
        self.assertEqual([e['weight'] for e in mid], [2, 1])

    def test_unknown_or_stale_telemetry_never_changes_baseline(self):
        entries = [{'profile': 'native', 'weight': 2}, {'profile': 'ark', 'weight': 1}]
        q = {'kimi-for-coding': self.sample(20),
             'volcengine-agent-plan': dict(self.sample(100), stale=True)}
        effective, reason, _ = routing.dynamics(
            entries, {'native': 'kimi-for-coding', 'ark': 'volcengine-agent-plan'}, q,
            now=1, adaptive=self.deep_adaptive)
        self.assertEqual(effective, entries)
        self.assertEqual(reason, 'telemetry_unknown')

    def test_profiles_on_one_provider_share_one_signal_and_do_not_self_balance(self):
        entries = [{'profile': 'ark-a', 'weight': 2}, {'profile': 'ark-b', 'weight': 1}]
        effective, reason, _ = routing.dynamics(
            entries, {'ark-a': 'volcengine-agent-plan', 'ark-b': 'volcengine-agent-plan'},
            {'volcengine-agent-plan': self.sample(80)}, now=1, adaptive=self.deep_adaptive)
        self.assertEqual(effective, entries)
        self.assertEqual(reason, 'single_provider')

    def test_fixed_pool_never_changes_without_explicit_adaptive_ladder(self):
        entries = [{'profile': 'primary', 'weight': 1}, {'profile': 'reviewer', 'weight': 1}]
        q = {'first': self.sample(5), 'second': self.sample(100)}
        effective, reason, _ = routing.dynamics(
            entries, {'primary': 'first', 'reviewer': 'second'}, q, now=1)
        self.assertEqual(effective, entries)
        self.assertEqual(reason, 'fixed_weights')


if __name__ == '__main__':
    unittest.main()
