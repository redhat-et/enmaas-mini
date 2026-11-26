#!/usr/bin/env python3
import importlib.util
import math
from pathlib import Path
import unittest
from unittest.mock import MagicMock

spec=importlib.util.spec_from_file_location('report',(Path(__file__).resolve().parents[1] / 'report.py'))
report=importlib.util.module_from_spec(spec);spec.loader.exec_module(report)

class Report(unittest.TestCase):
    def test_completed_outcomes_are_not_presented_as_successful_throughput(self):
        axes=[MagicMock() for _ in range(3)];plt=MagicMock()
        plt.subplots.return_value=(MagicMock(),axes)
        phase={'name':'stress_500','offered_rps':500,'offered_slots':1000,
               'dispatched':1000,'admission_seconds':2,'completed_rps':100,
               'statuses':{'200':800,'502':200},'elapsed_seconds':10,
               'peak_in_flight':500,'max_in_flight':1000,
               'skipped_concurrency':0,'skipped_scheduler':0}
        self.assertTrue(report.stress_graph(plt,Path('/unused'),[phase]))
        series={call.kwargs['label']:call.args[1] for call in axes[0].plot.call_args_list}
        self.assertEqual(series['All outcomes / time including drain'],[100])
        self.assertEqual(series['HTTP 200 / time including drain'],[80])


    def test_many_stage_labels_remain_short_and_distinguish_repeated_rates(self):
        phases=[{'name':'experiment / stress_500','offered_rps':500} for _ in range(25)]
        phases[-1]={'name':'experiment / stress_recovery','offered_rps':10}
        labels=[report.stress_tick(i,p) for i,p in enumerate(phases)]
        self.assertEqual(len(set(labels)),25)
        self.assertTrue(all(max(map(len,label.splitlines()))<=8 for label in labels))
        self.assertEqual(labels[-1],'P25\nRecovery')


    def test_aborted_stage_is_never_presented_as_qualified(self):
        self.assertEqual(report.failure_notice({'ledger_verified':True}),'')
        notice=report.failure_notice({'failure_type':'SafetyStop','failure_phase':'stress_1000','failure_reason':'gateway <OOM>'})
        self.assertIn('not qualified',notice)
        self.assertIn('gateway &lt;OOM&gt;',notice)
        self.assertIn('Only completed stages are tabulated',notice)
        self.assertIn('accounting verification or cleanup',notice)

    def test_capacity_table_escapes_labels_and_validates_measurements(self):
        metrics={name:{'minimum':2,'maximum':4} for name in ('host_cpu_percent','host_available_memory_percent',
            'host_available_disk_percent','sampled_containers_memory_mib','metering_pool_wait_seconds_per_second','mean_inference_seconds')}
        data={'scope':'Linux <host>','phases':[{'phase':'<team>','cycle':1,'metrics':metrics}]}
        text=report.capacity_table(data)
        self.assertIn('&lt;host&gt;',text);self.assertIn('&lt;team&gt;',text)
        self.assertIn('not a percentile',text)
        metrics['host_cpu_percent']['maximum']=float('nan')
        with self.assertRaises(ValueError):report.capacity_table(data)

    def test_failed_qualification_is_visible_and_escaped(self):
        text=report.capacity_table({'scope':'local','phases':[], 'qualification_passed':False,'failure':'Tempo <OOM>'})
        self.assertIn('Qualification failed',text)
        self.assertIn('Tempo &lt;OOM&gt;',text)

    def test_host_storage_units_and_optional_wal(self):
        record={'utc':'2026-01-01T00:00:00Z',
                'memory':{'MemAvailable_kib':2*1024**2,'Cached_kib':1024**2},
                'disk_available_percent':51.5,
                'postgres':{'database_bytes':3*1024**2,'wal_directory_bytes':None}}
        series=report.host_resources([record])
        self.assertEqual(series['available_gib'][0][1],2)
        self.assertEqual(series['cached_gib'][0][1],1)
        self.assertEqual(series['disk_percent'][0][1],51.5)
        self.assertEqual(series['database_mib'][0][1],3)
        self.assertNotIn('wal_mib',series)
        record['postgres']['wal_directory_bytes']=32*1024**2
        self.assertEqual(report.host_resources([record])['wal_mib'][0][1],32)

    def test_missing_host_fields_are_not_invented_and_bad_values_fail(self):
        self.assertEqual(report.host_resources([{'utc':'2026-01-01T00:00:00Z'}]),{})
        for value in (-1,101,float('nan')):
            with self.assertRaises(ValueError):
                report.host_resources([{'utc':'2026-01-01T00:00:00Z','disk_available_percent':value}])
        with self.assertRaises(ValueError):
            report.host_resources([{'utc':'2026-01-01T00:00:00Z','postgres':{'database_bytes':-1}}])

    def test_missing_middle_sample_breaks_storage_line(self):
        rows=[{'utc':f'2026-01-01T00:00:{i:02}Z','postgres':{'database_bytes':1024**2}} for i in (0,10,20)]
        rows[1]['postgres']={'error':'collection failed'}
        points=report.host_resources(rows)['database_mib']
        self.assertTrue(math.isnan(points[1][1]))
        self.assertEqual(points[0][1],1)
        self.assertEqual(points[2][1],1)
        with self.assertRaisesRegex(ValueError,'Invalid host/storage'):
            report.host_resources([{'utc':rows[0]['utc'],'memory':[]}])

    def test_units_do_not_confuse_decimal_and_binary_memory(self):
        self.assertEqual(report.memory_bytes('2MB'),2000000)
        self.assertEqual(report.memory_bytes('2 MiB'),2097152)
        for invalid in ('NaNMB','-2MB','2PB','2'):
            with self.assertRaises(ValueError):report.memory_bytes(invalid)

    def test_reject_failed_and_nonfinite_samples(self):
        with self.assertRaises(ValueError):report.resources([{'utc':'2026-01-01T00:00:00Z','containers':{'error':'failed'}}])
        with self.assertRaises(ValueError):report.number('nan')
        with self.assertRaises(ValueError):report.stamp('2026-01-01T00:00:00')

    def test_duration_units(self):
        self.assertAlmostEqual(report.cpu_seconds('1m2.5s'),62.5)
        self.assertAlmostEqual(report.cpu_seconds('250ms'),.25)
        with self.assertRaises(ValueError):report.cpu_seconds('NaN')

    def test_counter_reset_is_a_gap_not_negative_cpu_or_failed_report(self):
        rows=[{'utc':f'2026-01-01T00:00:{i:02}Z','containers':[
            {'name':'enmaas-local-tempo','id':'same-container','cpu_time':counter,'mem_usage':'2 MiB / 1 GiB'}]}
            for i,counter in ((0,'10s'),(10,'20s'),(20,'0s'),(30,'5s'))]
        values=report.resources(rows)['tempo']
        self.assertEqual(values[1][1],100)
        self.assertTrue(math.isnan(values[2][1]))
        self.assertEqual(values[3][1],50)

    def test_normalizes_names_and_keeps_multicore_cpu(self):
        records=[{'utc':f'2026-01-01T00:00:{i:02}Z','containers':[
            {'name':'enmaas-local-gateway-sso','id':'id1','cpu_time':f'{i*2}s','mem_usage':'2 MiB / 10 MiB'}]} for i in (0,10)]
        values=report.resources(records)['gateway']
        self.assertEqual(values[1][1:],(200,2))
        self.assertEqual(values[1][0]-values[0][0],10)

    def test_hosts_and_service_replicas_do_not_merge_cpu_counters(self):
        rows=[]
        for seconds in (0,10):
            for host,multiplier in (('one',1),('two',3)):
                rows.append({'utc':f'2026-01-01T00:00:{seconds:02}Z','host':host,'containers':[
                    {'name':'enmaas-local-provider','id':'same-id-on-different-hosts',
                     'cpu_time':f'{seconds*multiplier}s','mem_usage':'2 MiB / 10 MiB'},
                    {'name':'enmaas-local-provider--second','id':'replica',
                     'cpu_time':f'{seconds*2}s','mem_usage':'3 MiB / 10 MiB'}]})
        series=report.resources(rows)
        self.assertEqual(len(series),4)
        self.assertEqual(series['one / provider'][1][1],100)
        self.assertEqual(series['two / provider'][1][1],300)
        self.assertEqual(series['one / provider / second'][1][1],200)
        self.assertEqual(series['two / provider / second'][1][2],3)

if __name__=='__main__':unittest.main()
