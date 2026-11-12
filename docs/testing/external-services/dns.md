# Optional deployment DNS

**Qualification pending.** IP addresses with explicitly trusted private CA remain
the default. This helper manages DNS records only; it does not register a domain,
change hosted-zone delegation, open ports or issue certificates. Hostname-aware
issuer/TLS configuration and private backchannel resolution are available through
the separate [OIDC setup](oidc.md#private-issuer-resolution), with RHEL qualification
still pending; the DNS helper does not configure them automatically.

## Prerequisites

Use an existing public Route 53 zone for a domain you control. Confirm its public
NS delegation separately. The selected private deployment manifest must contain
its `zone_id`, approved `domain`, account, region and AWS profile. The deployment
profile must restrict changes to the exact deployment names; IAM bootstrap and
live deny tests remain pending. Keep AWS credentials in the operator terminal.

## Plan

From the repository, after obtaining the three live VM addresses:

```sh
python3 tests/external-services/dns.py plan \
  --manifest ".state/deployments/$RUN_PREFIX.json" \
  --journal ".state/$RUN_PREFIX-dns.json" \
  --gateway-ip "$GATEWAY_HOST_IP" \
  --keycloak-ip "$KEYCLOAK_HOST_IP" \
  --observability-ip "$OBSERVABILITY_HOST_IP"
```

The plan checks the AWS account and public zone, then reports collisions for three
A records and a deployment ownership TXT record. It performs no writes. Names are
`gateway.RUN_PREFIX.DOMAIN`, `keycloak.RUN_PREFIX.DOMAIN` and
`observability.RUN_PREFIX.DOMAIN`; the TXT record contains a nonsecret ownership
marker. All records use a 60-second TTL.

## Create and inspect

After reviewing the plan and qualifying the scoped AWS profile:

```sh
python3 tests/external-services/dns.py create \
  --manifest ".state/deployments/$RUN_PREFIX.json" \
  --journal ".state/$RUN_PREFIX-dns.json" \
  --gateway-ip "$GATEWAY_HOST_IP" \
  --keycloak-ip "$KEYCLOAK_HOST_IP" \
  --observability-ip "$OBSERVABILITY_HOST_IP" \
  --confirm "create $RUN_PREFIX $ACCOUNT"
```

Creation uses an atomic CREATE batch;
existing records are never overwritten. An immutable mode-0600 journal is saved
before the AWS write. If the API response is lost, keep that journal and inspect:

```sh
python3 tests/external-services/dns.py status \
  --manifest ".state/deployments/$RUN_PREFIX.json" \
  --journal ".state/$RUN_PREFIX-dns.json"
```

An exact match is safe to recognize on retry. A conflict stops the operation.
Changing IPs is intentionally not an automatic UPSERT: inspect/delete the exact old
records before creating a separately journaled replacement. Wait for Route 53
change propagation; cached DNS may outlive the API response.

## Delete only the journaled records

```sh
python3 tests/external-services/dns.py delete \
  --manifest ".state/deployments/$RUN_PREFIX.json" \
  --journal ".state/$RUN_PREFIX-dns.json" \
  --confirm "delete $RUN_PREFIX $ACCOUNT"
```

Deletion requires exact live names, types, values, TTLs and the ownership marker.
The AWS DELETE operation also checks those values, protecting against a change
between verification and submission. Drift stops deletion. Already absent records
produce no write. The hosted zone, domain and local journal are retained.

Offline tests cover scope, collisions/drift comparisons, account mismatch and
journal-before-write behavior. Actual AWS IAM, delegation, propagation and cleanup
qualification remain pending. DNS resolution does not establish certificate trust:
keep using the verified IP/private-CA endpoint until hostname configuration and
certificates have been explicitly prepared.
