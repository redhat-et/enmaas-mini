"""Bounded retries for read-only local host/container observations."""
import subprocess
import time


class ProbeError(RuntimeError):
    """Safe diagnostic categories; never retain subprocess arguments or output."""
    def __init__(self, command, failure):
        prefix=tuple(command[:3])
        self.operation=('vm-ssh' if prefix==('podman','machine','ssh') else
                        'container-'+command[1] if command[:1]==['podman'] and
                        len(command)>1 and command[1] in ('stats','inspect','ps','exec') else 'other')
        self.kind='timeout' if isinstance(failure,subprocess.TimeoutExpired) else 'exit'
        super().__init__('Local read-only probe failed after three bounded attempts; output suppressed')


def read(command, *, input=None, text=False):
    """Never interpret a transient probe timeout as a successful health check."""
    for attempt in range(3):
        try:
            return subprocess.run(command,input=input,text=text,check=True,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        except (subprocess.TimeoutExpired,subprocess.CalledProcessError) as failure:
            if attempt==2:raise ProbeError(command,failure) from None
            time.sleep(.2)
