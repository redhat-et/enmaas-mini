import json
from pathlib import Path
import unittest
import importlib.util


class StreamPayload(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('common_provider',Path(__file__).resolve().parents[2]/'common/provider.py')
        self.common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.common)

    def test_payload_bytes_chunks_protocol_and_usage(self):
        from stream_payload import streamed_frames
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages'):
            result = self.common.completion(path, {'model':'fixture'}, False, declared_tools_only=True)
            count, frames = streamed_frames(self.common, path, result, 129)
            rows = list(frames)
            self.assertEqual(count,len(rows))
            decoded = [json.loads(x.split(b'data: ',1)[1]) for x in rows if b'[DONE]' not in x]
            if path.endswith('completions'):
                parts = [x['choices'][0]['delta'].get('content','') for x in decoded if x.get('choices')]
            elif path.endswith('responses'):
                parts = [x['delta'] for x in decoded if x['type']=='response.output_text.delta']
                self.assertEqual([x['sequence_number'] for x in decoded],list(range(len(decoded))))
                self.assertEqual(decoded[-1]['response']['output'][0]['content'][0]['text'],'data'*129)
            else:
                parts = [x['delta']['text'] for x in decoded if x['type']=='content_block_delta']
            self.assertEqual(''.join(parts),'data'*129)
            self.assertEqual(len([x for x in parts if x]),33)
            self.assertLessEqual(max(map(len,parts)),16)
            self.assertIn(b'usage',b''.join(rows))

    def test_bounds_and_maximum_wire_size(self):
        from stream_payload import streamed_frames
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages'):
            result = self.common.completion(path,{'model':'fixture'},False,declared_tools_only=True)
            count, frames = streamed_frames(self.common,path,result,4096)
            self.assertLess(sum(map(len,frames)),1024*1024)
            self.assertGreater(count,1024)
            for value in (0,4097,True):
                with self.assertRaises(ValueError):streamed_frames(self.common,path,result,value)
