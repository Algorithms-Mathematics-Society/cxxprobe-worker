#!/usr/bin/env bash
#
# Contest-day judging capacity, assembled from what the account actually has.
#
#   judge-fleet-multiregion.sh up      launch everywhere, within quota
#   judge-fleet-multiregion.sh down    terminate everywhere
#   judge-fleet-multiregion.sh status  instances per region + queue depth
#
# **Why this is spread over eighteen regions.** The account is on the AWS
# Free Tier plan, which permits only free-tier-eligible instance types — the
# largest being `c7i-flex.large` at 2 vCPU — and caps EC2 at 5–8 vCPU *per
# region*. One region therefore cannot hold a contest-sized fleet, and no
# amount of picking smaller instances changes that: the quota counts vCPUs,
# not instances.
#
# What it *cannot* do is stop being per-region. Eighteen regions at 5–8 vCPU
# is ~84 usable vCPU, which is the same order as the 96 the original
# single-region plan assumed — so the capacity exists, just spread out.
#
# Everything still points at ONE queue and ONE bucket, both in ap-south-1.
# Workers poll it cross-region. That is fine: a job is seconds of compiling
# against a few KB of input, so an extra ~200 ms of round-trip per poll is
# noise, and SQS long-polling amortises it away entirely.
#
# Cost, all regions, on-demand c7i-flex.large ≈ $0.09/h:
#   42 instances × $0.09 ≈ $3.8/hour ≈ $11 for a three-hour contest.
# Spot roughly a third of that. `down` is not optional.
set -uo pipefail

HOME_REGION=ap-south-1
QUEUE="https://sqs.${HOME_REGION}.amazonaws.com/362249012864/submission-evaluation"
NAME=ams-judge
TYPE=c7i-flex.large
VCPU_PER_INSTANCE=2
# Canonical's public SSM parameter resolves the current Ubuntu 24.04 AMI in
# whichever region it is queried from — hardcoding eighteen AMI ids would be
# eighteen things to get wrong.
AMI_PARAM=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id

REGIONS="${JUDGE_REGIONS:-ap-northeast-1 ap-northeast-2 ap-northeast-3 ap-south-1 ap-south-2 ap-southeast-1 ap-southeast-2 ca-central-1 eu-central-1 eu-north-1 eu-west-1 eu-west-2 eu-west-3 sa-east-1 us-east-1 us-east-2 us-west-1 us-west-2}"

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

# How many instances fit in this region's spare quota.
capacity() {
  local region="$1"
  local quota
  quota=$(aws service-quotas get-service-quota --region "$region" \
    --service-code ec2 --quota-code L-1216C47A --query 'Quota.Value' --output text 2>/dev/null)
  [ -z "$quota" ] && { echo 0; return; }
  # Leave whatever is already running alone — another workload's instances
  # count against the same quota, and stealing its headroom is not our call.
  local used
  used=$(aws ec2 describe-instances --region "$region" \
    --filters "Name=instance-state-name,Values=running,pending" \
    --query 'length(Reservations[].Instances[])' --output text 2>/dev/null)
  python3 -c "print(max(0, int((${quota%.*} - 2*${used:-0}) // $VCPU_PER_INSTANCE)))"
}

launch_region() {
  local region="$1" n="$2" userdata="$3"
  local ami sg subnet vpc
  ami=$(aws ssm get-parameter --region "$region" --name "$AMI_PARAM" --query 'Parameter.Value' --output text 2>/dev/null)
  [ -z "$ami" ] && { echo "$region: no AMI, skipped"; return; }
  vpc=$(aws ec2 describe-vpcs --region "$region" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text 2>/dev/null)
  sg=$(aws ec2 describe-security-groups --region "$region" --filters "Name=group-name,Values=$NAME" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
  if [ -z "$sg" ] || [ "$sg" = "None" ]; then
    sg=$(aws ec2 create-security-group --region "$region" --group-name "$NAME" \
      --description "cxxprobe judging workers: egress only" --vpc-id "$vpc" --query 'GroupId' --output text 2>/dev/null)
  fi
  # Not just any subnet: an instance type is offered per *availability zone*,
  # and the default VPC has one subnet per AZ including ones that do not
  # offer it. Picking blind fails with `Unsupported` in roughly one region in
  # three — us-east-1e does not offer c7i-flex.large, for instance.
  local azs
  azs=$(aws ec2 describe-instance-type-offerings --region "$region" \
    --location-type availability-zone --filters "Name=instance-type,Values=$TYPE" \
    --query 'InstanceTypeOfferings[].Location' --output text 2>/dev/null | tr '\t' ',')
  [ -z "$azs" ] && { echo "$region: $TYPE offered in no AZ, skipped"; return; }
  subnet=$(aws ec2 describe-subnets --region "$region" \
    --filters "Name=vpc-id,Values=$vpc" "Name=availability-zone,Values=$azs" \
    --query 'Subnets[0].SubnetId' --output text 2>/dev/null)
  [ -z "$subnet" ] || [ "$subnet" = "None" ] && { echo "$region: no usable subnet, skipped"; return; }
  aws ec2 run-instances --region "$region" --image-id "$ami" --instance-type "$TYPE" --count "$n" \
    --iam-instance-profile Name=ams-worker-profile \
    --security-group-ids "$sg" --subnet-id "$subnet" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --user-data "file://$userdata" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'length(Instances)' --output text 2>&1 \
    | tail -1 | xargs -I{} echo "$region: {}"
}

case "${1:-}" in
  up)
    UD="${2:-$(dirname "$0")/spot-user-data.sh}"
    [ -f "$UD" ] || { echo "no user-data at $UD" >&2; exit 1; }
    for r in $REGIONS; do
      n=$(capacity "$r")
      [ "$n" -gt 0 ] && launch_region "$r" "$n" "$UD" &
    done
    wait
    echo "all regions requested; first job in ~90s"
    ;;
  down)
    for r in $REGIONS; do
      (ids=$(aws ec2 describe-instances --region "$r" \
         --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running,pending" \
         --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null)
       [ -n "$ids" ] && aws ec2 terminate-instances --region "$r" --instance-ids $ids >/dev/null 2>&1 \
         && echo "$r: terminated $(echo $ids | wc -w)") &
    done
    wait
    echo "everything down"
    ;;
  status)
    total=0
    for r in $REGIONS; do
      n=$(aws ec2 describe-instances --region "$r" --filters "Name=tag:Name,Values=$NAME" \
        "Name=instance-state-name,Values=running,pending" \
        --query 'length(Reservations[].Instances[])' --output text 2>/dev/null)
      [ "${n:-0}" -gt 0 ] && { echo "  $r: $n"; total=$((total + n)); }
    done
    echo "total: $total instances = $((total * VCPU_PER_INSTANCE)) vCPU"
    aws sqs get-queue-attributes --region "$HOME_REGION" --queue-url "$QUEUE" \
      --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
      --query 'Attributes.[ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible]' \
      --output text 2>/dev/null | awk '{print "queue — waiting: "$1"  judging: "$2}'
    ;;
  *) usage ;;
esac
