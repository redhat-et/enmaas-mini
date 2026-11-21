"""Bounded capacity staircase; never label an offered target as measured capacity."""


def plan(*, duration=600, concurrency=5000, profile="decade"):
    if type(duration) is not int or not 1 <= duration <= 3600:
        raise ValueError('Duration must be 1–3600 seconds')
    if type(concurrency) is not int or not 1 <= concurrency <= 40000:
        raise ValueError('Concurrency must be 1–40000')
    if profile not in ('decade','gradual','extended','full','higher'):raise ValueError('Unknown staircase profile')
    rates=((10,25),(100,250),(1000,2500),(10000,2500),(10,25)) if profile=='decade' else tuple((rate,2500) for rate in (10,50,100,150,10))
    if profile=='extended':rates=tuple((rate,2500) for rate in (100,150,200,250,10))
    if profile=='full':rates=tuple((rate,2500) for rate in (10,50,100,150,200,250,10))
    if profile=='higher':rates=tuple((rate,2500) for rate in (500,750,1000,10))
    rows=[]
    for rate, users in rates:
        rows.append({'name':'stress_recovery' if len(rows)==len(rates)-1 else 'stress_'+str(rate),
                     'offered_rps':rate,'users':users,'duration':duration,
                     'concurrency':min(concurrency,rate*120) if profile=='decade' else concurrency,
                     'rate_limit_rps':max(1000,rate*2),
                     'minimum_response_seconds':10,'maximum_response_seconds':120})
    return rows


def escalation_stop(result):
    """Stop raising load on errors or more than 1% missed offered admissions.

    Recovery is still required. This classification does not imply that the
    service's maximum capacity was reached: the generator can be the bottleneck.
    """
    if result.get('errors'):return 'request_errors'
    statuses=result.get('statuses',{})
    if any(int(status)>=500 and count for status,count in statuses.items()):return 'service_errors'
    if statuses.get('429',0):return 'unexpected_policy_denials'
    if any(status!='200' and count for status,count in statuses.items()):return 'unexpected_http_status'
    offered=result.get('offered_slots',0)
    if offered<=0:return 'no_offered_traffic'
    if result.get('skipped_scheduler',0)/offered>.01:return 'generator_scheduler_limit'
    if result.get('skipped_concurrency',0)/offered>.01:return 'generator_concurrency_limit'
    return None


def select_phases(rows, *, minimum=10, maximum=10000):
    """Select a bounded offered-load interval, always retaining recovery."""
    if (type(minimum) is not int or type(maximum) is not int or
            not 1 <= minimum <= maximum <= 10000):
        raise ValueError('Invalid offered-rate interval')
    selected=[row for row in rows if row['name']=='stress_recovery' or
              minimum <= row['offered_rps'] <= maximum]
    if not any(row['name']!='stress_recovery' for row in selected):
        raise ValueError('No workload stage in the selected offered-rate interval')
    return selected
