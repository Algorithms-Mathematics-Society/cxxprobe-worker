#!/usr/bin/env bash
#
# The benchmark pool, on and off (cxxprobe #12).
#
#   bench-pool.sh up [N]    N homogeneous benchers (default 3)
#   bench-pool.sh down      terminate every bencher
#   bench-pool.sh status    what is running, and each machine's health
#
# Separate from the judging fleet on purpose: one region, one availability
# zone, one instance type, on-demand (a spot reclaim mid-benchmark is a lost
# measurement), one benchmark at a time per machine. Throughput comes from
# more machines, never more parallelism per machine.
#
# Sizing: correctness gates benchmarking and integration is per team, so 20
# teams iterating every ten minutes over three hours is about 360 runs,
# which 2-4 machines carry. 3 x c7i.xlarge on-demand in ap-south-1 for five
# hours is a few dollars. `down` is not optional.
set -euo pipefail

REGION=ap-south-1
ZONE="${BENCH_ZONE:-ap-south-1a}"
TYPE=c7i.xlarge
TAG=ams-bench
PROFILE="${BENCH_INSTANCE_PROFILE:-ams-judge-worker}"
DEFAULT_SIZE=3
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

running() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Pool,Values=$TAG" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

case "${1:-}" in
  up)
    SIZE="${2:-$DEFAULT_SIZE}"
    AMI=$(aws ssm get-parameter --region "$REGION" \
      --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
      --query Parameter.Value --output text)
    aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$TYPE" \
      --count "$SIZE" --placement "AvailabilityZone=$ZONE" \
      --iam-instance-profile "Name=$PROFILE" \
      --metadata-options HttpTokens=required \
      --user-data "file://$HERE/bench-user-data.sh" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Pool,Value=$TAG},{Key=Name,Value=$TAG}]" \
      --query 'Instances[].InstanceId' --output text
    echo "launching $SIZE x $TYPE in $ZONE. Boot to first calibration is about 3 minutes."
    ;;
  down)
    IDS=$(running)
    if [ -n "$IDS" ]; then
      # shellcheck disable=SC2086
      aws ec2 terminate-instances --region "$REGION" --instance-ids $IDS \
        --query 'TerminatingInstances[].InstanceId' --output text
    fi
    echo "pool down."
    ;;
  status)
    aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Pool,Values=$TAG" "Name=instance-state-name,Values=pending,running" \
      --query 'Reservations[].Instances[].[InstanceId,InstanceType,Placement.AvailabilityZone,State.Name,LaunchTime]' \
      --output table
    ;;
  *) usage ;;
esac
