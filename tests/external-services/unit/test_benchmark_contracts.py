"""Benchmark result, phase, refresh and dashboard contracts retained from integration."""
import importlib.util
import json
import os
import fcntl
from pathlib import Path
import unittest
import tempfile
import yaml
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
monitor=load('benchmark_monitor','benchmark_monitoring.py')
update=load('benchmark_update','monitoring-update.py')
result=load('benchmark_result','monitoring-result.py')
phase=load('benchmark_phase','phase-metrics.py')

class BenchmarkContracts(unittest.TestCase):
    def test_capacity_renderer_uses_operational_queries_with_split_modules(self):
        reply={'status':'success','data':{'result':[{
            'metric':{'host':'fixture','component':'gateway'},'values':[[1,'2'],[2,'4']]}]}}
        phases=[{'name':'baseline','cycle':1,'started_utc':'2026-01-01T00:00:00+00:00',
                 'ended_utc':'2026-01-01T00:01:05+00:00'}]
        with patch.object(result.signals,'query',return_value=reply) as query:
            report=result.capacity(phases)
        self.assertEqual(query.call_count,14)
        self.assertEqual(report['phases'][0]['metrics']['host_cpu_percent']['mean'],3)
        self.assertEqual(report['phases'][0]['resources']['service_memory_bytes'][0]['labels']['host'],'fixture')

    def test_generator_concurrency_uses_outstanding_outcomes(self):
        docs=monitor.all_dashboards()
        panel=docs['benchmark-dashboard.json']['spec']['panels']['concurrency']['spec']
        query=panel['queries'][0]['spec']['plugin']['spec']['query']
        self.assertIn('enmaas_stress_dispatched - enmaas_stress_completed',query)
        self.assertNotIn('enmaas_stress_active_requests',query)

    def test_benchmark_population_estimates_are_alternatives_with_explicit_rates(self):
        panels=monitor.all_dashboards()['benchmark-dashboard.json']['spec']['panels']
        for key,factor in (('people',60),('bots',6)):
            panel=panels[key]['spec']
            self.assertIn('alternative',panel['display']['description'].lower())
            self.assertIn('not observed',panel['display']['description'].lower())
            queries=[q['spec']['plugin']['spec']['query'] for q in panel['queries']]
            self.assertEqual(queries,[f'{factor} * enmaas_benchmark_offered_rps',f'{factor} * enmaas_stress_dispatched / clamp_min(enmaas_stress_elapsed_seconds, 1)'])

    def test_update_refuses_active_benchmark_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.benchmark.lock'
            fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,'benchmark'):
                    with update.monitoring_lock(path):pass
            finally:os.close(fd)
            with update.monitoring_lock(path):pass
            with update.monitoring_lock(path):pass

    def test_result_rejects_partial_runs_and_unreconciled_accounting(self):
        phase={'cycle':1,'interrupted':False,'errors':{},'telemetry_verified':True,'admission_seconds':65,'target_seconds':65,'statuses':{'200':1}}
        phases=[dict(phase,name=name) for name in ('baseline','team','parallel','sustained_high','rate_limited','recovery')]
        phases[4]['statuses']={'200':1,'429':1}
        summary={'mock_only':True,'ledger_verified':True,'phases':phases}
        self.assertEqual(result.check_report(summary,{'complete':True}),phases)
        for change in ({'interrupted':True},{'errors':{'Timeout':1}},{'telemetry_verified':False},{'admission_seconds':20},{'cycle':2},{'statuses':{'200':1,'500':1}},{'statuses':{'429':1}}):
            with self.assertRaises(ValueError):result.check_report(dict(summary,phases=[dict(phases[0],**change),*phases[1:]]),{'complete':True})
        with self.assertRaises(ValueError):result.check_report(dict(summary,phases=phases[:-1]),{'complete':True})
        with self.assertRaises(ValueError):result.check_report(dict(summary,ledger_verified=False),{'complete':True})
        with self.assertRaises(ValueError):result.check_report(summary,{'complete':False})

    def test_result_requires_every_expected_healthy_alert_rule(self):
        rules=[{'name':entry['alert'],'health':'ok','lastError':''}
               for group in monitor.rules()['groups'] for entry in group['rules']]
        def reply(rows):
            return {'status':'success','data':{'groups':[
                {'name':'enmaas-operational','rules':rows[:-3]},
                {'name':'enmaas-benchmark','rules':rows[-3:]}]}}
        self.assertEqual(result.check_rules(reply(rules)),rules)
        invalid=(rules[:-1],rules+[rules[0]],[dict(rules[0],health='err'),*rules[1:]],
                 [dict(rules[0],lastError='query failed'),*rules[1:]],
                 [dict(rules[0],name='EnmaasUnexpected'),*rules[1:]])
        for rows in invalid:
            with self.subTest(rows=rows):
                with self.assertRaises(RuntimeError):result.check_rules(reply(rows))
        for value in (None,{},dict(reply(rules),status='error')):
            with self.assertRaises(RuntimeError):result.check_rules(value)
        for index in (0,1):
            missing=reply(rules)
            missing['data']['groups'].pop(index)
            with self.assertRaises(RuntimeError):result.check_rules(missing)

    def test_capacity_aggregates_actual_values_and_rejects_empty_results(self):
        reply={'status':'success','data':{'result':[{'values':[[1,'NaN'],[2,'2'],[3,'6']]}]}}
        self.assertEqual(result.measurements(reply),{'minimum':2,'maximum':6,'mean':4,'samples':2})
        for reply in (None,{'status':'error'}, {'status':'success','data':{'result':[]}},
                      {'status':'success','data':{'result':[{'values':[[1,'NaN']]}]}}):
            with self.assertRaises(ValueError):result.measurements(reply)

    def test_capacity_report_preserves_multiple_resource_identities(self):
        reply={'status':'success','data':{'result':[
          {'metric':{'host':'one','component':'gateway','replica':'a','instance':'private-address'},'values':[[1,'1'],[2,'3']]},
          {'metric':{'host':'two','component':'gateway','replica':'a'},'values':[[1,'9'],[2,'11']]}]}}
        rows=result.series_measurements(reply)
        self.assertEqual([r['mean'] for r in rows],[2,10])
        self.assertEqual([r['labels']['host'] for r in rows],['one','two'])
        self.assertNotIn('private-address',str(rows))
        with self.assertRaises(ValueError):result.series_measurements({'status':'success','data':{'result':[]}})

    def test_benchmark_is_concise_and_explicit_about_phase_scope(self):
        panels=monitor.all_dashboards()['benchmark-dashboard.json']['spec']['panels']
        self.assertLessEqual(len(panels),22)
        for key in ('rates','concurrency','skips','failures','phase','firing'):
            self.assertIn(key,panels)
        self.assertNotIn('estimatedpeople',panels)
        for key in ('loadcpu','loadmemory','loadpool','loadduration'):
            self.assertIn(key,panels)
            self.assertEqual(panels[key]['spec']['plugin']['spec']['legend']['values'],[])
        self.assertIn('MEANING\n',panels['rates']['spec']['display']['description'])
        for key in ('currentgenerator','currentgeneratorskips'):
            spec=panels[key]['spec']
            self.assertEqual(spec['plugin']['spec']['format']['unit'],'percent')
            self.assertIn('sample_unixtime_seconds',spec['queries'][0]['spec']['plugin']['spec']['query'])

    def test_mock_resources_are_separate_and_do_not_claim_backend_latency(self):
        panels=monitor.all_dashboards()['benchmark-dashboard.json']['spec']['panels']
        self.assertIn('mockintro',panels)
        for key in ('mockcpu','mockmemory'):
            self.assertEqual(panels[key]['spec']['plugin']['spec']['legend']['position'],'bottom')
            queries=panels[key]['spec']['queries']
            for query in queries:
                expression=query['spec']['plugin']['spec']['query']
                self.assertIn('component="provider"',expression)
                self.assertIn('host=~"$host"',expression)
                self.assertNotIn('or vector(0)',expression)
        memory=panels['mockmemory']['spec']
        self.assertEqual(memory['plugin']['spec']['yAxis']['format']['unit'],'bytes')
        self.assertIn('memory_limit_bytes',memory['queries'][1]['spec']['plugin']['spec']['query'])
        self.assertIn('not measured backend latency',panels['mockintro']['spec']['plugin']['spec']['text'])

    def test_monitoring_refresh_rejects_unreadable_dashboard_metadata(self):
        update.check_dashboards(lambda url:next(doc for doc in monitor.all_dashboards().values() if doc['metadata']['name']==url.rsplit('/',1)[1]))
        with self.assertRaisesRegex(RuntimeError,'differs from source'):
            update.check_dashboards(lambda url:{'metadata':{'name':url.rsplit('/',1)[1]},'spec':{'panels':{'stale':{}}}})
        for reply in (None,{}, {'metadata':{'name':'wrong'},'spec':{'panels':{}}}):
            with self.assertRaisesRegex(RuntimeError,'Perses dashboard API unavailable'):
                update.check_dashboards(lambda url:reply)

    def test_secret_replacement_preserves_security_and_unrelated_mount(self):
        command=['podman','run','--read-only','--cap-drop','ALL','--secret','old,target=/lab/config,mode=0444','--secret','tls,target=/lab/key,mode=0444','image','--config=/lab/config']
        actual=update.replacement(command,'image',{'/lab/config':'new'})
        self.assertNotIn('old,target=/lab/config,mode=0444',actual)
        self.assertIn('new,target=/lab/config,mode=0444',actual)
        self.assertIn('tls,target=/lab/key,mode=0444',actual)
        self.assertIn('--read-only',actual);self.assertIn('ALL',actual)

    def test_every_capacity_profile_has_valid_phase_telemetry(self):
        from stress_plan import plan
        for profile in ('decade','gradual','extended','full','higher'):
            for row in plan(profile=profile,concurrency=40000):
                with self.subTest(profile=profile,phase=row['name']):
                    metrics=phase.payload(row,now=1000)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
                    points=metrics[0]['gauge']['dataPoints']
                    active=[p['attributes'][0]['value']['stringValue'] for p in points if p['asDouble']==1]
                    self.assertEqual(active,[row['name']])

    def test_scenario_annotations_are_bounded_and_clear_old_phase(self):
        row={'name':'team','users':25,'streams_per_user':4,'offered_rps':30,'rate_limit_rps':1000,'cycle':2}
        metrics=phase.payload(row,now=1000)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        points=metrics[0]['gauge']['dataPoints']
        self.assertEqual(sum(p['asDouble'] for p in points),1)
        self.assertEqual(next(p for p in points if p['asDouble'])['attributes'][0]['value']['stringValue'],'team')
        idle=phase.payload({'name':'idle'},now=1000)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        self.assertEqual(sum(p['asDouble'] for p in idle[0]['gauge']['dataPoints']),0)
        for change in ({'name':'private-identity'},{'offered_rps':float('nan')},{'users':-1}):
            with self.assertRaises(ValueError):phase.payload(dict(row,**change))
