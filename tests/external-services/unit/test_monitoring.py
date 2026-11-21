"""Operational query contracts and bounded component labels."""
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import re
import yaml

HERE=Path(__file__).resolve().parents[1]

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

monitor=load('monitor','monitoring.py')
component=load('component','container-metrics.py')

class Monitoring(unittest.TestCase):
    def test_operational_links_resolve_without_benchmark_assets(self):
        documents=monitor.all_dashboards()
        targets={document['metadata']['name'] for document in documents.values()}
        text=json.dumps(documents)
        self.assertNotIn('enmaas_stress_',text)
        self.assertNotIn('enmaas_benchmark_',text)
        self.assertNotIn('enmaas-benchmark',text)
        links=set(re.findall(r'/projects/praxis/dashboards/(enmaas-[a-z-]+)',text))
        self.assertLessEqual(links-{'enmaas-'},targets)
        self.assertTrue(all(rule[1]!='generator' for rule in monitor.RULES))

    def test_failed_trace_table_preserves_inference_scope_and_detail_links(self):
        document=monitor.all_dashboards()['traces-dashboard.json']['spec']
        panel=document['panels']['errors']['spec']
        query=panel['queries'][0]['spec']['plugin']['spec']['query']
        self.assertIn('span.enmaas.request.kind = "inference"',query)
        self.assertIn('status = error',query)
        self.assertEqual(panel['plugin']['spec']['links'],document['panels']['0']['spec']['plugin']['spec']['links'])
        self.assertIn('not a count of HTTP 5xx',panel['display']['description'])


    def test_status_log_links_preserve_time_and_select_structured_metadata(self):
        from urllib.parse import urlparse, parse_qs
        docs=monitor.all_dashboards()
        link=monitor.status_logs_link('5xx')
        query=parse_qs(urlparse(link['url']).query)
        self.assertEqual(query['start'],['${__from}'])
        self.assertEqual(query['end'],['${__to}'])
        variables={v['spec']['name']:v['spec'] for v in docs['logs-dashboard.json']['spec']['variables']}
        for name in ('log_service','log_status'):
            values=[v['value'] for v in variables[name]['plugin']['spec']['values']]
            self.assertIn(query['var-'+name][0],values)
        log_query=docs['logs-dashboard.json']['spec']['panels']['0']['spec']['queries'][0]['spec']['plugin']['spec']['query']
        self.assertIn('http_response_status_code',log_query)
        self.assertNotIn('| json',log_query)
        self.assertIn('not zero gateway errors',docs['logs-dashboard.json']['spec']['panels']['0']['spec']['display']['description'])
        with self.assertRaises(ValueError):monitor.status_logs_link('invalid')


    def test_main_shows_named_control_plane_services_and_limits(self):
        docs=monitor.dashboards()
        main=docs['monitoring-dashboard.json']['spec']['panels']
        for key,name,component_id in (('maas','MaaS Standalone','maas'),('metering','PriceTag Metering','metering-sso')):
            cpu=main[key+'cpu']['spec'];memory=main[key+'memory']['spec']
            self.assertIn(name,cpu['display']['name'])
            self.assertIn(name,memory['display']['name'])
            self.assertEqual(len(memory['queries']),2)
            for panel in (cpu,memory):
                for query in panel['queries']:
                    expression=query['spec']['plugin']['spec']['query']
                    self.assertIn('component="'+component_id+'"',expression)
                    self.assertIn('host=~"$host"',expression)
                    self.assertNotIn('sum(',expression)
            self.assertIn('memory_limit_bytes',memory['queries'][1]['spec']['plugin']['spec']['query'])

    def test_http_status_classes_are_separate_adjacent_panels(self):
        document=monitor.dashboards()['monitoring-dashboard.json']['spec']
        boxes=[]
        for status in ('3xx','4xx','5xx'):
            key='public'+status
            panel=document['panels'][key]['spec']
            query=panel['queries'][0]['spec']['plugin']['spec']['query']
            self.assertIn('status_class="'+status+'"',query)
            self.assertIn(monitor.PUBLIC,query)
            self.assertIn('timestamp(praxis_process_open_fds)',query)
            self.assertNotIn('or vector(0)',query)
            boxes.append(next(row for row in document['layouts'][0]['spec']['items'] if row['content']['$ref'].endswith('/'+key)))
        self.assertEqual([box['width'] for box in boxes],[8,8,8])
        self.assertEqual([box['x'] for box in boxes],[0,8,16])
        self.assertEqual(len({box['y'] for box in boxes}),1)
        variable=next(v['spec'] for v in document['variables'] if v['spec']['name']=='response_class')
        self.assertEqual(variable['defaultValue'],['2xx','1xx','unknown'])
        self.assertTrue(variable['allowMultiple'])
        for key in ('gatewaytraffic','public3xx','public4xx','public5xx'):
            self.assertTrue(any(link['name']=='Logs' and link['url'].endswith('/enmaas-logs') for link in document['panels'][key]['spec']['links']))
        self.assertIn('$response_class',document['panels']['gatewaytraffic']['spec']['queries'][0]['spec']['plugin']['spec']['query'])


    def test_production_overview_separates_benchmark_and_host_scope(self):
        docs=monitor.dashboards()
        main=docs['monitoring-dashboard.json']
        self.assertNotIn('enmaas_stress_',json.dumps(main))
        self.assertNotIn('enmaas_benchmark_',json.dumps(main))
        for key in ('currentcpu','currentmemory'):
            query=main['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['query']
            self.assertIn('and on(host)',query)
            self.assertIn('$host',query)
        self.assertEqual(main['spec']['variables'][0]['spec']['name'],'host')
        for key in ('hostcpu','hostmemory','hostdisk'):
            spec=main['spec']['panels'][key]['spec']
            query=spec['queries'][0]['spec']['plugin']['spec']['query']
            self.assertIn('host=~"$host"',query)
            self.assertNotIn('max(',query)
            self.assertIn('by(host)',query)
        for key in ('gatewaycpu','gatewaymemory','gatewaytraffic','upstream','active','latency','fds'):
            self.assertIn(key,main['spec']['panels'])
        query=main['spec']['panels']['firing']['spec']['queries'][0]['spec']['plugin']['spec']['query']
        self.assertIn('category!="generator"',query)
        for panel in docs['alerts-dashboard.json']['spec']['panels'].values():
            for query in panel['spec'].get('queries',[]):
                self.assertIn('category!="generator"',query['spec']['plugin']['spec']['query'])
        traffic=main['spec']['panels']['gatewaytraffic']['spec']['queries'][0]['spec']['plugin']['spec']['query']
        self.assertNotIn('$host',traffic)
        self.assertIn('fleet-wide',json.dumps(main).lower())
        self.assertNotIn('benchmark-dashboard.json',docs)






    def test_resource_units_and_multihost_layout(self):
        documents=monitor.dashboards()
        main=documents['monitoring-dashboard.json']['spec']
        for key in ('gatewaytraffic','upstream','hostcpu','hostmemory','hostdisk'):
            box=next(i for i in main['layouts'][0]['spec']['items'] if i['content']['$ref'].endswith('/'+key))
            self.assertEqual(box['width'],24)
        for key,unit in (('gatewaycpu','decimal'),('gatewaymemory','bytes'),('hostcpu','percent')):
            self.assertEqual(main['panels'][key]['spec']['plugin']['spec']['yAxis']['format']['unit'],unit)
        for doc in documents.values():
            for panel in doc['spec']['panels'].values():
                if panel['spec']['plugin']['kind']=='TimeSeriesChart':
                    self.assertTrue(panel['spec']['display'].get('description'))

    def test_database_scope_is_independent_from_host_and_has_real_metrics(self):
        main=monitor.dashboards()['monitoring-dashboard.json']['spec']
        self.assertEqual([v['spec']['name'] for v in main['variables']],['host','response_class','database'])
        for key in ('dbconnections','dbsize','dbtransactions','dbwaiters','dbdeadlocks','dbtemp'):
            query=main['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['query']
            self.assertIn('host=~"$database"',query)
            self.assertNotIn('$host',query)
            self.assertNotIn('or vector(0)',query)



    def test_panel_titles_are_unique_for_accessible_navigation(self):
        for document in monitor.dashboards().values():
            names=[panel['spec']['display']['name'] for panel in document['spec']['panels'].values()]
            self.assertEqual(len(names),len(set(names)))

    def test_generated_assets_export_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            monitor.write(output)
            for file,expected in monitor.all_dashboards().items():
                self.assertEqual(json.loads((output/file).read_text()),expected)
            self.assertEqual(yaml.safe_load((output/'monitoring-rules.yaml').read_text()),monitor.rules())
        for file,expected in monitor.dashboards().items():
            for panel in expected['spec']['panels'].values():
                for link in panel['spec'].get('links',[]):
                    self.assertTrue(link['url'].startswith('/projects/praxis/dashboards/enmaas-'))

    def test_layout_does_not_overlap_after_an_incomplete_row(self):
        panels=[('left',{},12,10),('section',{},24,3),('small',{},12,5),('tall',{},12,8),('next',{},24,4)]
        rows=monitor.dashboard('test','test',panels)['spec']['layouts'][0]['spec']['items']
        for index,a in enumerate(rows):
            for b in rows[index+1:]:
                self.assertFalse(a['x']<b['x']+b['width'] and b['x']<a['x']+a['width'] and a['y']<b['y']+b['height'] and b['y']<a['y']+a['height'])

    def test_alerts_current_and_staleness_detects_cached_samples(self):
        panels=monitor.dashboards()['monitoring-dashboard.json']['spec']['panels']
        query=panels['firing']['spec']['queries'][0]['spec']['plugin']['spec']
        self.assertTrue(query['instant']);self.assertIn('alertstate="firing"',query['query'])
        self.assertNotIn('severity=',query['query'])
        self.assertTrue(panels['firing']['spec']['plugin']['spec']['pagination'])
        layout=monitor.dashboards()['monitoring-dashboard.json']['spec']['layouts'][0]['spec']['items']
        box=next(item for item in layout if item['content']['$ref'].endswith('/firing'))
        self.assertEqual(box['width'],24)
        self.assertGreaterEqual(box['height'],9)
        stale=next(rule for rule in monitor.rules()['groups'][0]['rules'] if rule['alert']=='EnmaasHostTelemetryStale')
        self.assertIn('max_over_time(enmaas_host_sample_unixtime_seconds{host!=""}[24h])',stale['expr'])
        self.assertIn('absent(',stale['expr'])





    def test_resource_details_filter_hosts_and_services(self):
        docs=monitor.dashboards()
        for name in ('host','components'):
            spec=docs[name+'-dashboard.json']['spec']
            variables={v['spec']['name']:v['spec'] for v in spec['variables']}
            self.assertIn('host',variables)
            self.assertTrue(variables['host']['allowMultiple'])
            if name=='components':self.assertIn('component',variables)
            for panel in spec['panels'].values():
                for query in panel['spec'].get('queries',[]):
                    expr=query['spec']['plugin']['spec']['query']
                    self.assertIn('host=~"$host"',expr)
                    if name=='components':self.assertIn('component=~"$component"',expr)
                    self.assertEqual(panel['spec']['plugin']['spec']['legend']['position'],'right')

    def test_service_replicas_keep_distinct_bounded_identity(self):
        rows=[{'name':'enmaas-test-provider--'+replica,'cpu_time':'1s','mem_usage':'1MB / 512MiB','pids':'2'} for replica in ('one','two')]
        self.assertEqual(component.inventory([r['name'] for r in rows]+['foreign-provider','enmaas-test-private-helper'],'enmaas-test'),[r['name'] for r in rows])
        with self.assertRaises(ValueError):component.inventory([],'enmaas-test')
        metrics=component.payload(rows,'enmaas-test',host_id='gateway')
        points=metrics['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['sum']['dataPoints']
        identities=[{a['key']:a['value']['stringValue'] for a in p['attributes']} for p in points]
        self.assertEqual([i['replica'] for i in identities],['one','two'])
        self.assertTrue(all(i['component']=='provider' and i['host']=='gateway' for i in identities))
        for invalid in ('enmaas-test-foreign--one','enmaas-test-provider--','enmaas-test-provider--a.b'):
            with self.assertRaises(ValueError):component.payload([dict(rows[0],name=invalid)],'enmaas-test')
        with self.assertRaises(ValueError):component.payload([rows[0],rows[0]],'enmaas-test')

    def test_component_hosts_and_unlimited_memory(self):
        row={'name':'enmaas-test-db','cpu_time':'2s','mem_usage':'12MB / 0B','pids':'8'}
        for alias in ('gateway-1','gateway-2'):
            metrics=component.payload([row],'enmaas-test',host_id=alias)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
            self.assertEqual(metrics[2]['gauge']['dataPoints'],[])
            for metric in metrics:
                for point in metric.get('gauge',metric.get('sum',{}))['dataPoints']:
                    self.assertIn({'key':'host','value':{'stringValue':alias}},point['attributes'])
        with self.assertRaises(ValueError):component.payload([row],'enmaas-test',host_id='https://private.example')

    def test_component_conversion_and_foreign_rejection(self):
        row={'name':'enmaas-test-db','cpu_time':'1m2.5s','mem_usage':'12.5MB / 512MiB','pids':'8'}
        metrics=component.payload([row],'enmaas-test',now=1000)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        self.assertEqual(metrics[0]['sum']['dataPoints'][0]['asDouble'],62.5)
        self.assertEqual(metrics[1]['gauge']['dataPoints'][0]['asDouble'],12500000)
        self.assertNotIn('enmaas-test-db',str(metrics))
        for rows in ([dict(row,name='foreign-db')],[row,row],[dict(row,mem_usage='999GB / 512MiB')],[dict(row,pids='-1')]):
            with self.assertRaises(ValueError):component.payload(rows,'enmaas-test')
        for text in ('nan','-1s','1sBAD','1.2'):
            with self.assertRaises(ValueError):component.duration(text)

if __name__=='__main__':unittest.main()
