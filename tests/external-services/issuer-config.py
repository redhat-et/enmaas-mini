"""One canonical OIDC identity with an optional private address for server peers."""
import importlib.util
import ipaddress
from pathlib import Path

path=Path(__file__).resolve().parents[2]/'scripts/pricetag/oidc_config.py'
spec=importlib.util.spec_from_file_location('lab_issuer_validation',path)
oidc=importlib.util.module_from_spec(spec);spec.loader.exec_module(oidc)


def resolve(public_ip,name=None,private_ip=None):
    public_ip=str(ipaddress.IPv4Address(public_ip))
    if name is None and private_ip is None:return public_ip,None
    if not isinstance(name,str) or not name or private_ip is None:
        raise ValueError('supply both issuer DNS name and private IPv4 address')
    mapping=oidc.private_issuer_host('https://'+name+':8443/realms/enmaas-lab',private_ip)
    # The supplied name must be canonical, with no URL/path/port interpretation.
    if mapping.split(':',1)[0]!=name:
        raise ValueError('issuer name must be an exact lowercase DNS hostname')
    return name,mapping
