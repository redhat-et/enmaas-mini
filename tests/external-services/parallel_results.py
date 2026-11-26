"""Bounded worker partitioning and aggregation without credentials or deployment I/O."""
from collections import Counter
import math
import re

COUNTERS=('offered_slots','dispatched','skipped_concurrency','skipped_scheduler',
          'response_bytes','request_bytes','stream_events')


def partition(*, users, concurrency, offered_rps, workers):
    for value, maximum in ((users,2500),(concurrency,40000),(offered_rps,10000),(workers,12)):
        if type(value) is not int or not 1<=value<=maximum:
            raise ValueError('Invalid bounded worker allocation')
    if workers>min(users,concurrency,offered_rps):
        raise ValueError('Every worker needs users, concurrency and offered traffic')
    return [{'start':users*i//workers,'end':users*(i+1)//workers,
             'concurrency':concurrency//workers+int(i<concurrency%workers),
             'offered_rps':offered_rps//workers+int(i<offered_rps%workers)} for i in range(workers)]


def nonnegative(value):
    return type(value) is int and value>=0


def finite(value, minimum=0):
    return type(value) in (int,float) and math.isfinite(value) and value>=minimum


def validate(row):
    for key in (*COUNTERS,'attempts','users','max_in_flight','peak_in_flight'):
        if not nonnegative(row.get(key)):raise ValueError('Invalid worker counter')
    partition(users=row['users'],concurrency=row['max_in_flight'],offered_rps=row['offered_rps'],workers=1)
    for key in ('started_monotonic','elapsed_seconds','admission_seconds','target_seconds','mean_in_flight'):
        if not finite(row.get(key)):raise ValueError('Invalid worker timing')
    if not 0<row['target_seconds']<=3600 or not 0<row['elapsed_seconds']<=3785:
        raise ValueError('Worker timing outside allowed bounds')
    if row['admission_seconds']>row['elapsed_seconds'] or row['peak_in_flight']>row['max_in_flight'] or row['mean_in_flight']>row['max_in_flight']:
        raise ValueError('Invalid worker concurrency or drain')
    if type(row.get('interrupted')) is not bool:raise ValueError('Missing cancellation evidence')
    for key in ('statuses','errors','os_errors'):
        values=row.get(key)
        if not isinstance(values,dict) or len(values)>256 or any(
                not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,128}',name)
                or not nonnegative(value) for name,value in values.items()):
            raise ValueError('Invalid worker outcome counters')
    if any(status!='0' and not re.fullmatch(r'[1-5][0-9]{2}',status) for status in row['statuses']):
        raise ValueError('Invalid worker HTTP status')
    attempts=row['attempts']
    if attempts!=row['dispatched'] or attempts!=sum(row['statuses'].values()):
        raise ValueError('Incomplete worker outcomes')
    if sum(row['errors'].values())!=row['statuses'].get('0',0):
        raise ValueError('Inconsistent transport-error counts')
    if row['offered_slots']!=row['dispatched']+row['skipped_concurrency']+row['skipped_scheduler']:
        raise ValueError('Worker admission counts do not reconcile')
    for key, count in (('per_user_attempts',attempts),('per_user_successes',row['statuses'].get('200',0))):
        values=row.get(key)
        if not isinstance(values,list) or len(values)!=row['users'] or not all(map(nonnegative,values)) or sum(values)!=count:
            raise ValueError('Worker user counts do not reconcile')
    if any(s>a for s,a in zip(row['per_user_successes'],row['per_user_attempts'])):
        raise ValueError('Worker user successes exceed attempts')
    buckets=row.get('latency_bucket_upper_seconds')
    if not isinstance(buckets,list) or not 2<=len(buckets)<=64 or buckets[-1] is not None or any(
            not finite(value,0.000001) for value in buckets[:-1]) or sorted(set(buckets[:-1]))!=buckets[:-1]:
        raise ValueError('Invalid histogram boundaries')
    for key in ('latency_bucket_counts','first_byte_bucket_counts'):
        values=row.get(key)
        if not isinstance(values,list) or len(values)!=len(buckets) or not all(map(nonnegative,values)):
            raise ValueError('Invalid worker histogram')
        if sum(values)>attempts or key=='latency_bucket_counts' and sum(values)!=attempts:
            raise ValueError('Worker histogram does not reconcile')


def merge(rows, *, sampled_peak=0):
    if not isinstance(rows,list) or not 1<=len(rows)<=12:raise ValueError('Invalid worker result count')
    for row in rows:validate(row)
    if len({row['target_seconds'] for row in rows})!=1 or any(
            row['latency_bucket_upper_seconds']!=rows[0]['latency_bucket_upper_seconds'] for row in rows):
        raise ValueError('Workers disagree about phase duration or histogram boundaries')
    users=sum(row['users'] for row in rows)
    concurrency=sum(row['max_in_flight'] for row in rows)
    rate=sum(row['offered_rps'] for row in rows)
    partition(users=users,concurrency=concurrency,offered_rps=rate,workers=len(rows))
    begin=min(row['started_monotonic'] for row in rows)
    end=max(row['started_monotonic']+row['elapsed_seconds'] for row in rows)
    admission_end=max(row['started_monotonic']+row['admission_seconds'] for row in rows)
    elapsed=end-begin
    result={key:sum(row[key] for row in rows) for key in COUNTERS}
    for key in ('statuses','errors','os_errors'):
        counter=Counter()
        for row in rows:counter.update(row[key])
        result[key]=dict(counter)
    for key in ('per_user_attempts','per_user_successes'):
        result[key]=[value for row in rows for value in row[key]]
    for key in ('latency_bucket_counts','first_byte_bucket_counts'):
        result[key]=[sum(values) for values in zip(*(row[key] for row in rows))]
    upper=sum(row['peak_in_flight'] for row in rows)
    if not nonnegative(sampled_peak) or sampled_peak>upper:raise ValueError('Invalid sampled concurrency peak')
    result.update(users=users,max_in_flight=concurrency,offered_rps=rate,
        target_seconds=rows[0]['target_seconds'],started_monotonic=begin,
        admission_seconds=admission_end-begin,elapsed_seconds=elapsed,drain_seconds=end-admission_end,
        interrupted=any(row['interrupted'] for row in rows),attempts=sum(row['attempts'] for row in rows),
        latency_bucket_upper_seconds=rows[0]['latency_bucket_upper_seconds'],
        mean_in_flight=sum(row['mean_in_flight']*row['elapsed_seconds'] for row in rows)/elapsed,
        completed_rps=sum(row['attempts'] for row in rows)/elapsed,
        peak_in_flight=sampled_peak,peak_in_flight_upper_bound=upper,
        peak_measurement='Latest worker snapshots; sum of individual maxima is a separate upper bound',
        generator_workers=len(rows),worker_start_skew_seconds=max(row['started_monotonic'] for row in rows)-begin)
    return result
