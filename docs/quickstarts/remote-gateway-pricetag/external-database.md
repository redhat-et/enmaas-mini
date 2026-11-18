# External PostgreSQL databases

The default deployment runs PostgreSQL in a local rootless container. You can
instead connect Praxis MaaS and PriceTag metering to two databases on an
external PostgreSQL server that you already operate. enMAAS Mini does **not**
create, provision, upgrade, back up, or manage that server or its databases.

Use PostgreSQL **17.11**. This is the PostgreSQL version used by the bundled
deployment and is the tested version for external databases.

## Requirements

Ask your database administrator to create both databases before installation:

| Component | Database | Role | Required access |
| --- | --- | --- | --- |
| MaaS | `maas` | `maas` | Owner of the database (or full create privileges on its schema) |
| PriceTag metering | `metering` | `metering` | Owner of the database (or full create privileges on its schema) |

The two URLs must use the same PostgreSQL server address and port. Both
databases normally live on one server, but they remain separate databases with
separate roles and passwords. Use empty databases for a new deployment; the
applications create their schemas on first startup. Do not point both URLs at
one database.

Each URL must:

- use `postgres://` or `postgresql://`;
- include a host, port, username, password, and database name;
- target the exact database shown above;
- set `sslmode=verify-full`;
- optionally set `connect_timeout` to an integer from 1 through 30 seconds;
- optionally set `application_name`; metering may also set `hostaddr` to the
  same literal IPv4 or IPv6 address and port used by the MaaS URL.
- use a hostname reachable from the deployment host and its Podman network.

Other URI query parameters, including parameters that override the host, role,
database, or password, are rejected. This ensures startup validation checks
the same connection that the applications use.

`sslmode=verify-full` and an explicit CA file are required to verify the server
certificate and hostname. Encryption-only modes such as `sslmode=require` are
rejected. Existing deployments using that mode must obtain the database CA and
use a certificate-matching hostname before preparing a new configuration.

URL-encode special characters in the username or password. For example, use
`%40` for `@` and `%2F` for `/`.

## Create private URL files

On the RHEL host, create two root-only files. Replace the server, passwords,
and CA path with values from your database administrator:

```console
sudo install -d -m 0700 /root/pricetag-external-db
sudo sh -c 'umask 077; cat > /root/pricetag-external-db/maas.url' <<'EOF'
postgresql://maas:MAAS_PASSWORD@db.example.com:5432/maas?sslmode=verify-full
EOF
sudo sh -c 'umask 077; cat > /root/pricetag-external-db/metering.url' <<'EOF'
postgresql://metering:METERING_PASSWORD@db.example.com:5432/metering?sslmode=verify-full
EOF
sudo chmod 0600 /root/pricetag-external-db/*.url
```

For certificate verification, copy the private CA certificate to the host and
pass its absolute path to the installer. The generated application URLs use
the copy mounted inside the application containers.

## Install

Add both URL files to the normal installer command:

```console
sudo scripts/pricetag/install \
  --hostname gateway.example.com \
  --maas-database-url-file /root/pricetag-external-db/maas.url \
  --metering-database-url-file /root/pricetag-external-db/metering.url \
  --external-database-ca-file /root/pricetag-external-db/ca.crt
```

The flags can be used with real or mock providers and either image mode. The
installer validates URL structure, TLS mode, database names, and that both URLs
use the same server address and port. It does not print the URLs or passwords.
The prepared deployment stores private environment files and the original
application URLs under `/etc/praxis-pricetag`; protect that directory and your
installation log accordingly.

`scripts/pricetag/start` performs a live connection check to both databases
before starting the applications. It verifies that each connection reaches the
expected database and role, but it does not create either database or role.
After MaaS and metering start, their readiness checks and PriceTag
initialization use the external databases. A nonempty `usage_events` table is
subject to the same `--adopt-existing-database` safety check as the local
deployment.

For a manually prepared deployment, pass `--maas-database-url-file` and
`--metering-database-url-file` to `scripts/pricetag/prepare.py`. The external
mode omits the local PostgreSQL container, volume, and Quadlets.

## Troubleshooting

- **Connection refused or timeout:** confirm DNS, routing, firewall rules, and
  that PostgreSQL accepts connections from the deployment host.
- **Certificate verification failed:** verify the CA file, server certificate
  chain, and that the URL hostname matches the certificate SAN.
- **Password authentication failed:** confirm the role name, URL-encoded
  password, and PostgreSQL authentication configuration.
- **Permission denied while creating tables:** make the application role owner
  of its database, or grant it create privileges on that database's schema.
- **Database already contains usage:** back it up, review it, and restart with
  `sudo scripts/pricetag/start --adopt-existing-database` only if it is yours
  and safe to adopt.
