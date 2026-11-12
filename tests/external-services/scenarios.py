#!/usr/bin/env python3
"""Bounded deadline-driven traffic scheduling, independent of deployment/transport.

The request callback receives (user_index, timeout_seconds) and MUST honor that
wall-clock timeout. The deployment driver owns credentials, mock-only validation,
policy changes, resource guardrails and accounting reconciliation. This module
never connects to a service or changes a policy on its own.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import math
import threading
import time

# Fixed buckets keep memory independent of traffic volume. These are histogram
# upper bounds, not exact percentiles. The final bucket counts longer requests.
LATENCY_BUCKETS = (.005, .01, .025, .05, .1, .25, .5, 1, 2, 5, 10, 30, 60)


def active_at(elapsed, active, idle):
    return active is None or elapsed % (active + idle) < active


def run_phase(request, *, users, lanes, duration, offered_rps, timeout,
              active=None, idle=None, stop=None):
    """Pace admissions globally, keep user lanes separate and bound result memory.

A saturated generator does not accumulate missed slots or replay a catch-up
burst. Requests admitted before the deadline may finish afterwards; report both
admission time and drain time. Cancellation prevents new admissions, not abrupt
cancellation of in-flight requests whose accounting still needs reconciliation.
"""
    if (type(users) is not int or type(lanes) is not int or not 1 <= users <= 100
            or not 1 <= lanes <= 32 or users*lanes > 512):
        raise ValueError('require 1–100 users, 1–32 lanes/user, at most 512 lanes')
    for value, maximum in ((duration, 3600), (offered_rps, 2000), (timeout, 60)):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError('duration, offered rate and timeout must be finite and bounded')
    if (active is None) != (idle is None):
        raise ValueError('active and idle durations must be supplied together')
    if active is not None and any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                                  for v in (active, idle)):
        raise ValueError('burst durations must be finite and positive')
    stop = stop if stop is not None else threading.Event()
    lock = threading.Lock()
    ready = threading.Barrier(users*lanes+1)
    timing = {}
    statuses, errors = Counter(), Counter()
    per_user = [0]*users
    successful_per_user = [0]*users
    latencies = [0]*(len(LATENCY_BUCKETS)+1)
    first_bytes = [0]*(len(LATENCY_BUCKETS)+1)
    completed_bytes = 0

    def bucket(values, seconds):
        index = next((i for i, bound in enumerate(LATENCY_BUCKETS) if seconds <= bound), len(LATENCY_BUCKETS))
        values[index] += 1

    def worker(user):
        nonlocal completed_bytes
        ready.wait()
        while not stop.is_set():
            now = time.monotonic()
            if now >= timing['deadline']:
                return
            if not active_at(now-timing['start'], active, idle):
                remaining = active+idle-(now-timing['start']) % (active+idle)
                stop.wait(min(remaining, timing['deadline']-now))
                continue
            with lock:
                slot = max(now, timing['next'])
                timing['next'] = slot+1/offered_rps
            if slot >= timing['deadline']:
                return
            if stop.wait(max(0, slot-time.monotonic())):
                return
            started = time.monotonic()
            if started >= timing['deadline'] or not active_at(started-timing['start'], active, idle):
                continue
            error = None
            result = {}
            try:
                result = request(user, timeout)
                status = result.get('status')
                if type(status) is not int or not 100 <= status <= 599:
                    raise ValueError('request callback returned an invalid HTTP status')
            except Exception as exc:
                # Never retain exception messages: HTTP exceptions can contain
                # URLs, headers, credentials or response bodies.
                error = type(exc).__name__
                status = 0
                stop.set()
            elapsed = time.monotonic()-started
            with lock:
                statuses[str(status)] += 1
                per_user[user] += 1
                successful_per_user[user] += int(status == 200)
                if error:
                    errors[error] += 1
                bucket(latencies, elapsed)
                first = result.get('first_byte_seconds') if isinstance(result, dict) else None
                if isinstance(first, (int, float)) and math.isfinite(first) and 0 <= first <= elapsed:
                    bucket(first_bytes, first)
                size = result.get('bytes', 0) if isinstance(result, dict) else 0
                if type(size) is int and size >= 0:
                    completed_bytes += size

    with ThreadPoolExecutor(max_workers=users*lanes) as pool:
        futures = [pool.submit(worker, user) for user in range(users) for _ in range(lanes)]
        timing['start'] = time.monotonic()
        timing['deadline'] = timing['start']+duration
        timing['next'] = timing['start']
        ready.wait()
        stop.wait(max(0, timing['deadline']-time.monotonic()))
        admission_ended = time.monotonic()
        for future in futures:
            future.result()
    ended = time.monotonic()
    return {
        'users': users, 'streams_per_user': lanes, 'offered_rps': offered_rps,
        'target_seconds': duration, 'active_seconds': active, 'idle_seconds': idle,
        'admission_seconds': admission_ended-timing['start'],
        'elapsed_seconds': ended-timing['start'], 'drain_seconds': ended-admission_ended,
        'interrupted': stop.is_set(), 'attempts': sum(statuses.values()),
        'statuses': dict(statuses), 'errors': dict(errors), 'per_user_attempts': per_user,
        'per_user_successes': successful_per_user,
        'response_bytes': completed_bytes, 'latency_bucket_upper_seconds': list(LATENCY_BUCKETS)+[None],
        'latency_bucket_counts': latencies, 'first_byte_bucket_counts': first_bytes,
        'completed_rps': sum(statuses.values())/(ended-timing['start']),
    }
