#!/usr/bin/env python3
"""Bootstrap two fresh RDS databases on RHEL; read the DB admin secret on stdin."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.parse


def scram(password,salt=None):
    salt=salt or secrets.token_bytes(16)
    salted=hashlib.pbkdf2_hmac('sha256',password.encode('ascii'),salt,4096)
    client=hmac.digest(salted,b'Client Key','sha256')
    stored=hashlib.sha256(client).digest();server=hmac.digest(salted,b'Server Key','sha256')
    enc=lambda value:base64.b64encode(value).decode('ascii')
    return 'SCRAM-SHA-256$4096:'+enc(salt)+'$'+enc(stored)+':'+enc(server)


def credentials(raw):
    if len(raw)>16384:raise ValueError('Secret input too large')
    value=json.loads(raw);user=value['username'];password=value['password']
    if not isinstance(user,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,62}',user):raise ValueError('Invalid database administrator')
    if not isinstance(password,str) or not password or len(password)>4096 or any(c in password for c in '\x00\r\n'):
        raise ValueError('Invalid database password')
    return user,password


def role_sql(passwords):
    if set(passwords)!={'maas','metering'}:raise ValueError('Expected the two application roles')
    sql=['BEGIN;']
    for name in ('maas','metering'):
        if not re.fullmatch(r'[a-f0-9]{64}',passwords[name]):raise ValueError('Require generated application password')
        sql.append(f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{scram(passwords[name])}';")
    sql.append('COMMIT;')
    for name in ('maas','metering'):
        sql.extend([f'CREATE DATABASE {name} OWNER {name};',f'REVOKE ALL ON DATABASE {name} FROM PUBLIC;'])
    return '\n'.join(sql)+'\n'


def private_write(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(value);stream.flush();os.fsync(stream.fileno())


def bootstrap(args,secret):
    if os.geteuid()!=0:raise ValueError('Run on RHEL with sudo')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*\.rds\.amazonaws\.com(?:\.cn)?',args.host):
        raise ValueError('Use the verified RDS endpoint hostname')
    if not args.ca_file.is_file() or args.ca_file.is_symlink():raise ValueError('RDS CA bundle is required')
    user,password=credentials(secret)
    if shutil.which('psql') is None:raise ValueError('Install the RHEL PostgreSQL client first')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    passwords={name:secrets.token_hex(32) for name in ('maas','metering')}
    private_write(args.output/'ca.crt',args.ca_file.read_text())
    for name,generated in passwords.items():
        private_write(args.output/(name+'.url'),f'postgresql://{name}:{generated}@{args.host}:5432/{name}?sslmode=verify-full\n')
    state={'complete':False,'stage':'preflight','roles':['maas','metering'],'databases':['maas','metering']}
    journal=args.output/'bootstrap.json'
    def save():
        temporary=args.output/'.bootstrap.tmp'
        with temporary.open('w') as stream:
            json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
        temporary.chmod(0o600);temporary.replace(journal)
    save()
    # No credential in process arguments, inherited environment, logs or evidence.
    with tempfile.TemporaryDirectory(prefix='enmaas-rds-admin-') as directory:
        pgpass=Path(directory)/'pgpass'
        escaped=password.replace('\\','\\\\').replace(':','\\:')
        private_write(pgpass,f'{args.host}:5432:*:{user}:{escaped}\n')
        env={key:value for key,value in os.environ.items() if not key.startswith(('PG','AWS_'))}
        env.update(PGHOST=args.host,PGPORT='5432',PGUSER=user,PGDATABASE='postgres',PGPASSFILE=str(pgpass),
            PGSSLMODE='verify-full',PGSSLROOTCERT=str(args.output/'ca.crt'),PGCONNECT_TIMEOUT='10')
        def execute(sql,database='postgres',role=None,role_password=None):
            current=dict(env);current['PGDATABASE']=database
            if role:
                private_write(Path(directory)/(role+'.pgpass'),f'{args.host}:5432:*:{role}:{role_password}\n')
                current.update(PGUSER=role,PGPASSFILE=str(Path(directory)/(role+'.pgpass')))
            result=subprocess.run(['psql','-X','-qAt','-v','ON_ERROR_STOP=1'],input=sql,text=True,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=current,timeout=60)
            if result.returncode:raise ValueError('Database operation failed; inspect bootstrap stage, not credential output')
            return result.stdout.strip()
        existing=execute("SELECT (SELECT count(*) FROM pg_roles WHERE rolname IN ('maas','metering')) + (SELECT count(*) FROM pg_database WHERE datname IN ('maas','metering'));\n")
        if existing!='0':raise ValueError('Application roles/databases already exist; no adoption or password rotation performed')
        state['stage']='create-roles-and-databases';save()
        execute(role_sql(passwords))
        for name in ('maas','metering'):
            state['stage']='verify-'+name;save()
            result=execute("SELECT current_user || ':' || current_database() || ':' || ssl::text FROM pg_stat_ssl WHERE pid=pg_backend_pid();\n",name,name,passwords[name])
            if result!=name+':'+name+':true':raise ValueError('Application role/TLS check failed')
        state['stage']='verify-role-isolation';save()
        isolation=execute("SELECT count(*) FROM pg_roles WHERE rolname IN ('maas','metering') AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);\n"
            "SELECT has_database_privilege('maas','metering','CONNECT') OR has_database_privilege('metering','maas','CONNECT');\n")
        if isolation!='0\nf':raise ValueError('Application role isolation check failed')
        state.update(complete=True,stage='verified');save()
    print('Application databases ready. Private installer inputs: '+str(args.output))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True);parser.add_argument('--ca-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('/root/pricetag-external-db'))
    args=parser.parse_args();os.umask(0o077)
    if sys.stdin.isatty():parser.error('Pipe the database administrator secret JSON through stdin; do not type it on the command line')
    bootstrap(args,sys.stdin.buffer.read(16385))

if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError):
        raise SystemExit('RDS bootstrap stopped. Keep the private output and inspect bootstrap.json before recovery; nothing was deleted automatically') from None
