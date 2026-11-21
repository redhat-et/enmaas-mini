# Size the RHEL gateway

Size for **requests per second, concurrent streams and retained data**, not only
registered users. No GPU is required when models run at external providers.
Self-hosted inference needs a separate GPU/model-memory plan.

## Initial AWS candidates

These are **example sizes to benchmark**, not certified throughput bands.
Choose CPU versus RAM from measured pressure, then qualify the actual workload.
A larger instance alone does not establish complete usage accounting.

| Candidate | vCPUs / RAM | Experiment |
| --- | --- | --- |
| `m7i.xlarge` | 4 / 16 GiB | Establish a low-load baseline and service memory floor. |
| `m7i.2xlarge` | 8 / 32 GiB | Compare throughput and latency at the same request mix. |
| `c7i.4xlarge` | 16 / 32 GiB | Test whether more CPU improves throughput at fixed RAM. |
| `m7i.4xlarge` | 16 / 64 GiB | Test whether more RAM improves the same workload. |

Specifications: [AWS M7i](https://aws.amazon.com/ec2/instance-types/m7i/) and
[AWS C7i](https://aws.amazon.com/ec2/instance-types/c7i/). Check regional
availability and cost before provisioning. The [AWS helper](../../testing/aws-pricetag.md)
accepts an explicit instance type. These examples are not prerequisites.

Size retained database/telemetry storage separately. External PostgreSQL and
remote observability change which host consumes those resources. Keep the load
generator separate when measuring a cloud gateway's supported capacity.

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
