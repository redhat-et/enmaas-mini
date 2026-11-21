"""Worker credentials stay off argv and cleanup requires fixture ownership."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import unittest
import parallel_generator as workers


class WorkerCommand(unittest.TestCase):
    def test_restricted_launch_has_only_public_ca_secret_and_no_credential_argv(self):
        name=workers.lab.POD+'-generator-abcd1234'
        with patch.object(workers,'image_tag',return_value='test-image'):
            args=workers.command(name,'test-image',512)
        self.assertIn('--read-only',args)
        self.assertIn('no-new-privileges',args)
        self.assertEqual(args[args.index('--memory')+1],'512m')
        self.assertEqual(args[args.index('--pod')+1],workers.lab.POD)
        self.assertEqual(args.count('--secret'),1)
        self.assertIn('target=/fixture/ca.pem',args[args.index('--secret')+1])
        for forbidden in ('--privileged','--env','--volume','--network'):
            self.assertNotIn(forbidden,args)
        with patch.object(workers,'image_tag',return_value='test-image'):
            for bad in (0,8192,True):
                with self.assertRaises(ValueError):workers.command(name,'test-image',bad)
            with self.assertRaises(ValueError):workers.command(name,'wrong-image',512)


class WorkerCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_create_waits_for_engine_before_cleanup(self):
        entered=asyncio.Event();release=asyncio.Event();finished=[]
        async def engine(*args):
            entered.set();await release.wait();finished.append(True)
            return 0,b'container-id'
        with patch.object(workers,'podman',side_effect=engine):
            task=asyncio.create_task(workers.create_owned(['podman','create','fixture']))
            await entered.wait();task.cancel();await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(finished,[True])


    async def test_cleanup_refuses_foreign_labels_or_unknown_state(self):
        for replies in ([(2,b'')],[(0,b''),(0,json.dumps([{'Config':{'Labels':{}}}]).encode())]):
            with patch.object(workers,'podman',new=AsyncMock(side_effect=replies)) as command:
                with self.assertRaises(RuntimeError):await workers.cleanup('fixture-name')
                self.assertFalse(any(call.args[0]=='rm' for call in command.call_args_list))

    async def test_cleanup_records_exit_evidence_before_owned_removal(self):
        record={'Config':{'Labels':{'io.enmaas.test-lab':workers.lab.INSTANCE}},
                'State':{'Running':False,'OOMKilled':True,'ExitCode':137},'HostConfig':{'Memory':512*1024**2}}
        with patch.object(workers,'podman',new=AsyncMock(side_effect=[(0,b''),(0,json.dumps([record]).encode()),(0,b'')])) as command:
            evidence=await workers.cleanup('fixture-name')
        self.assertTrue(evidence['state']['OOMKilled'])
        self.assertEqual(command.call_args.args,('rm','--force','fixture-name'))

    async def test_worker_protocol_failures_cleanup_without_exporting_credentials(self):
        request=SimpleNamespace(minimum=10,maximum=120,definition=SimpleNamespace(
            keys=['private-test-key'],body=b'{"model":"fixture"}',profiles=[{}],user_agents=['test']))
        allocation={'start':0,'end':1,'concurrency':1,'offered_rps':1}
        for wire in (b'{bad-json',b'{"kind":"failure","data":{"type":"ValueError"}}\n',b''):
            process=MagicMock(returncode=1)
            process.stdin.drain=AsyncMock();process.stdout.readline=AsyncMock(return_value=wire)
            process.wait=AsyncMock(return_value=1)
            evidence=[]
            with patch.object(workers,'image_tag',return_value='test-image'),patch.object(workers,'create_owned',new=AsyncMock()),patch.object(workers.asyncio,'create_subprocess_exec',new=AsyncMock(return_value=process)) as spawn,patch.object(workers,'cleanup',new=AsyncMock(return_value={'removed':True})) as cleanup:
                with self.assertRaises((ValueError,RuntimeError)):
                    await workers.single(request,allocation,image='test-image',memory_mib=512,duration=1,
                        timeout=150,stop=asyncio.Event(),sample=lambda value:None,evidence=evidence)
                cleanup.assert_awaited_once()
                self.assertNotIn('private-test-key',str(spawn.call_args))
                self.assertIn(b'private-test-key',process.stdin.write.call_args.args[0])
                self.assertEqual(evidence,[{'removed':True}])

    async def test_failed_worker_cancels_and_joins_siblings(self):
        cancelled=[]
        async def child(request,allocation,**kwargs):
            if allocation['start']==0:
                await asyncio.sleep(.02)
                raise RuntimeError('worker failed')
            try:await asyncio.sleep(60)
            finally:cancelled.append(True)
        request=SimpleNamespace(definition=SimpleNamespace(keys=['one','two']))
        owner=json.dumps([{'Labels':{'io.enmaas.test-lab':workers.lab.INSTANCE}}]).encode()
        with patch.object(workers,'podman',new=AsyncMock(return_value=(0,owner))),patch.object(workers,'single',side_effect=child):
            with self.assertRaisesRegex(RuntimeError,'worker failed'):
                await workers.run_phase(request,users=2,concurrency=4,offered_rps=10,workers=2,
                    duration=1,timeout=150,stop=asyncio.Event(),sample=lambda value:None,image='test-image')
        self.assertEqual(cancelled,[True])


if __name__=='__main__':unittest.main()
