"""Owned-pod load worker: protected input on stdin, counters only on stdout."""
import asyncio
import json
import sys

from async_scenarios import run_phase
from async_transport import AsyncTransport
from parallel_results import partition

MAX_INPUT=4*1024*1024


def parse(raw):
    if len(raw)>MAX_INPUT:raise ValueError('Worker input exceeds bound')
    document=json.loads(raw)
    if not isinstance(document,dict) or set(document)!={'port','keys','model','profiles','user_agents','phase'}:
        raise ValueError('Invalid worker input')
    port=document['port']
    if type(port) is not int or not 28443<=port<=58443:
        raise ValueError('Only the owned local gateway port is accepted')
    keys=document['keys'];phase=document['phase']
    if not isinstance(keys,list) or not isinstance(phase,dict) or set(phase)!={
            'concurrency','duration','offered_rps','minimum_seconds','maximum_seconds','timeout'}:
        raise ValueError('Invalid worker phase')
    partition(users=len(keys),concurrency=phase['concurrency'],offered_rps=phase['offered_rps'],workers=1)
    if any(type(phase[key]) is not int for key in ('duration','minimum_seconds','maximum_seconds','timeout')) or not (
            1<=phase['duration']<=3600 and 10<=phase['minimum_seconds']<=phase['maximum_seconds']<=120
            and phase['maximum_seconds']<phase['timeout']<=180):
        raise ValueError('Invalid worker deadlines')
    return document


def emit(kind, data):
    print(json.dumps({'kind':kind,'data':data},allow_nan=False),flush=True)


async def execute(document):
    phase=document['phase']
    async with AsyncTransport('https://localhost:'+str(document['port']),'/fixture/ca.pem',
            document['keys'],document['model'],profiles=document['profiles'],
            user_agents=document['user_agents'],concurrency=phase['concurrency'],
            minimum_seconds=phase['minimum_seconds'],maximum_seconds=phase['maximum_seconds']) as request:
        result=await run_phase(request,users=len(document['keys']),concurrency=phase['concurrency'],
            duration=phase['duration'],offered_rps=phase['offered_rps'],timeout=phase['timeout'],
            sample=lambda sample:emit('sample',sample))
    emit('result',result)


if __name__=='__main__':
    try:
        asyncio.run(execute(parse(sys.stdin.buffer.readline(MAX_INPUT+1))))
    except Exception as error:
        # Never export exception text, URLs, keys, profiles or response bodies.
        emit('failure',{'type':type(error).__name__})
        raise SystemExit(1) from None
