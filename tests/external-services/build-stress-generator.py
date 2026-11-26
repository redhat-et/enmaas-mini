#!/usr/bin/env python3
"""Build a nonroot generator from an explicit, credential-free source context."""
import ast
import hashlib
from pathlib import Path
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def inputs():
    tree=ast.parse((ROOT/'scripts/pricetag/prepare.py').read_text())
    base=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id=='PYTHON' for target in node.targets))
    if not base.startswith('docker.io/library/python@sha256:'):
        raise ValueError('Require the pinned fixture Python image')
    sources=('generator_worker.py','async_scenarios.py','async_transport.py','parallel_results.py','transport.py')
    files={name:(HERE/name).read_bytes() for name in sources}
    files['requirements.txt']=(HERE/'stress-requirements.txt').read_bytes()
    recipe=(f'FROM {base}\nCOPY --chmod=0644 requirements.txt /tmp/requirements.txt\n'
        'RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt\n')
    recipe+=''.join(f'COPY --chmod=0644 {name} /fixtures/{name}\n' for name in sources)
    recipe+='USER 65532:65532\nENTRYPOINT ["python3","-B","/fixtures/generator_worker.py"]\n'
    files['Containerfile']=recipe.encode()
    digest=hashlib.sha256(b''.join(name.encode()+b'\0'+value for name,value in sorted(files.items()))).hexdigest()
    return files,'localhost/enmaas-stress-generator:'+digest[:16]


def main():
    files,image=inputs()
    with tempfile.TemporaryDirectory(prefix='enmaas-generator-build-') as temporary:
        root=Path(temporary)
        for name,content in files.items():(root/name).write_bytes(content)
        subprocess.run(['podman','build','--tag',image,'--file',str(root/'Containerfile'),str(root)],
                       check=True,timeout=600)
    print(image)


if __name__=='__main__':main()
