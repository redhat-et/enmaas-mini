import unittest
from stress_signals import payload,FIELDS

class StressSignals(unittest.TestCase):
    def test_bounded_generator_metrics_and_freshness(self):
        data=payload(dict.fromkeys(FIELDS,10),512,now=1000000000)
        metrics=data['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        self.assertEqual(len(metrics),13)
        self.assertTrue(all(m['name'].startswith('enmaas_stress_') for m in metrics))
        self.assertEqual(metrics[-1]['gauge']['dataPoints'][0]['asDouble'],1)
        self.assertNotIn('user',str(data))
    def test_invalid_samples_rejected(self):
        for bad in (-1,float('nan'),True,100000001):
            row=dict.fromkeys(FIELDS,0);row['active_requests']=bad
            with self.assertRaises(ValueError):payload(row,512)

    def test_stream_metrics_are_bounded_and_statuses_have_no_identity_labels(self):
        row=dict.fromkeys(FIELDS,1);row.update(request_bytes=1000,response_bytes=2000,stream_events=50,statuses={'200':7,'503':2})
        values={m['name']:m['gauge']['dataPoints'][0]['asDouble'] for m in payload(row,512)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']}
        self.assertEqual(values['enmaas_stress_successful'],7)
        self.assertEqual(values['enmaas_stress_stream_events'],50)
        row['request_bytes']=-1
        with self.assertRaises(ValueError):payload(row,512)

    def test_forty_thousand_cap_is_reported_without_clamping(self):
        metrics=payload(dict.fromkeys(FIELDS,0),40000)['resourceMetrics'][0]['scopeMetrics'][0]['metrics']
        cap=next(m for m in metrics if m['name']=='enmaas_stress_concurrency_limit')
        self.assertEqual(cap['gauge']['dataPoints'][0]['asDouble'],40000)
        with self.assertRaises(ValueError):payload({},40001)
