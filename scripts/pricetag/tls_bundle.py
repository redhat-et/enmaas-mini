"""Validate a pre-issued gateway TLS bundle before installer side effects."""
import os
from pathlib import Path
import stat
import ssl
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def validate(directory,hostname):
    directory=Path(directory)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError('TLS bundle must be an absolute regular directory')
    info=directory.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('TLS bundle directory must be owned by the invoking user and not writable by others')
    files={name:directory/name for name in ('server.pem','server.key','ca.pem')}
    for name,path in files.items():
        try:info=path.lstat()
        except OSError:raise ValueError('TLS bundle is missing a required file') from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o022:
            raise ValueError('TLS files must be owned by the invoking user, not symlinks or writable by other users')
        if name=='server.key' and info.st_mode&0o077:
            raise ValueError('TLS private key must have mode 0600')
    try:
        # Retain the existing key-match and minimum remaining lifetime policy.
        result=subprocess.run([str(ROOT/'scripts/remote-gateway/credentials'),'check-tls',
            '--cert',str(files['server.pem']),'--key',str(files['server.key'])],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if result.returncode:
            raise ValueError('TLS key mismatch or insufficient remaining lifetime')
        server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(files['server.pem'],files['server.key'])
        client=ssl.create_default_context(cafile=str(files['ca.pem']))
        client.hostname_checks_common_name=False
        # In-memory TLS handshakes validate SANs, chain, validity and server purpose
        # without opening ports or depending on platform-specific openssl CLI flags.
        for name in (hostname,'localhost','127.0.0.1'):
            incoming,outgoing=ssl.MemoryBIO(),ssl.MemoryBIO()
            peer_incoming,peer_outgoing=ssl.MemoryBIO(),ssl.MemoryBIO()
            peer=server.wrap_bio(peer_incoming,peer_outgoing,server_side=True)
            connection=client.wrap_bio(incoming,outgoing,server_hostname=name)
            complete=False
            for _ in range(16):
                try:
                    connection.do_handshake()
                    complete=True
                    break
                except ssl.SSLWantReadError:
                    pass
                peer_incoming.write(outgoing.read())
                try:
                    peer.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                incoming.write(peer_outgoing.read())
            if not complete:
                raise ValueError('TLS validation handshake did not complete')
    except (ssl.SSLError,OSError,subprocess.SubprocessError,ValueError):
        raise ValueError('TLS bundle must have a matching key, valid trusted server certificate and public/loopback names') from None
    return files
