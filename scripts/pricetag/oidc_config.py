"""Validate opt-in external OIDC configuration without logging credential values."""
import json
import ipaddress
import os
from pathlib import Path
import re
import ssl
import stat
from urllib.parse import urlsplit


def https_url(value,origin=False):
    if not isinstance(value,str) or not value or not value.isascii() or any(c.isspace() or ord(c)<32 for c in value):
        raise ValueError('OIDC URLs must be printable ASCII HTTPS URLs')
    try:
        parsed=urlsplit(value)
        port=parsed.port
    except ValueError:
        raise ValueError('invalid OIDC URL') from None
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or '\\' in value or parsed.query or parsed.fragment or (origin and parsed.path not in ('','/'))
            or (port is not None and not 1<=port<=65535)):
        raise ValueError('OIDC URLs require HTTPS, no credentials/query/fragment, and an exact origin where requested')
    return value.rstrip('/')


def private_issuer_host(issuer, address):
    """Pin only the issuer DNS name to a private address; preserve HTTPS identity."""
    name=urlsplit(https_url(issuer)).hostname
    if (len(name)>253 or '.' not in name or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)
            for label in name.split('.'))):
        raise ValueError('private issuer resolution requires an explicit DNS hostname')
    try:ipaddress.ip_address(name)
    except ValueError:pass
    else:raise ValueError('private issuer resolution requires DNS, not an IP issuer URL')
    if not isinstance(address,str):raise ValueError('private issuer address must be RFC1918 IPv4')
    try:private=ipaddress.IPv4Address(address)
    except (ValueError,TypeError):raise ValueError('private issuer address must be RFC1918 IPv4') from None
    if not any(private in ipaddress.IPv4Network(network) for network in
               ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
        raise ValueError('private issuer address must be RFC1918 IPv4')
    return name+':'+str(private)


def load(path,public_origin):
    """Return environment settings, optional CA path and private host mapping; disabled unless called."""
    path=Path(path)
    if not path.is_absolute(): raise ValueError('OIDC input path must be absolute')
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except OSError:
        raise ValueError('OIDC input must be a private regular file, not a symlink') from None
    with os.fdopen(descriptor, 'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
            raise ValueError('OIDC input must be owned by the invoking user and mode 0600')
        content=source.read(65537)
    if len(content)>65536: raise ValueError('OIDC input is too large')
    try: config=json.loads(content)
    except (ValueError,UnicodeError): raise ValueError('OIDC input must be valid JSON') from None
    if not isinstance(config,dict) or set(config)-{'issuer_url','client_id','client_secret','ca_file','issuer_private_ip'}:
        raise ValueError('unsupported OIDC configuration fields')
    issuer=https_url(config.get('issuer_url'))
    mapping=private_issuer_host(issuer,config['issuer_private_ip']) if 'issuer_private_ip' in config else None
    origin=https_url(public_origin,origin=True)
    client=config.get('client_id')
    if not isinstance(client,str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',client):
        raise ValueError('invalid OIDC client identifier')
    secret=config.get('client_secret')
    # Values enter a systemd EnvironmentFile: reject quoting/newline semantics.
    if (not isinstance(secret,str) or not 16<=len(secret)<=4096
            or any(not 33<=ord(c)<=126 or c in "'\\\"" for c in secret)):
        raise ValueError('OIDC client secret must be 16–4096 visible ASCII characters without quotes or backslashes')
    env={'OIDC_ENABLED':'true','OIDC_ISSUER_URL':issuer,'OIDC_CLIENT_ID':client,'OIDC_CLIENT_SECRET':secret,
         'OIDC_REDIRECT_URL':origin+'/oauth/callback','OIDC_POST_LOGOUT_REDIRECT_URL':origin+'/login',
         'OIDC_SCOPES':'openid profile email','OIDC_DEVELOPMENT_MODE':'false',
         'OIDC_CLAIMS_DEBUG':'false','OIDC_TEST_LOGIN':'false'}
    ca=None
    if 'ca_file' in config:
        if not isinstance(config['ca_file'],str): raise ValueError('invalid OIDC CA path')
        ca=Path(config['ca_file'])
        if not ca.is_absolute() or ca.is_symlink() or not ca.is_file():
            raise ValueError('OIDC CA must be an absolute regular certificate file')
        info=ca.stat()
        if info.st_uid!=os.geteuid() or info.st_mode&0o022:
            raise ValueError('OIDC CA must be owned by the invoking user and not group/world writable')
        try: ssl.create_default_context(cafile=str(ca))
        except (OSError,ssl.SSLError): raise ValueError('OIDC CA could not be loaded') from None
        env['SSL_CERT_FILE']='/etc/pricetag-oidc-ca.pem'
    return env,ca,mapping
