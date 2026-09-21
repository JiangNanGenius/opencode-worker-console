import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import capacity
import economics
import quota
import routing
import analytics
import workload

NOW = 1800000000


def win(name='week', p=40, hours=20, reset=144, duration=10080):
    return {'name': name, 'valid': True, 'remaining_percent': p, 'duration_minutes': duration,
            'resets_at': NOW + reset * 3600,
            'consumption_estimate': {'hours': hours, 'rate_percent_per_hour': p / hours if hours else 2}}


def provider(windows, **kw):
    return dict(state='ok', available=True, stale=False, windows=windows, **kw)


class CapacityTests(unittest.TestCase):
    def test_observed_pace_is_shared_by_routing_and_forecast(self):
        w = win()
        f = capacity.window(w, NOW)
        self.assertAlmostEqual(f['runway'], 20 / 144)
        self.assertEqual(routing._window_runway(w, NOW), f['runway'])
        self.assertEqual(routing._runway(provider([w]), NOW), f['runway'])

    def test_idle_does_not_erase_working_rate(self):
        w = win()
        w['consumption_estimate'].update(idle=True, active_rate_percent_per_hour=4)
        self.assertEqual(capacity.window(w, NOW)['hours'], 10)

    def test_expired_window_is_unknown_for_admission(self):
        w = win(reset=-1)
        self.assertIsNone(routing._window_runway(w, NOW))
        self.assertFalse(capacity.provider(provider([w]), NOW)['fresh'])

    def test_short_window_zero_blocks_full_week(self):
        q = provider([win('AFPWeekly', 90, 90), win('five', 0, 0, 2, 300)])
        fit = economics._window_fit('volcengine-agent-plan', q, NOW)
        self.assertEqual(fit['amount'], 0)
        self.assertEqual(fit['window'], 'five')

    def test_short_window_reset_cannot_restore_exhausted_month(self):
        q = provider([win('five', 0, 0, 1, 300), win('AFPMonthly', 0, 0, 240, 43200)])
        pool = economics.work_pool({}, {'volcengine-agent-plan': q}, NOW, False)
        self.assertFalse(any(x['hours_until'] == 1 for x in pool['refills']))
        self.assertTrue(all(x['hours_until'] > 1 for x in pool['refills']))

    def test_idle_short_window_does_not_hide_known_weekly_refill(self):
        short={'name':'five','valid':True,'remaining_percent':100,'duration_minutes':300,
               'resets_at':NOW+3600,'consumption_estimate':{'idle':True,'rate_percent_per_hour':0}}
        q=provider([short,win('overall',0,0,20)])
        q['available']=False
        pool=economics.work_pool({}, {'kimi-for-coding':q}, NOW, False)
        self.assertEqual(pool['refills'][0]['hours_until'],20)

    def test_billing_stop_does_not_predict_recovery_from_positive_window(self):
        q = provider([win()]); q.update(available=False, state='billing_blocked', billing={'reason':'monthly_usage_limit'})
        pool = economics.work_pool({}, {'volcengine-agent-plan': q}, NOW, False)
        self.assertEqual(pool['refills'], [])
        self.assertFalse(pool['complete'])

    def test_stale_pool_is_displayable_but_not_complete(self):
        q = provider([win()]); q['stale'] = True
        pool = economics.work_pool({}, {'volcengine-agent-plan': q}, NOW, False)
        self.assertGreater(pool['total'], 0)
        self.assertFalse(pool['complete'])

    def test_generic_provider_and_missing_configured_peer(self):
        c = {'profiles': {'a': {'model':'custom/model'}, 'b': {'model':'kimi-for-coding/k3'}}}
        pool = economics.work_pool(c, {'custom': provider([win()])}, NOW, False)
        self.assertIn('custom', pool['components'])
        self.assertFalse(pool['complete'])

    def test_only_configured_providers_count(self):
        c = {'profiles': {'a': {'model':'volcengine-agent-plan/k3'}}}
        q = {'volcengine-agent-plan':provider([win()]), 'kimi-for-coding':provider([win(p=100)])}
        pool = economics.work_pool(c, q, NOW, False)
        self.assertEqual(pool['total'], pool['components']['plan']['amount'])

    def test_conservation_recovery_needs_margin_and_stable_period(self):
        prior = {'level':2, 'at':NOW}
        held = capacity.transition(1, 27, [38,25], prior, NOW + 1)
        self.assertEqual(held['level'], 2)
        pending = capacity.transition(1, 32, [38,25], held, NOW + 60)
        self.assertEqual(pending['level'], 2)
        released = capacity.transition(1, 32, [38,25], pending, NOW + 361)
        self.assertEqual(released['level'], 1)
        self.assertEqual(capacity.transition(2, 15, [38,25], released, NOW + 362)['level'],2)
        self.assertEqual(capacity.transition(0, 100, [38,25], prior, NOW + 1, recovery=True)['level'],0)

    def test_unknown_does_not_create_new_conservation(self):
        self.assertEqual(capacity.transition(0,None,[38,25],{'level':2,'at':NOW},NOW+1)['level'],0)

    def test_capability_floor_filters_admission_and_recovery(self):
        c={'profiles':{'a':{'model':'a/normal'}, 'f':{'model':'b/fast'}, 'd':{'model':'a/deep'}},
           'routing_policy':{'fast':[[{'profile':'f','weight':1}]],
                             'background':[[{'profile':'a','weight':1}],[{'profile':'f','weight':1}]],
                             'deep':[[{'profile':'d','weight':1}],[{'profile':'a','weight':1}]]}}
        task={'requested_profile':'auto','tier':'normal','capability_floor':'normal'}
        q={'a':dict(provider([win()]),available=False),'b':provider([win()])}
        self.assertFalse(quota.meets_capability_floor('f',task,c))
        self.assertIsNone(quota.route(task,c,q)[0])
        self.assertEqual(quota.alternatives(task,c,q),[])
        task.pop('capability_floor')
        self.assertEqual(quota.route(task,c,q)[0],'f')

    def test_active_rate_excludes_idle_intervals(self):
        w=win(p=80,hours=20,reset=100)
        samples=[]
        for minutes,p in [(60,90),(55,88),(50,86),(45,84),(40,82),(35,80),(30,80),(20,80),(10,80)]:
            samples.append({'time':NOW-minutes*60,'windows':{quota._window_key(w):{'remaining_percent':p,'resets_at':w['resets_at']}}})
        estimate=quota.consumption_estimate(w,samples,NOW)
        self.assertGreater(estimate['active_rate_percent_per_hour'],estimate['rate_percent_per_hour'])
        self.assertAlmostEqual(estimate['hours'],80/24,places=2)

    def test_model_analytics_preserve_mixed_segments_and_unattributed_tail(self):
        rows=analytics._models([{'model':'a/model','usage':{'total':100,'by_model':[{'model':'a/model','total':60},{'model':'b/model','total':30}]}}])
        self.assertEqual({r['name']:r['tokens'] for r in rows},{'a/model':60,'b/model':30,'mixed / unattributed':10})

    def test_workload_avoids_double_count_and_predicts_only_after_calibration(self):
        records=[]
        for i in range(5):
            records.append(({'id':str(i),'profile':'a','tier':'normal','status':'completed','started_at':NOW-1000,'finished_at':NOW-400}, {'total':100}))
        running=({'id':'running','profile':'a','tier':'normal','status':'running','started_at':NOW-300},{'total':100})
        records.extend([running,running])
        load=workload.summarize(records,{'a':{'model':'a/model'}},NOW)['a']
        self.assertEqual(load['active_jobs'],1)
        self.assertEqual(load['confidence'],'calibrated')
        self.assertEqual(load['prediction_samples'],1)
        self.assertGreater(load['expected_remaining_seconds'],0)

    def test_demand_forecast_is_bounded_and_shared(self):
        record=provider([win()], workload={'confidence':'calibrated','demand_multiplier':9})
        f=capacity.provider(record,NOW)
        self.assertEqual(f['windows'][0]['demand_multiplier'],1.25)
        self.assertEqual(f['hours'],16)
        self.assertEqual(routing._runway(record,NOW),f['runway'])
        record['workload']['confidence']='collecting'
        self.assertEqual(capacity.provider(record,NOW)['hours'],20)

    def test_work_pool_does_not_add_simultaneous_hours_twice(self):
        c={'profiles':{'a':{'model':'custom-a/model'},'b':{'model':'custom-b/model'}}}
        q={p:provider([win()],workload={'confidence':'calibrated','observed_share':.5}) for p in ('custom-a','custom-b')}
        pool=economics.work_pool(c,q,NOW,False)
        self.assertEqual(pool['total'],20)

    def test_unknown_or_stale_peer_cannot_change_adaptive_weights(self):
        entries=[{'profile':'a','weight':2},{'profile':'b','weight':1}]
        q={'a':provider([win()]),'b':provider([win()])}
        q['b']['stale']=True
        result,reason,_=routing.dynamics(entries,{'a':'a','b':'b'},q,NOW,{'ladder':[[3,1],[2,1],[1,1]]})
        self.assertEqual(result,entries)
        self.assertEqual(reason,'telemetry_unknown')

    def test_capability_floor_validation_happens_before_service_start(self):
        import delegate
        spec={'directory':str(Path(__file__).resolve().parents[1]),'objective':'Inspect',
              'tier':'fast','capability_floor':'deep','capability_reason':'Required logic'}
        with patch.object(delegate,'config',return_value={'profiles':{}}):
            with self.assertRaisesRegex(ValueError,'Capability floor'):
                delegate.submit(spec)

    def test_replay_is_deterministic(self):
        import replay_capacity
        key='week|10080'
        history={'x':{'samples':[{'time':NOW-600,'windows':{key:{'remaining_percent':50,'resets_at':NOW+3600}}},
                                  {'time':NOW,'windows':{key:{'remaining_percent':40,'resets_at':NOW+3600}}}]}}
        c={'profiles':{'x':{'model':'x/model'}},'routing':{'fast':'x','background':'x'}}
        first=replay_capacity.replay(history,c)
        self.assertEqual(first,replay_capacity.replay(history,c))
        self.assertEqual(first['samples'],2)


if __name__ == '__main__':
    unittest.main()
