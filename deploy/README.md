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

## The account cannot hold this fleet in one region

Proven by trying, 2026-09-18 and 2026-09-20. The AWS account is on the
**Free Tier plan**, which permits only free-tier-eligible instance types —
the largest being `c7i-flex.large` at **2 vCPU** — and caps EC2 at **5–8
vCPU per region**. Lightsail is capped the same way (only `micro`/`nano`).
The quota counts *vCPUs, not instances*, so picking smaller instances buys
nothing.

`judge-fleet-multiregion.sh` is the answer: the same quota exists in each of
the 18 enabled regions, ~84 usable vCPU in total, which is the same order as
the 96 the single-region plan assumed.

Two ways to drive it. **`judge_fleet.py` is the one to use** — it follows
the queue, so the fleet is only as large as the backlog justifies and comes
back to zero on its own. `judge-fleet-multiregion.sh` is the manual
equivalent, kept for when you want a fixed size.

```bash
./deploy/judge_fleet.py init --user-data deploy/spot-user-data.sh   # once
./deploy/judge_fleet.py watch          # T-30: size to the backlog, every 60s
./deploy/judge_fleet.py status
./deploy/judge_fleet.py destroy        # remove the fleet entirely
```

For a real contest, install `systemd/ams-judge-autoscale.{service,timer}` on
`ams-app` rather than leaving `watch` running on a laptop — a laptop that
sleeps mid-contest stops scaling.

**Sizing.** One instance (2 vCPU) sustains ~0.8 jobs/s warm, so the
controller holds ~24 queued jobs per instance: a submission arriving at the
back of the queue waits about 30 s. It scales out immediately and scales in
only after three consecutive quiet checks, because coming down costs a full
90 s boot to undo and the lull between two problems' bursts should not cost
a fleet.

**Verified end to end, 2026-09-20**: 300 jobs queued → controller scaled
0 → 12 instances across three regions → drained → scaled back to 0 with no
manual step.

**Measured, 2026-09-20** — 1,000 real submissions (38% accepted, 40% wrong,
12% compile error, 6% symbolic, 4% timeout) across 24 vCPU in ap-south-1,
us-east-1 and us-west-2: **105 s, 9.5 jobs/s**. Extrapolated to the full
~84 vCPU: **~33 jobs/s**, against a 2,000-competitor contest's estimated
peak of 7–12 jobs/s.

Cross-region costs throughput: 0.73 jobs/s/vCPU in the bucket's own region
against 0.40 spread out, because the package is re-fetched from S3 on every
job. See the package-caching entry in `~/ams-access/BACKLOG.md`.

Prerequisite that does exist: IAM role `ams-worker-role` + instance profile
`ams-worker-profile`.

If the account ever leaves the Free Tier plan, `judge-fleet.sh` and the
single-region ASG become the better option — fewer moving parts and no
cross-region penalty — and need a Spot vCPU quota increase to ~64.
