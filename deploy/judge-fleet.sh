#!/usr/bin/env bash
#
# Contest-day judging capacity, on and off.
#
#   judge-fleet.sh up [N]    bring N workers up (default 12)
#   judge-fleet.sh down      back to zero
#   judge-fleet.sh status    what is running, and what the queue looks like
#
# **Why this exists.** The always-on worker on `ams-app` manages roughly
# 1,100 jobs/hour at `concurrency: 1` — it shares 1 GB of RAM with Postgres,
# Redis and the API, so it cannot be turned up. A 2,000-competitor contest
# submitting through a three-hour window needs about three times that. The
# fleet is the answer; the small box stays exactly as it is and keeps
# judging between contests for free.
#
# Cost: 12 × c6a.2xlarge spot ≈ $0.09/h each ≈ $3.30 for a three-hour
# contest. `down` is not optional — an ASG left at 12 is ~$79/day.
set -euo pipefail

REGION=ap-south-1
ASG=ams-judge-spot
QUEUE=https://sqs.ap-south-1.amazonaws.com/362249012864/submission-evaluation
DEFAULT_SIZE=12

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

queue_depth() {
  aws sqs get-queue-attributes --region "$REGION" --queue-url "$QUEUE" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' \
    --output text
}

case "${1:-}" in
  up)
    SIZE="${2:-$DEFAULT_SIZE}"
    aws autoscaling update-auto-scaling-group --region "$REGION" \
      --auto-scaling-group-name "$ASG" \
      --min-size 0 --max-size "$SIZE" --desired-capacity "$SIZE"
    echo "scaling to $SIZE. Boot to first job is about 90s."
    echo "Watch: $0 status"
    ;;
  down)
    aws autoscaling update-auto-scaling-group --region "$REGION" \
      --auto-scaling-group-name "$ASG" \
      --min-size 0 --max-size 0 --desired-capacity 0
    # Termination sends SIGTERM, which the worker treats as "stop claiming,
    # finish what you have" — so a job in flight when you run this still
    # gets its verdict rather than going back to the queue for 15 minutes.
    echo "scaling to 0. In-flight jobs finish first (up to 300s)."
    ;;
  status)
    echo "── instances ──────────────────────────────────────"
    aws autoscaling describe-auto-scaling-groups --region "$REGION" \
      --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].[DesiredCapacity,length(Instances)]' --output text \
      | awk '{print "desired: "$1"  running: "$2}'
    aws autoscaling describe-auto-scaling-groups --region "$REGION" \
      --auto-scaling-group-names "$ASG" \
      --query 'AutoScalingGroups[0].Instances[].[InstanceId,LifecycleState,HealthStatus]' \
      --output text || true
    echo "── queue ──────────────────────────────────────────"
    queue_depth | awk '{print "waiting: "$1"  being judged: "$2}'
    echo "── workers the control plane can see ──────────────"
    echo "(ams-api: SELECT hostname, last_heartbeat_at FROM evaluation.workers)"
    ;;
  *) usage ;;
esac
