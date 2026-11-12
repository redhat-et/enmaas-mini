import importlib.util
from pathlib import Path
import json
import signal
import unittest
from unittest.mock import Mock, patch, MagicMock

spec=importlib.util.spec_from_file_location('outage',Path(__file__).with_name('collector-outage.py'))
outage=importlib.util.module_from_spec(spec);spec.loader.exec_module(outage)


class CollectorOutage(unittest.TestCase):
    def test_interruption_guard_restores_handlers_and_protects_cleanup(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        for number in previous:
            with self.subTest(signal=number):
                with self.assertRaises(InterruptedError):
                    with outage.interruption_guard():
                        handler=signal.getsignal(number)
                        try:
                            handler(number, None)
                        finally:
                            # Repeated cancellation cannot interrupt service restoration.
                            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
                            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                for restored, handler in previous.items():
                    self.assertEqual(signal.getsignal(restored), handler)

    def test_recovery_requires_both_fresh_metrics_and_traces(self):
        metric=lambda value:{'data':{'result':[{'value':[200,str(value)]}]}}
        trace=lambda value:{'traces':[{'startTimeUnixNano':str(int(value*1e9))}]}
        self.assertTrue(outage.fresh_gateway_signals(metric(101),trace(101),100))
        self.assertFalse(outage.fresh_gateway_signals(metric(99),trace(101),100))
        self.assertFalse(outage.fresh_gateway_signals(metric(101),trace(99),100))
        self.assertFalse(outage.fresh_gateway_signals({},trace(101),100))
        self.assertFalse(outage.fresh_gateway_signals(metric(101),{},100))

    def test_rejects_unbounded_duration_before_inspecting_services(self):
        with patch.object(outage.bench.fixture,'run') as run:
            for seconds in (0,9,121):
                with self.assertRaises(ValueError):outage.main(seconds)
            run.assert_not_called()

    def test_inference_failure_restores_owned_collector_and_retires_test_user(self):
        lab=outage.bench.lab
        calls=[]
        def run(*args):
            calls.append(args)
            if args==('inspect',lab.POD+'-gateway-sso'):
                return json.dumps([{'Image':'test-image'}]).encode()
            if args==('inspect',lab.POD+'-collector'):
                return json.dumps([{'Id':'owned-id','State':{'Running':True},
                    'Config':{'Labels':{'io.enmaas.test-lab':lab.INSTANCE}}}]).encode()
            if args==('inspect','owned-id'):
                return b'[{"State":{"Running":false}}]'
            return b''
        controller=Mock();controller.telemetry=True;controller.cleanup.return_value=True
        transport=MagicMock();transport.__enter__.return_value=Mock(return_value={'status':503})
        controller.transport.return_value=transport
        with patch.object(outage.bench.fixture,'run',side_effect=run), \
             patch.object(outage.bench,'LocalController',return_value=controller), \
             patch.object(lab,'check') as check:
            with self.assertRaisesRegex(RuntimeError,'Inference failed'):outage.main(10)
        self.assertIn(('stop','--time','5','owned-id'),calls)
        self.assertIn(('start','owned-id'),calls)
        controller.cleanup.assert_called_once()
        check.assert_called_once()


if __name__=='__main__':unittest.main()
