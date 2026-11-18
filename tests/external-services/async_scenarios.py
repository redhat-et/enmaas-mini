"""Bounded asynchronous load; offered slots never accumulate into a replay queue."""
import asyncio
import errno
from collections import Counter
import math
import time

BUCKETS = (.01, .05, .1, .5, 1, 2, 5, 10, 30, 60, 90, 120, 180)


async def run_phase(request, *, users, concurrency, duration, offered_rps,
                    timeout, stop=None, sample=None):
    for value, maximum in ((users, 2500), (concurrency, 40000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Invalid bounded population or concurrency')
    for value, maximum in ((duration, 3600), (offered_rps, 10000), (timeout, 180)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError('Invalid bounded duration, rate or deadline')
    stop = stop if stop is not None else asyncio.Event()
    started = time.monotonic()
    deadline = started + duration
    pending = set()
    statuses, errors, os_errors = Counter(), Counter(), Counter()
    attempts, successes = [0]*users, [0]*users
    latency, first_bytes = [0]*(len(BUCKETS)+1), [0]*(len(BUCKETS)+1)
    stats = dict(offered_slots=0, dispatched=0, skipped_concurrency=0,
                 skipped_scheduler=0, peak_in_flight=0, response_bytes=0, request_bytes=0, stream_events=0)
    request_seconds = 0.

    def active_requests():
        # Done callbacks can lag completed outcomes; pending also owns those tasks.
        return stats['dispatched']-sum(statuses.values())

    def bucket(histogram, value):
        histogram[next((i for i, bound in enumerate(BUCKETS) if value <= bound), len(BUCKETS))] += 1

    async def execute(user):
        nonlocal request_seconds
        begin = time.monotonic()
        reply = None
        try:
            async with asyncio.timeout(timeout):
                reply = await request(user, timeout)
            status = reply.get('status')
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError('Invalid HTTP status')
        except Exception as error:
            # Exception text can contain credentials, response bodies or URLs.
            errors[type(error).__name__] += 1
            cause=getattr(error,'os_error',error)
            number=getattr(cause,'errno',None) if isinstance(cause,OSError) else None
            if type(number) is int and number in errno.errorcode:
                os_errors[errno.errorcode[number]] += 1
            status = 0
        elapsed = time.monotonic()-begin
        request_seconds += elapsed
        statuses[str(status)] += 1
        attempts[user] += 1
        successes[user] += int(status == 200)
        bucket(latency, elapsed)
        if isinstance(reply, dict):
            first, size = reply.get('first_byte_seconds'), reply.get('bytes', 0)
            if type(first) in (int, float) and math.isfinite(first) and 0 <= first <= elapsed:
                bucket(first_bytes, first)
            if type(size) is int and size >= 0:
                stats['response_bytes'] += size
            for key in ('request_bytes','stream_events'):
                value = reply.get(key,0)
                if type(value) is int and value >= 0:stats[key] += value

    total = int(duration*offered_rps)
    last_sample = started-1
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            now = time.monotonic()
            due = min(total, int((now-started)*offered_rps)+1)-stats['offered_slots']
            if due > 0:
                stats['offered_slots'] += due
                # A delayed event loop must not replay a large overdue burst.
                ready = min(due, 100)
                stats['skipped_scheduler'] += due-ready
                admitted = min(ready, concurrency-active_requests())
                stats['skipped_concurrency'] += ready-admitted
                for _ in range(admitted):
                    user = stats['dispatched'] % users
                    task = asyncio.create_task(execute(user))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    stats['dispatched'] += 1
                stats['peak_in_flight'] = max(stats['peak_in_flight'], active_requests())
            if sample is not None and now-last_sample >= 1:
                sample(dict(stats, statuses=dict(statuses), errors=dict(errors), os_errors=dict(os_errors), active_requests=active_requests(), completed=sum(statuses.values()),
                            elapsed_seconds=now-started))
                last_sample = now
            await asyncio.sleep(min(.01, max(0, deadline-time.monotonic())))
        admission_end = time.monotonic()
        if not stop.is_set():
            missed = total-stats['offered_slots']
            stats['offered_slots'] += missed
            stats['skipped_scheduler'] += missed
        # Each admitted request has an independently enforced total deadline.
        while pending:
            await asyncio.wait(tuple(pending),timeout=1)
            if sample is not None:
                sample(dict(stats,statuses=dict(statuses),errors=dict(errors), os_errors=dict(os_errors),active_requests=active_requests(),completed=sum(statuses.values()),
                            elapsed_seconds=time.monotonic()-started))
        if sample is not None:
            sample(dict(stats,statuses=dict(statuses),errors=dict(errors), os_errors=dict(os_errors),active_requests=0,completed=sum(statuses.values()),
                        elapsed_seconds=time.monotonic()-started))
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*tuple(pending), return_exceptions=True)
    ended = time.monotonic()
    return dict(stats, users=users, max_in_flight=concurrency, offered_rps=offered_rps,
                target_seconds=duration, admission_seconds=admission_end-started,
                elapsed_seconds=ended-started, drain_seconds=ended-admission_end,
                interrupted=stop.is_set(), statuses=dict(statuses), errors=dict(errors), os_errors=dict(os_errors),
                attempts=sum(statuses.values()), per_user_attempts=attempts,
                per_user_successes=successes, latency_bucket_upper_seconds=[*BUCKETS, None],
                latency_bucket_counts=latency, first_byte_bucket_counts=first_bytes,
                mean_in_flight=request_seconds/(ended-started),
                completed_rps=sum(statuses.values())/(ended-started))
