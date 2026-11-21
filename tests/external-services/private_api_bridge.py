"""Bounded request/reply pipe to the private management client container."""
import json
import os
import selectors
import subprocess
import time


class PrivateApiBridge:
    def __init__(self, command, *, timeout=20):
        if not 0 < timeout <= 60:raise ValueError('Invalid bridge deadline')
        self.timeout=timeout
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,bufsize=0)
        self.pending=b''

    def call(self, command):
        raw=json.dumps(command).encode()+b'\n'
        if len(raw)>65536:raise ValueError('Management command exceeds bound')
        deadline=time.monotonic()+self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin,selectors.EVENT_WRITE)
                offset=0
                while offset<len(raw):
                    if not selector.select(max(0,deadline-time.monotonic())):raise TimeoutError()
                    offset+=os.write(self.process.stdin.fileno(),raw[offset:offset+4096])
                selector.unregister(self.process.stdin)
                selector.register(self.process.stdout,selectors.EVENT_READ)
                while b'\n' not in self.pending:
                    if not selector.select(max(0,deadline-time.monotonic())):raise TimeoutError()
                    chunk=os.read(self.process.stdout.fileno(),65536)
                    if not chunk:raise RuntimeError('Private management bridge stopped')
                    self.pending+=chunk
                    if len(self.pending)>1048577:raise ValueError('Management reply exceeds bound')
                reply,self.pending=self.pending.split(b'\n',1)
                if self.pending:raise ValueError('Unsolicited management reply')
                return json.loads(reply)
        except Exception:
            self.close()
            # Never expose private server replies, keys or subprocess output.
            raise RuntimeError('Private management operation failed; inspect owned fixture') from None

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=3)
        for pipe in (self.process.stdin,self.process.stdout):
            if pipe:pipe.close()
