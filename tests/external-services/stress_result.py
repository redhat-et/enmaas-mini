"""Validate staircase evidence without confusing offered and completed traffic."""
import math
from stress_plan import escalation_stop, plan


def check_report(summary,cleanup):
    if summary.get('mock_only') is not True or summary.get('ledger_verified') is not True:
        raise ValueError('Require a mock run with exact accounting reconciliation')
    if cleanup.get('complete') is not True or cleanup.get('errors'):
        raise ValueError('Require complete restoration')
    maximum=summary.get('requested_max_rate')
    if type(maximum) is not int or maximum not in (10,50,100,150,200,250,500,750,1000,10000):raise ValueError('Missing requested maximum rate')
    target=summary.get('phase_duration_seconds')
    if type(target) is not int or not 1<=target<=3600:raise ValueError('Invalid planned phase duration')
    phases=summary.get('phases',[])
    if len(phases)<2 or phases[-1].get('name')!='stress_recovery':raise ValueError('Recovery is required')
    expected=[rate for rate in (10,100,1000,10000) if rate<=maximum]
    planned=summary.get('planned_phases')
    if planned is not None:
        if not isinstance(planned,list) or len(planned)<2 or planned[-1].get('name')!='stress_recovery' or planned[-1].get('offered_rps')!=10:
            raise ValueError('Invalid planned staircase')
        rates=[p.get('offered_rps') for p in planned[:-1]]
        if not rates or any(type(rate) is not int for rate in rates):
            raise ValueError('Invalid planned rates')
        allowed=[[row['offered_rps'] for row in plan(profile=profile)[:-1]
                  if rates[0]<=row['offered_rps']<=maximum]
                 for profile in ('decade','gradual','extended','full','higher')]
        if rates not in allowed or [p.get('name') for p in planned[:-1]]!=['stress_'+str(rate) for rate in rates]:
            raise ValueError('Invalid planned load order')
        expected=rates
    loads=phases[:-1]
    if [p.get('name') for p in loads]!=['stress_'+str(rate) for rate in expected[:len(loads)]]:
        raise ValueError('Unexpected or skipped load stage')
    reasons=[]
    for index,phase in enumerate(phases):
        expected_rate=10 if index==len(phases)-1 else expected[index]
        if phase.get('offered_rps')!=expected_rate:raise ValueError('Offered rate differs from planned stage')
        if phase.get('telemetry_verified') is not True or phase.get('interrupted'):
            raise ValueError('Incomplete phase or missing telemetry')
        admission=phase.get('admission_seconds')
        if type(admission) not in (int,float) or not math.isfinite(admission):raise ValueError('Invalid admission duration')
        if phase.get('target_seconds')!=target or admission<target-.5:
            raise ValueError('Phase did not sustain planned duration')
        for key in ('offered_slots','dispatched','attempts','skipped_concurrency','skipped_scheduler'):
            if type(phase.get(key)) is not int or phase[key]<0:raise ValueError('Invalid admission evidence')
        if phase['offered_slots']!=target*expected_rate:raise ValueError('Offered slots do not match the sustained target')
        statuses=phase.get('statuses',{})
        if not statuses or any(type(v) is not int or v<0 for v in statuses.values()):raise ValueError('Invalid status counts')
        if phase['dispatched']!=phase['attempts'] or phase['attempts']!=sum(statuses.values()):raise ValueError('Unfinished requests')
        if phase['offered_slots']!=phase['dispatched']+phase['skipped_concurrency']+phase['skipped_scheduler']:
            raise ValueError('Admissions do not reconcile')
        for key in ('peak_in_flight','max_in_flight'):
            if type(phase.get(key)) is not int or not 1<=phase[key]<=40000:raise ValueError('Invalid concurrency evidence')
        if phase['peak_in_flight']>phase['max_in_flight']:raise ValueError('Concurrency exceeded cap')
        reasons.append(escalation_stop(phase))
    if reasons[-1] is not None:raise ValueError('Recovery was not healthy')
    stop=reasons[-2]
    if any(reasons[:-2]):raise ValueError('Escalation continued after a detected limit')
    if stop!=summary.get('escalation_stop'):raise ValueError('Stop reason differs from measured evidence')
    if len(loads)!=len(expected) and not stop:raise ValueError('Missing requested load stages without a measured stop')
    return {'outcome':'bounded_limit_observed' if stop else 'requested_stages_completed',
            'stop_reason':stop,'highest_offered_rps':loads[-1]['offered_rps'],
            'planned_phase_seconds':target,'sustained_ten_minute_phases':target>=600,
            'successful_requests':sum(p['statuses'].get('200',0) for p in phases),
            'ledger_verified':True,'cleanup_complete':True,
            'note':'Offered targets are not achieved completion rates; inspect each phase including drain time.'}
