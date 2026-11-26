"""Bounded synthetic text: four bytes per estimated token, four tokens per delta.

These are wire-size estimates, not tokenizer claims. Expand lazily so each open
stream does not retain hundreds of encoded protocol frames in memory.
"""
import copy
import json


def streamed_frames(common, path, result, output_tokens):
    if type(output_tokens) is not int or not 32 <= output_tokens <= 4096:
        raise ValueError('Invalid synthetic output size')
    result = copy.deepcopy(result)
    text = 'data'*output_tokens
    if path == '/v1/chat/completions':
        result['choices'][0]['message'] = {'role':'assistant','content':text}
    elif path == '/v1/responses':
        result['output'][0]['content'][0]['text'] = text
    elif path == '/v1/messages':
        result['content'][0] = {'type':'text','text':text}
    else:
        raise ValueError('Unknown mock protocol')
    base = list(common.events(path,result))
    count = len(base)+(output_tokens+3)//4-1

    def expanded():
        sequence = 0
        for frame in base:
            raw = frame.split(b'data: ',1)[1].strip()
            if raw == b'[DONE]':
                yield frame
                continue
            original = json.loads(raw)
            kind = original.get('type')
            delta = (path.endswith('completions') and original.get('choices') and
                     original['choices'][0]['delta'].get('content') is not None)
            is_text = delta or kind in ('response.output_text.delta','content_block_delta')
            values = (text[i:i+16] for i in range(0,len(text),16)) if is_text else (None,)
            for value in values:
                row = copy.deepcopy(original)
                if delta:
                    row['choices'][0]['delta']['content'] = value
                elif kind == 'response.output_text.delta':
                    row['delta'] = value
                elif kind == 'content_block_delta':
                    row['delta']['text'] = value
                if path.endswith('responses'):
                    row['sequence_number'] = sequence
                    sequence += 1
                prefix = (('event: '+kind+'\n') if kind else '')+'data: '
                yield (prefix+json.dumps(row)+'\n\n').encode()
    return count, expanded()
