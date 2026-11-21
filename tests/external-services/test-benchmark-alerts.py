#!/usr/bin/env python3
"""Qualify generator alerts independently of operational alert definitions."""
import importlib.util
from pathlib import Path
import signal
import sys
import benchmark_monitoring as monitoring

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('operational_rule_tests',HERE/'test-alert-rules.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


def suite():
    tests=[]
    def case(name,series,checks):
        tests.append({'name':name,'interval':'15s','input_series':series,'alert_rule_test':checks})
    def sample(name,values):return {'series':name,'values':values}
    def check(name,time,firing):
        rule=next(rule for group in monitoring.rules()['groups'] for rule in group['rules'] if rule['alert']=='Enmaas'+name)
        return {'eval_time':time,'alertname':'Enmaas'+name,'exp_alerts':[
            {'exp_labels':rule['labels'],'exp_annotations':rule['annotations']}] if firing else []}
    for name,metric in (('GeneratorConcurrencyLimited','concurrency'),('GeneratorSchedulerLimited','scheduler')):
        case(name+' fires only on fresh skipped admissions',[
          sample('enmaas_stress_skipped_'+metric,'10+0x40'),
          sample('enmaas_stress_offered_slots','100+0x40'),
          sample('enmaas_stress_sample_unixtime_seconds','0+15x16 240+0x24')],
          [check(name,'2m',True),check(name,'6m',False)])
        case(name+' clears when a recovery phase has no skips',[
          sample('enmaas_stress_skipped_'+metric,'10+0x16 0+0x24'),
          sample('enmaas_stress_offered_slots','100+0x40'),
          sample('enmaas_stress_sample_unixtime_seconds','0+15x40')],
          [check(name,'2m',True),check(name,'6m',False)])
    for description,completed,successful,fresh,firing in (
        ('failed terminal outcomes','1000+0x40','800+0x40',True,True),
        ('healthy terminal outcomes','1000+0x40','1000+0x40',True,False),
        ('too few outcomes','50+0x40','0+0x40',True,False),
        ('stale failed outcomes','1000+0x40','800+0x40',False,False),
        ('recovery resets outcomes','1000+0x16 100+0x24','800+0x16 100+0x24',True,False),
    ):
        case('benchmark reliability: '+description,[
          sample('enmaas_stress_completed',completed),
          sample('enmaas_stress_successful',successful),
          sample('enmaas_stress_active_requests','10000+0x40'),
          sample('enmaas_stress_sample_unixtime_seconds','0+15x40' if fresh else '0+0x40')],
          [check('BenchmarkFailuresHigh','6m',firing)])
    return {'rule_files':['/rules.yaml'],'evaluation_interval':'15s','fuzzy_compare':True,'tests':tests}


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    runner.main(ruleset=monitoring.rules(),cases=suite())
