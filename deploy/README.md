# Contest-day judging fleet

The always-on worker on `ams-app` judges ~1,100 jobs/hour. It runs
`concurrency: 1` because it shares 1 GB of RAM with Postgres, Redis, the API
and Caddy, and a GTest-linked C++23 compile can want most of the free memory
on its own — so it cannot be turned up. A 2,000-competitor contest needs
roughly three times that throughput for a few hours, twice a year.

This directory is how those hours are bought: an Auto Scaling group that
sits at zero, goes to twelve, and comes back to zero.

| File | What it is |
|---|---|
| `spot-user-data.sh` | Bare Ubuntu → draining the queue, ~90 s. Baked into the launch template |
| `judge-fleet.sh` | `up` / `down` / `status` |
| `../config/spot.yaml` | The worker config those hosts run |

## On the day

```bash
./deploy/judge-fleet.sh up          # T−30 min
./deploy/judge-fleet.sh status      # confirm 12 running, queue draining
./deploy/judge-fleet.sh down        # when the last submission is judged
```

`down` is not optional. Twelve `c6a.2xlarge` left running is about **$79 a
day**; for a three-hour contest the fleet costs about **$3.30**.

## Decisions worth knowing

**x86_64, not Graviton.** The worker is Python and would run on either, but
it shells out to `/usr/local/bin/cxxprobe`, and the only published build of
that is `cxxprobe-x86_64-linux`. Spot for `c6a.2xlarge` is ~$0.0915/h against
`c6g.2xlarge`'s ~$0.0855 — **$0.21 more across an entire contest**, which is
not worth maintaining a second architecture's build and a second set of
verdicts nobody can reproduce on their laptop.

**`concurrency: 8`, one per vCPU.** These hosts have 16 GB and nothing else
on them, so ~2 GB per concurrent compile is comfortable. This is the number
to lower first if judging starts OOMing.

**The judge binary is pinned and checksummed.** A worker that quietly judged
with a different cxxprobe than the rest of the fleet would produce verdicts
nobody could reproduce. Bump `CXXPROBE_VERSION` in the user-data
deliberately, and never to a floating ref.

**Spot interruptions are handled, not feared.** `spot-drain` watches the
instance-action metadata endpoint and stops the worker on the two-minute
notice; SIGTERM means "stop claiming, finish what you have". Without it an
interrupted host's in-flight jobs sit invisible until the 900 s SQS
visibility timeout expires — a candidate watching "queued" for fifteen
minutes in the middle of their contest.

**No credentials in the launch template.** User-data is readable by anyone
with EC2 read access. AWS access comes from the instance role
(`ams-worker-role`); the control-plane key is pulled from SSM at boot.

## Still to create in AWS

Nothing here runs until these exist — see `~/infra.md`:

1. `ams-worker-role` + instance profile — S3 `problems/*`,`submissions/*` read,
   `artifacts/*` write, consume both queues, read one SSM parameter. Nothing else.
2. A launch template carrying `spot-user-data.sh`.
3. The `ams-judge-spot` ASG at desired 0, across all three AZs.
