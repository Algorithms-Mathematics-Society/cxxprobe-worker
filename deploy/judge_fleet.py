#!/usr/bin/env python3
"""Contest-day judging capacity that follows the queue.

    judge_fleet.py init      create the fleet in every region, at zero
    judge_fleet.py scale     one control step: size the fleet to the backlog
    judge_fleet.py watch     scale every INTERVAL seconds until stopped
    judge_fleet.py status    what is running, and what is waiting
    judge_fleet.py destroy   remove everything

**Why this is spread over eighteen regions.** The AWS account is on the Free
Tier plan: only free-tier-eligible instance types may launch (the largest is
``c7i-flex.large``, 2 vCPU) and EC2 is capped at 5-8 vCPU *per region*. The
quota counts vCPUs rather than instances, so choosing smaller instances buys
nothing. The same quota exists in each region though, so the capacity is
there — roughly 84 usable vCPU — just spread out.

**Why a controller rather than target-tracking.** The obvious AWS answer is
an ASG scaling policy on an SQS metric, but the queue lives in one region and
the ASGs live in eighteen, and a scaling policy cannot read another region's
CloudWatch. So one controller reads the backlog once and sets desired
capacity everywhere. It is also the only place that can spread demand across
regions by their differing quotas.

**Scale out fast, scale in slow.** An instance takes ~90 s from launch to its
first job, so reacting late to a spike means candidates waiting. Coming down
has no such urgency and thrashing costs a full boot each time, so scale-in
waits for several consecutive quiet checks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

ACCOUNT = "362249012864"
HOME_REGION = "ap-south-1"
QUEUE = os.environ.get(
    "JUDGE_QUEUE", f"https://sqs.{HOME_REGION}.amazonaws.com/{ACCOUNT}/submission-evaluation"
)
NAME = "ams-judge"
TYPE = "c7i-flex.large"
VCPU = 2
PROFILE = "ams-worker-profile"
AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
ON_DEMAND_QUOTA = "L-1216C47A"

REGIONS = os.environ.get("JUDGE_REGIONS", "").split() or [
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-south-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ca-central-1",
    "eu-central-1",
    "eu-north-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "sa-east-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
]

# One instance (2 vCPU) sustains ~0.8 judged jobs/s once warm — measured
# across three regions, 1,000 real submissions. Holding ~24 queued jobs per
# instance therefore means a submission arriving at the back of the queue
# waits about 30 s, which is the longest a candidate should stare at
# "queued" without wondering whether it broke.
BACKLOG_PER_INSTANCE = int(os.environ.get("JUDGE_BACKLOG_PER_INSTANCE", "24"))

# Consecutive quiet control steps before shrinking. At a 60 s interval that
# is three minutes of genuinely empty queue — long enough that the lull
# between two problems' submission bursts does not cost a fleet.
QUIET_STEPS_BEFORE_SCALE_IN = 3

# Scale-in hysteresis has to survive the process, not just the loop. The
# controller is meant to run from a timer, which means every step is a fresh
# process — an in-memory counter resets to zero each time, never reaches the
# threshold, and the fleet never comes down. That is a $79/day bug, so the
# counter lives on disk.
STATE_FILE = Path(os.environ.get("JUDGE_STATE", "/var/tmp/ams-judge-fleet.json"))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError:
        # A controller that cannot persist still has to keep controlling;
        # it just loses scale-in hysteresis until the path is writable.
        print(json.dumps({"warning": f"cannot write {STATE_FILE}"}), file=sys.stderr)


def aws(*args: str, region: str | None = None) -> str:
    cmd = ["aws", *args]
    if region:
        cmd += ["--region", region]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"aws {' '.join(args)} failed")
    return out.stdout.strip()


def aws_quiet(*args: str, region: str | None = None) -> str | None:
    try:
        return aws(*args, region=region)
    except RuntimeError:
        return None


def parallel(fn, items):
    with concurrent.futures.ThreadPoolExecutor(max_workers=18) as pool:
        return list(pool.map(fn, items))


# ── capacity ──────────────────────────────────────────────────────────────


def region_max_instances(region: str) -> int:
    """How many instances this region's quota allows us, leaving others' alone."""
    quota = aws_quiet(
        "service-quotas",
        "get-service-quota",
        "--service-code",
        "ec2",
        "--quota-code",
        ON_DEMAND_QUOTA,
        "--query",
        "Quota.Value",
        "--output",
        "text",
        region=region,
    )
    if not quota:
        return 0
    used = aws_quiet(
        "ec2",
        "describe-instances",
        "--filters",
        "Name=instance-state-name,Values=running,pending",
        "--query",
        "length(Reservations[].Instances[])",
        "--output",
        "text",
        region=region,
    )
    # Anything already running in the region belongs to something else and
    # its vCPUs are not ours to take.
    ours = instance_count(region)
    spare = int(float(quota)) - VCPU * (int(used or 0) - ours)
    return max(0, spare // VCPU)


def instance_count(region: str) -> int:
    n = aws_quiet(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag:Name,Values={NAME}",
        "Name=instance-state-name,Values=running,pending",
        "--query",
        "length(Reservations[].Instances[])",
        "--output",
        "text",
        region=region,
    )
    return int(n or 0)


def backlog() -> tuple[int, int]:
    """(waiting, being judged). Waiting is what sizing keys off."""
    out = aws(
        "sqs",
        "get-queue-attributes",
        "--queue-url",
        QUEUE,
        "--attribute-names",
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "--query",
        "Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]",
        "--output",
        "text",
        region=HOME_REGION,
    )
    waiting, inflight = (int(x) for x in out.split())
    return waiting, inflight


def spread(total: int, caps: dict[str, int]) -> dict[str, int]:
    """Place `total` instances across regions, filling the roomiest first.

    Round-robin rather than filling one region at a time: a fleet concentrated
    in a single region dies with that region, and spreading also keeps each
    region's own quota headroom intact for whatever else runs there.
    """
    plan = dict.fromkeys(caps, 0)
    remaining = min(total, sum(caps.values()))
    while remaining > 0:
        progressed = False
        for region in sorted(caps, key=lambda r: caps[r] - plan[r], reverse=True):
            if remaining == 0:
                break
            if plan[region] < caps[region]:
                plan[region] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return plan


# ── the fleet ─────────────────────────────────────────────────────────────


def ensure_region(region: str, user_data: str) -> str:
    """Security group, launch template and ASG — created once, then reused."""
    vpc = aws_quiet(
        "ec2",
        "describe-vpcs",
        "--filters",
        "Name=isDefault,Values=true",
        "--query",
        "Vpcs[0].VpcId",
        "--output",
        "text",
        region=region,
    )
    if not vpc or vpc == "None":
        return f"{region}: no default VPC, skipped"

    sg = aws_quiet(
        "ec2",
        "describe-security-groups",
        "--filters",
        f"Name=group-name,Values={NAME}",
        "--query",
        "SecurityGroups[0].GroupId",
        "--output",
        "text",
        region=region,
    )
    if not sg or sg == "None":
        sg = aws_quiet(
            "ec2",
            "create-security-group",
            "--group-name",
            NAME,
            "--description",
            "cxxprobe judging workers: egress only",
            "--vpc-id",
            vpc,
            "--query",
            "GroupId",
            "--output",
            "text",
            region=region,
        )
    if not sg:
        return f"{region}: no security group, skipped"

    ami = aws_quiet(
        "ssm",
        "get-parameter",
        "--name",
        AMI_PARAM,
        "--query",
        "Parameter.Value",
        "--output",
        "text",
        region=region,
    )
    if not ami:
        return f"{region}: no AMI, skipped"

    # An instance type is offered per availability zone, not per region, and
    # the default VPC has a subnet in every AZ — including ones that do not
    # offer it. Launching blind fails with `Unsupported` in about one region
    # in three.
    azs = aws_quiet(
        "ec2",
        "describe-instance-type-offerings",
        "--location-type",
        "availability-zone",
        "--filters",
        f"Name=instance-type,Values={TYPE}",
        "--query",
        "InstanceTypeOfferings[].Location",
        "--output",
        "text",
        region=region,
    )
    if not azs:
        return f"{region}: {TYPE} offered in no AZ, skipped"
    subnets = aws_quiet(
        "ec2",
        "describe-subnets",
        "--filters",
        f"Name=vpc-id,Values={vpc}",
        f"Name=availability-zone,Values={','.join(azs.split())}",
        "--query",
        "Subnets[].SubnetId",
        "--output",
        "text",
        region=region,
    )
    if not subnets:
        return f"{region}: no usable subnet, skipped"

    spec = {
        "ImageId": ami,
        "InstanceType": TYPE,
        "IamInstanceProfile": {"Name": PROFILE},
        "SecurityGroupIds": [sg],
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        "UserData": user_data,
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}
        ],
    }
    payload = json.dumps(spec)
    existing = aws_quiet(
        "ec2",
        "describe-launch-templates",
        "--launch-template-names",
        NAME,
        "--query",
        "LaunchTemplates[0].LaunchTemplateId",
        "--output",
        "text",
        region=region,
    )
    if existing and existing != "None":
        aws_quiet(
            "ec2",
            "create-launch-template-version",
            "--launch-template-name",
            NAME,
            "--launch-template-data",
            payload,
            region=region,
        )
        aws_quiet(
            "ec2",
            "modify-launch-template",
            "--launch-template-name",
            NAME,
            "--default-version",
            "$Latest",
            region=region,
        )
    else:
        aws_quiet(
            "ec2",
            "create-launch-template",
            "--launch-template-name",
            NAME,
            "--launch-template-data",
            payload,
            region=region,
        )

    cap = region_max_instances(region)
    asg_exists = aws_quiet(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        NAME,
        "--query",
        "AutoScalingGroups[0].AutoScalingGroupName",
        "--output",
        "text",
        region=region,
    )
    args = [
        "--auto-scaling-group-name",
        NAME,
        "--launch-template",
        f"LaunchTemplateName={NAME},Version=$Latest",
        "--min-size",
        "0",
        "--max-size",
        str(max(cap, 1)),
        "--vpc-zone-identifier",
        ",".join(subnets.split()),
        "--health-check-type",
        "EC2",
        "--health-check-grace-period",
        "180",
    ]
    if asg_exists and asg_exists != "None":
        aws_quiet("autoscaling", "update-auto-scaling-group", *args, region=region)
    else:
        aws_quiet(
            "autoscaling",
            "create-auto-scaling-group",
            *args,
            "--desired-capacity",
            "0",
            "--tags",
            f"Key=Name,Value={NAME},PropagateAtLaunch=true",
            region=region,
        )
    return f"{region}: ready, max {cap} instances ({cap * VCPU} vCPU)"


def set_desired(region: str, n: int) -> None:
    aws_quiet(
        "autoscaling",
        "update-auto-scaling-group",
        "--auto-scaling-group-name",
        NAME,
        "--desired-capacity",
        str(n),
        "--min-size",
        "0",
        region=region,
    )


def asg_desired(region: str) -> int | None:
    out = aws_quiet(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        NAME,
        "--query",
        "AutoScalingGroups[0].DesiredCapacity",
        "--output",
        "text",
        region=region,
    )
    if out in (None, "", "None"):
        return None
    return int(out)


# ── control ───────────────────────────────────────────────────────────────


def control_step(state: dict, dry_run: bool = False) -> dict:
    waiting, inflight = backlog()
    caps = dict(zip(REGIONS, parallel(region_max_instances, REGIONS), strict=True))
    ceiling = sum(caps.values())

    wanted = math.ceil(waiting / BACKLOG_PER_INSTANCE) if waiting else 0
    # Never drop below what is mid-flight: those jobs are on instances that
    # would otherwise be terminated out from under them.
    if inflight:
        wanted = max(wanted, math.ceil(inflight / BACKLOG_PER_INSTANCE))
    wanted = min(wanted, ceiling)

    current = sum(x for x in parallel(asg_desired, REGIONS) if x)

    if wanted >= current:
        state["quiet"] = 0
        target = wanted
    else:
        # Shrinking: only after several consecutive quiet steps.
        state["quiet"] = state.get("quiet", 0) + 1
        if state["quiet"] < QUIET_STEPS_BEFORE_SCALE_IN:
            target = current
        else:
            target = wanted
            state["quiet"] = 0

    plan = spread(target, caps)
    report = {
        "waiting": waiting,
        "inflight": inflight,
        "current": current,
        "target": target,
        "ceiling": ceiling,
        "quiet_steps": state.get("quiet", 0),
    }
    if not dry_run and target != current:
        parallel(lambda r: set_desired(r, plan[r]), REGIONS)
        report["applied"] = {r: n for r, n in plan.items() if n}
    print(json.dumps(report))
    return state


def main() -> int:
    ap = argparse.ArgumentParser(prog="judge_fleet.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("--user-data", required=True)
    sub.add_parser("scale").add_argument("--dry-run", action="store_true")
    w = sub.add_parser("watch")
    w.add_argument("--interval", type=int, default=60)
    sub.add_parser("status")
    sub.add_parser("destroy")
    a = ap.parse_args()

    if a.cmd == "init":
        import base64

        ud = base64.b64encode(Path(a.user_data).read_bytes()).decode()
        for line in parallel(lambda r: ensure_region(r, ud), REGIONS):
            print(line)
    elif a.cmd == "scale":
        save_state(control_step(load_state(), dry_run=a.dry_run))
    elif a.cmd == "watch":
        state = load_state()
        print(f"controlling every {a.interval}s; Ctrl-C to stop", file=sys.stderr)
        while True:
            try:
                state = control_step(state)
                save_state(state)
            except Exception as exc:  # a loop that dies mid-contest is the worst outcome
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
            time.sleep(a.interval)
    elif a.cmd == "status":
        counts = dict(zip(REGIONS, parallel(instance_count, REGIONS), strict=True))
        total = sum(counts.values())
        for r, n in counts.items():
            if n:
                print(f"  {r}: {n}")
        print(f"total: {total} instances = {total * VCPU} vCPU")
        waiting, inflight = backlog()
        print(f"queue — waiting: {waiting}  judging: {inflight}")
    elif a.cmd == "destroy":

        def kill(region: str) -> str:
            if asg_desired(region) is not None:
                aws_quiet(
                    "autoscaling",
                    "delete-auto-scaling-group",
                    "--auto-scaling-group-name",
                    NAME,
                    "--force-delete",
                    region=region,
                )
            aws_quiet(
                "ec2", "delete-launch-template", "--launch-template-name", NAME, region=region
            )
            return f"{region}: removed"

        for line in parallel(kill, REGIONS):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
