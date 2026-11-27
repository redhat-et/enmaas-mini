#!/usr/bin/env python3
"""Verify auxiliary image locks are indexes with native Linux amd64/arm64 images."""
import json
from pathlib import Path
import re
import subprocess

HERE=Path(__file__).resolve().parent
INDEX_TYPES={'application/vnd.oci.image.index.v1+json',
             'application/vnd.docker.distribution.manifest.list.v2+json'}


def validate_index(document):
    if not isinstance(document,dict) or document.get('mediaType') not in INDEX_TYPES:
        raise ValueError('Require a multi-architecture image index, not a platform-specific image')
    manifests=document.get('manifests')
    if not isinstance(manifests,list):raise ValueError('Missing index descriptors')
    result={}
    for architecture in ('amd64','arm64'):
        matches=[entry for entry in manifests if isinstance(entry,dict) and isinstance(entry.get('platform'),dict)
                 and entry.get('platform',{}).get('os')=='linux'
                 and entry.get('platform',{}).get('architecture')==architecture]
        if len(matches)!=1 or not re.fullmatch(r'sha256:[a-f0-9]{64}',str(matches[0].get('digest',''))):
            raise ValueError('Require exactly one pinned Linux image per supported architecture')
        result[architecture]=matches[0]['digest']
    return result


def main():
    images=json.loads((HERE/'images.json').read_text())
    for name,row in images.items():
        image=row['image']
        if not re.fullmatch(r'(?:docker.io|quay.io)/[a-z0-9_./-]+@sha256:[a-f0-9]{64}',image):
            raise ValueError('Require a public registry image pinned by digest')
        result=subprocess.run(['podman','manifest','inspect',image],capture_output=True,text=True,timeout=60)
        if result.returncode:raise ValueError('Cannot inspect auxiliary image index: '+name)
        validate_index(json.loads(result.stdout))
        print('Verified native amd64/arm64 index: '+name,flush=True)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise SystemExit('Auxiliary image-platform validation failed; no containers started') from None
