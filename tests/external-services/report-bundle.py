#!/usr/bin/env python3
"""Transfer only completed mock benchmark measurements; never bundle keys/configs."""
import argparse
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile

MAX_BYTES=32*1024*1024
REQUIRED={'summary.json','cleanup.json'}
SAMPLES={'observe.jsonl','resources.jsonl'}


def validate(files):
    if not REQUIRED <= files.keys() or len(files)!=3 or len(SAMPLES & files.keys())!=1:
        raise ValueError('Require summary, cleanup and exactly one resource sample file')
    summary=json.loads(files['summary.json'])
    cleanup=json.loads(files['cleanup.json'])
    if not isinstance(summary,dict) or summary.get('mock_only') is not True:
        raise ValueError('Require an explicit mock benchmark summary')
    if not isinstance(cleanup,dict) or type(cleanup.get('complete')) is not bool:
        raise ValueError('Require a completed cleanup report, including failed cleanup when applicable')
    source=next(iter(SAMPLES & files.keys()))
    for line in files[source].splitlines():
        if line.strip() and not isinstance(json.loads(line),dict):
            raise ValueError('Invalid resource record')


def private(info):
    if info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise ValueError('Require private inputs owned by the invoking user')


def export(directory,output):
    fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        private(os.fstat(fd))
        files={}
        for name in sorted(REQUIRED|SAMPLES):
            try:source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            except FileNotFoundError:
                if name in SAMPLES:continue
                raise
            with os.fdopen(source,'rb') as stream:
                info=os.fstat(stream.fileno());private(info)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_BYTES:
                    raise ValueError('Require bounded regular report files without links')
                data=stream.read(MAX_BYTES+1)
                if len(data)>MAX_BYTES:raise ValueError('Report file exceeds size limit')
                files[name]=data
        validate(files)  # Validate everything before emitting any archive bytes.
        with tarfile.open(fileobj=output,mode='w|',format=tarfile.USTAR_FORMAT) as archive:
            for name,data in sorted(files.items()):
                member=tarfile.TarInfo(name);member.size=len(data);member.mode=0o600
                archive.addfile(member,io.BytesIO(data))
    finally:os.close(fd)


class BoundedReader:
    """Bound archive payload plus metadata before tarfile processes headers."""
    def __init__(self,source,limit):
        self.source=source;self.remaining=limit

    def read(self,size=-1):
        count=self.remaining+1 if size<0 else min(size,self.remaining+1)
        data=self.source.read(count)
        self.remaining-=len(data)
        if self.remaining<0:raise ValueError('Archive exceeds total size limit')
        return data


def unpack(source,destination):
    files={}
    with tarfile.open(fileobj=BoundedReader(source,3*MAX_BYTES+65536),mode='r|') as archive:
        for member in archive:
            if (member.name not in REQUIRED|SAMPLES or member.name in files or not member.isfile()
                    or member.size<0 or member.size>MAX_BYTES):
                raise ValueError('Unexpected archive member, type, duplicate or size')
            stream=archive.extractfile(member)
            data=stream.read(MAX_BYTES+1)
            if len(data)!=member.size:raise ValueError('Incomplete archive member')
            files[member.name]=data
    validate(files)
    destination=Path(destination)
    destination.mkdir(mode=0o700)  # Never reuse/overwrite an existing destination.
    fd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name,data in files.items():
            file_fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(file_fd,'wb') as stream:stream.write(data)
    finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('export');p.add_argument('directory',type=Path)
    p=sub.add_parser('import');p.add_argument('archive',type=Path);p.add_argument('directory',type=Path)
    args=parser.parse_args();os.umask(0o077)
    if args.action=='export':export(args.directory,sys.stdout.buffer)
    else:
        with args.archive.open('rb') as stream:unpack(stream,args.directory)
        print('Imported measurement files: '+str(args.directory))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,tarfile.TarError):
        raise SystemExit('Report transfer refused: check private inputs, allowed members and output path.') from None
