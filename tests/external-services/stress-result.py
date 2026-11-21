#!/usr/bin/env python3
"""Verify a local staircase report and its observed generator-limit alerts."""
import argparse
import importlib.util
import json
from pathlib import Path
import urllib.parse
import signals
from stress_result import check_report

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('monitor_result',HERE/'monitoring-result.py')
monitor=importlib.util.module_from_spec(spec);spec.loader.exec_module(monitor)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    report=args.report.resolve()
    if report.parent!=signals.lab.STATE.resolve() or not report.name.startswith('lab-run-'):
        raise ValueError('Report must belong to this local fixture')
    summary=json.loads((report/'summary.json').read_text())
    result=check_report(summary,json.loads((report/'cleanup.json').read_text()))
    result['healthy_rules']=len(monitor.check_rules(signals.query('http://localhost:9090/api/v1/rules')))
    names={'generator_concurrency_limit':'EnmaasGeneratorConcurrencyLimited',
           'generator_scheduler_limit':'EnmaasGeneratorSchedulerLimited'}
    reason=result['stop_reason']
    if reason in names:
        phase=summary['phases'][-2]
        expression='ALERTS{alertname="'+names[reason]+'",alertstate="firing"}'
        reply=signals.query('http://localhost:9090/api/v1/query_range?'+urllib.parse.urlencode({
            'query':expression,'start':monitor.stamp(phase['started_utc']),
            'end':monitor.stamp(phase['ended_utc']),'step':5})) or {}
        if not any(float(p[1])==1 for row in reply.get('data',{}).get('result',[]) for p in row.get('values',[])):
            raise ValueError('Measured generator limit had no observed firing alert')
        current=signals.query('http://localhost:9090/api/v1/query?'+urllib.parse.urlencode({'query':expression})) or {}
        if current.get('status')!='success' or current.get('data',{}).get('result'):
            raise ValueError('Generator alert did not recover')
        result['generator_alert_fired_and_recovered']=True
    capacity=monitor.capacity(summary['phases'])
    capacity['qualification_passed']=True
    capacity['qualification_scope']=result['outcome']
    signals.lab.write(report,'capacity-summary.json',capacity)
    signals.lab.write(report,'stress-summary.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
