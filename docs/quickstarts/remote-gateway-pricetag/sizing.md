# Size the RHEL gateway

Size for **requests per second, concurrent streams and retained data**, not only
registered users. No GPU is required when models run at external providers.
Self-hosted inference needs a separate GPU/model-memory plan.

## Initial AWS candidates

These are **starting configurations to benchmark**, not certified traffic limits.
Local mock tests have exposed overload-related accounting loss; increasing the
instance size alone does not establish billing-safe capacity.

| Target offered traffic | RHEL EC2 candidate | vCPUs / RAM | Qualification needed |
| --- | --- | --- | --- |
| 10–25 requests/s | `m7i.xlarge` | 4 / 16 GiB | Representative users, long streams and exact accounting. |
| 25–50 requests/s | `m7i.2xlarge` | 8 / 32 GiB | Approximately 3,250 streams at 50 requests/s with a 65-second mean; validate on AWS. |
| 50–100 requests/s with long streams | `c7i.4xlarge` | 16 / 32 GiB | Local 16-vCPU test reached 100 requests/s with realistic-sized long streams; AWS and accounting qualification remain separate. |
| Same traffic with larger caches or colocated observability | `m7i.4xlarge` | 16 / 64 GiB | Demonstrate memory pressure before choosing the larger RAM size. |
| Above 100 requests/s | Not qualified | — | With a 20,000-request concurrency ceiling, the local 150 requests/s test produced 503s, client timeouts and a usage-record mismatch. Require a separate generator and complete usage verification before assigning a supported band. |

Specifications: [AWS M7i](https://aws.amazon.com/ec2/instance-types/m7i/) and
[AWS C7i](https://aws.amazon.com/ec2/instance-types/c7i/). Verify region availability
and pricing before provisioning. Use the AWS helper's instance-type option to
select the candidate; [AWS deployment instructions](../../testing/aws-pricetag.md).

These targets concern the gateway and its supporting services, not model compute.
The RHEL benchmark-host reference is 8 vCPUs, 32 GiB RAM and 150 GiB disk; the separate local Podman scaling experiment uses up to 16 vCPUs and 64 GiB RAM. The disk
size is a starting allocation, not a retention guarantee. External PostgreSQL and
remote observability change which host needs those resources.

## Translate people and bots into traffic

`requests/s = active people × requests/person/minute ÷ 60 + active bots × requests/bot/minute ÷ 60`

| Requests/s | Active people at 1 request/minute | Busy agents at 10 requests/minute |
| ---: | ---: | ---: |
| 10 | 600 | 60 |
| 25 | 1,500 | 150 |
| 50 | 3,000 | 300 |
| 100 | 6,000 | 600 |
| 250 | 15,000 | 1,500 |

The columns are **alternative assumptions**, not additive supported user counts.
An agent task can issue several model requests; a registered user may issue none.
Measure actual usage frequency, parallelism and bursts before converting traffic into seats.
A serial agent waiting 65 seconds per response cannot sustain ten requests/minute;
that example requires overlapping work or shorter responses.
For example, 15 people at one request/minute plus 10 bots at ten requests/minute
produce approximately **1.92 requests/s**.

## Allow for streaming duration

`average concurrent streams ≈ requests/s × mean response seconds`

At 100 requests/s, a ten-second average means roughly 1,000 open streams; a
65-second average means roughly 6,500. Size connection limits, gateway memory and
load-generator capacity for this concurrency. Stream byte size and chunk frequency
also consume CPU and bandwidth. Token counts in a usage event do not measure bytes.

## Qualify a size

Use [benchmark instructions](../../testing/external-services/benchmark.md) with
representative identities, payload sizes, response-duration tails and retained
usage data. Keep the generator on a separate host for an AWS capacity claim.

Require exact accounting, no unexplained errors or dropped admissions, and spare
CPU, memory, pool and disk capacity during at least ten-minute stages and a longer
soak. Include cold-start bursts, dashboard queries and recovery. Report the first
failing load separately from the supported operating target. Local AppleHV vCPUs
are not equivalent to EC2 vCPUs; local measurements cannot certify an AWS size.
