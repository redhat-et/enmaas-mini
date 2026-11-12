#!/usr/bin/env python3
"""Exercise bootstrap role SQL on isolated PostgreSQL (not RDS qualification)."""
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time
import tempfile

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('bootstrap',ROOT/'rds-bootstrap.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


def run():
    image=json.loads((ROOT/'images.json').read_text())['db']['image']
    name='enmaas-db-role-test-'+secrets.token_hex(6)
    secret=name+'-admin'
    passwords={role:secrets.token_hex(32) for role in ('maas','metering')}
    def command(args, data=None, check=True):
        result=subprocess.run(['podman',*args],input=data,text=True,capture_output=True,timeout=90)
        if check and result.returncode:raise RuntimeError('Database test operation failed (credential-bearing output suppressed)')
        return result
    made_secret=False;made_container=False
    try:
        command(['secret','create',secret,'-'],secrets.token_hex(32));made_secret=True
        command(['run','-d','--name',name,'--network','none','--memory','256m','--cpus','1',
            '--label','io.enmaas.test-lab=database-role-test','--secret',secret+',type=env,target=POSTGRES_PASSWORD',
            '-e','POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256',image]);made_container=True
        for _ in range(60):
            if command(['exec',name,'pg_isready','-U','postgres'],check=False).returncode==0:break
            time.sleep(0.5)
        else:raise RuntimeError('Database readiness timeout')
        def admin(sql):
            return command(['exec','-i',name,'psql','-X','-qAt','-U','postgres','-v','ON_ERROR_STOP=1'],sql).stdout.strip()
        admin(bootstrap.role_sql(passwords))
        flags=admin("SELECT count(*) FROM pg_roles WHERE rolname IN ('maas','metering') AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);")
        assert flags=='0','Unexpected elevated role'
        # A disposable CA/server certificate exercises libpq verify-full without
        # opening any port. DNS identity and the loopback transport stay separate.
        with tempfile.TemporaryDirectory(prefix='enmaas-db-tls-') as temporary:
            directory=Path(temporary)
            for stem,hostname in (('server','db.lab.test'),('untrusted','other.lab.test')):
                generated=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                    '-days','1','-subj',f'/CN={hostname}','-addext',f'subjectAltName=DNS:{hostname}',
                    '-keyout',str(directory/(stem+'.key')),'-out',str(directory/(stem+'.pem'))],
                    capture_output=True,timeout=30)
                if generated.returncode:raise RuntimeError('Test certificate generation failed')
            for filename in ('server.key','server.pem','untrusted.pem'):
                command(['cp',str(directory/filename),name+':/tmp/'+filename])
            command(['exec',name,'chown','postgres:postgres','/tmp/server.key','/tmp/server.pem','/tmp/untrusted.pem'])
            command(['exec',name,'chmod','0600','/tmp/server.key'])
        admin("ALTER SYSTEM SET ssl_cert_file='/tmp/server.pem'; ALTER SYSTEM SET ssl_key_file='/tmp/server.key'; ALTER SYSTEM SET ssl='on'; SELECT pg_reload_conf();")
        def login(role,database,password,sql,hostname='db.lab.test',ca='/tmp/server.pem'):
            # Password enters stdin, never argv or the host environment. The
            # shell does not echo it; PostgreSQL requires SCRAM on loopback TCP.
            return command(['exec','-i',name,'sh','-c',
                'IFS= read -r PGPASSWORD; export PGPASSWORD; export PGHOSTADDR=127.0.0.1 PGSSLMODE=verify-full; export PGSSLROOTCERT="$4"; exec psql -X -qAt -h "$3" -U "$1" -d "$2" -v ON_ERROR_STOP=1',
                'psql-login',role,database,hostname,ca],password+'\n'+sql,check=False)
        for _ in range(30):
            probe=login('maas','maas',passwords['maas'],'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid();')
            if probe.returncode==0 and probe.stdout.strip()=='t':break
            time.sleep(0.2)
        else:raise RuntimeError('Verified TLS connection did not become ready')
        for role in passwords:
            result=login(role,role,passwords[role],"CREATE TABLE migration_probe(id integer); INSERT INTO migration_probe VALUES (1); SELECT current_user || ':' || count(*) FROM migration_probe;")
            assert result.returncode==0 and result.stdout.strip()==role+':1','Own database migration failed'
            mismatch=login(role,role,passwords[role],'SELECT 1;',hostname='wrong.lab.test')
            assert mismatch.returncode!=0 and 'does not match host name' in mismatch.stderr,'Expected TLS hostname rejection'
            untrusted=login(role,role,passwords[role],'SELECT 1;',ca='/tmp/untrusted.pem')
            assert untrusted.returncode!=0 and 'certificate verify failed' in untrusted.stderr,'Expected TLS certificate trust rejection'
            other='metering' if role=='maas' else 'maas'
            assert login(role,other,passwords[role],'SELECT 1;').returncode!=0,'Cross-database access allowed'
            assert login(role,role,'wrong-password','SELECT 1;').returncode!=0,'Invalid password accepted'
            assert login(role,role,passwords[role],'CREATE ROLE forbidden_role;').returncode!=0,'Role creation allowed'
        print(json.dumps({'postgresql': '17.9', 'scram_login':True,'own_database_migration':True,
            'cross_database_denied':True,'wrong_password_denied':True,'role_creation_denied':True,
            'tls_verify_full':True,'wrong_hostname_denied':True,'untrusted_ca_denied':True,
            'rds_qualified':False}))
    finally:
        try:
            if made_container:command(['rm','-f','-v',name])
        finally:
            if made_secret:command(['secret','rm',secret])

if __name__=='__main__':run()
