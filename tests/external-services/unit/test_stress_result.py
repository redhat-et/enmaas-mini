import copy
import unittest
from stress_result import check_report

class StressResult(unittest.TestCase):
    def test_every_supported_profile_and_selected_interval_can_be_verified(self):
        from stress_plan import plan, select_phases
        for profile in ('decade','gradual','extended','full','higher'):
            for minimum in (10,100):
                rows=select_phases(plan(profile=profile,concurrency=40000), minimum=minimum)
                summary=self.summary()
                template=summary['phases'][0]
                summary['phases']=[dict(template,name=row['name'],offered_rps=row['offered_rps'],
                    offered_slots=600*row['offered_rps'],dispatched=600*row['offered_rps'],
                    attempts=600*row['offered_rps'],statuses={'200':600*row['offered_rps']},
                    max_in_flight=40000,peak_in_flight=30000) for row in rows]
                summary.update(planned_phases=rows,requested_max_rate=max(row['offered_rps'] for row in rows))
                with self.subTest(profile=profile,minimum=minimum):
                    self.assertEqual(check_report(summary,{'complete':True})['outcome'], 'requested_stages_completed')

    def summary(self):
        rows=[]
        for name,rate in (('stress_10',10),('stress_recovery',10)):
            rows.append({'name':name,'offered_rps':rate,'target_seconds':600,'admission_seconds':600,
                'elapsed_seconds':700,'offered_slots':6000,'dispatched':6000,'attempts':6000,
                'statuses':{'200':6000},'errors':{},'skipped_concurrency':0,'skipped_scheduler':0,
                'telemetry_verified':True,'peak_in_flight':650,'max_in_flight':1200,'interrupted':False})
        return {'mock_only':True,'ledger_verified':True,'requested_max_rate':10,
                'phase_duration_seconds':600,'phases':rows}

    def test_full_requested_sequence_is_not_claimed_as_ten_thousand(self):
        result=check_report(self.summary(),{'complete':True,'errors':[]})
        self.assertEqual(result['highest_offered_rps'],10)
        self.assertEqual(result['outcome'],'requested_stages_completed')
        self.assertEqual(result['successful_requests'],12000)

    def test_gradual_profile_requires_every_planned_stage(self):
        summary=self.summary();template=summary['phases'][0]
        rows=[]
        for rate in (10,50,100,150):
            count=600*rate
            rows.append(dict(template,name='stress_'+str(rate),offered_rps=rate,offered_slots=count,dispatched=count,attempts=count,statuses={'200':count}))
        rows.append(summary['phases'][-1])
        summary.update(phases=rows,planned_phases=[{'name':r['name'],'offered_rps':r['offered_rps']} for r in rows],requested_max_rate=150)
        result=check_report(summary,{'complete':True})
        self.assertEqual(result['highest_offered_rps'],150)
        summary['phases']=rows[:2]+rows[3:]
        with self.assertRaises(ValueError):check_report(summary,{'complete':True})
        summary['planned_phases'][1]['offered_rps']=51
        with self.assertRaises(ValueError):check_report(summary,{'complete':True})

    def test_unproven_skipped_stages_rejected(self):
        summary=self.summary();summary['requested_max_rate']=10000
        with self.assertRaises(ValueError):check_report(summary,{'complete':True})

    def test_limit_is_reported_separately_from_service_failure(self):
        summary=self.summary();summary['requested_max_rate']=10000
        row=copy.deepcopy(summary['phases'][0]);row.update(name='stress_100',offered_rps=100,
            offered_slots=60000,skipped_concurrency=54000)
        summary['phases'].insert(1,row);summary['escalation_stop']='generator_concurrency_limit'
        result=check_report(summary,{'complete':True})
        self.assertEqual(result['outcome'],'bounded_limit_observed')
        self.assertEqual(result['highest_offered_rps'],100)
        self.assertEqual(result['stop_reason'],'generator_concurrency_limit')

    def test_missing_cleanup_accounting_duration_and_recovery_fail(self):
        for key,value in (('ledger_verified',False),('mock_only',False)):
            summary=self.summary();summary[key]=value
            with self.assertRaises(ValueError):check_report(summary,{'complete':True})
        for field,value in (('admission_seconds',1),('telemetry_verified',False),('interrupted',True),('dispatched',5999),('admission_seconds',float('nan')),('max_in_flight',0),('peak_in_flight',True)):
            summary=self.summary();summary['phases'][-1][field]=value
            with self.assertRaises(ValueError):check_report(summary,{'complete':True})
        with self.assertRaises(ValueError):check_report(self.summary(),{'complete':False})

    def test_low_request_count_cannot_qualify_a_higher_offered_rate(self):
        summary=self.summary()
        row=summary['phases'][0]
        row.update(offered_slots=1,dispatched=1,attempts=1,statuses={'200':1})
        with self.assertRaisesRegex(ValueError,'sustained target'):
            check_report(summary,{'complete':True})
