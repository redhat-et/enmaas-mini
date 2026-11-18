#!/usr/bin/env python3
"""Build only the isolated asynchronous mock provider, never application images."""
import ast
import hashlib
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def inputs():
    tree = ast.parse((ROOT/'scripts/pricetag/prepare.py').read_text())
    base = next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PYTHON' for t in node.targets))
    if not base.startswith('docker.io/library/python@sha256:'):
        raise ValueError('Require the pinned fixture Python image')
    files = {'requirements.txt':(HERE/'stress-requirements.txt').read_bytes(),
             'async_provider.py':(HERE/'async_provider.py').read_bytes(),
             'stream_payload.py':(HERE/'stream_payload.py').read_bytes(),
             'provider.py':(ROOT/'tests/common/provider.py').read_bytes()}
    files['Containerfile'] = (f'FROM {base}\n'
        'COPY --chmod=0644 requirements.txt /tmp/requirements.txt\n'
        'RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt\n'
        'COPY --chmod=0644 provider.py /fixtures/provider.py\n'
        'COPY --chmod=0644 async_provider.py /fixtures/async_provider.py\n'
        'COPY --chmod=0644 stream_payload.py /fixtures/stream_payload.py\n'
        'USER 65532:65532\n'
        'ENTRYPOINT ["python3","-B","/fixtures/async_provider.py"]\n').encode()
    digest = hashlib.sha256(b''.join(k.encode()+b'\0'+v for k,v in sorted(files.items()))).hexdigest()
    return files, 'localhost/enmaas-stress-provider:'+digest[:16]


def main():
    files, image = inputs()
    # Never send the checkout or .state (which holds credentials) as a build context.
    with tempfile.TemporaryDirectory(prefix='enmaas-stress-build-') as directory:
        root = Path(directory)
        for name, content in files.items():
            (root/name).write_bytes(content)
        subprocess.run(['podman','build','--tag',image,'--file',str(root/'Containerfile'),str(root)],check=True)
    print(image)


if __name__ == '__main__':
    main()
