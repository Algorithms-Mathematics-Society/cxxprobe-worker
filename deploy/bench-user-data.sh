#!/usr/bin/env bash
#
# Benchmark pool machine bootstrap (cxxprobe #12).
#
# Runs once, as root, on a bare Ubuntu 24.04 x86_64 AMI on a c7i.xlarge.
# Same shape as spot-user-data.sh, with three differences that are the whole
# point of the pool:
#
#   * one physical core is given to the measured process: the OS, IRQs and
#     the worker itself are confined to CPU 0's core (bench-isolate-cpus.sh,
#     which reads the topology: on c7i.xlarge that core is CPUs 0+2);
#   * the worker runs `--env bench` (role: bencher, concurrency 1, the
#     benchmark queue only), and calibrates before it claims anything;
#   * the machine id it records with every measurement is instance id plus
#     availability zone, so a suspect host can be traced after the round.
#
# No credentials here; the instance role supplies AWS access.
set -euxo pipefail

exec > >(tee /var/log/bench-bootstrap.log) 2>&1

CXXPROBE_VERSION="${CXXPROBE_VERSION:-v1.1.0}"
WORKER_REF="${WORKER_REF:-main}"
REGION=ap-south-1

echo "── packages ───────────────────────────────────────────"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# binutils (nm) for profile symbolisation; the rest as for judging.
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl unzip git g++ g++-14 binutils cgroup-tools python3 python3-venv

echo "── aws cli ────────────────────────────────────────────"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp
/tmp/aws/install --update
rm -rf /tmp/awscli.zip /tmp/aws

echo "── the engine ─────────────────────────────────────────"
curl -fsSL -o /usr/local/bin/cxxprobe \
  "https://github.com/Algorithms-Mathematics-Society/cxxprobe/releases/download/${CXXPROBE_VERSION}/cxxprobe-x86_64-linux"
curl -fsSL -o /tmp/cxxprobe.sha256 \
  "https://github.com/Algorithms-Mathematics-Society/cxxprobe/releases/download/${CXXPROBE_VERSION}/cxxprobe-x86_64-linux.sha256"
echo "$(cat /tmp/cxxprobe.sha256)  /usr/local/bin/cxxprobe" | sha256sum -c -
chmod +x /usr/local/bin/cxxprobe
/usr/local/bin/cxxprobe bench --help >/dev/null

echo "── the worker ─────────────────────────────────────────"
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone --depth 1 --branch "$WORKER_REF" \
  https://github.com/Algorithms-Mathematics-Society/cxxprobe-worker.git /opt/cxxprobe-worker
cd /opt/cxxprobe-worker
uv sync --frozen --extra aws

echo "── identity ───────────────────────────────────────────"
IMDS_TOKEN=$(curl -fsSL -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
meta() { curl -fsSL -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
INSTANCE_ID=$(meta instance-id)
ZONE=$(meta placement/availability-zone)
TYPE=$(meta instance-type)
sed -i "s/^worker_id: .*/worker_id: bench-${INSTANCE_ID}/" config/bench.yaml
sed -i "s/^  machine_id: .*/  machine_id: \"${ZONE}\/${TYPE}\/${INSTANCE_ID}\"/" config/bench.yaml

echo "── secrets ────────────────────────────────────────────"
mkdir -p /etc/cxxprobe-worker
API_KEY=$(aws ssm get-parameter --region "$REGION" \
  --name /ams/prod/internal_api_secret --with-decryption \
  --query 'Parameter.Value' --output text)
install -m 600 /dev/null /etc/cxxprobe-worker/secrets.env
cat > /etc/cxxprobe-worker/secrets.env <<ENV
CXXPROBE_WORKER_CONTROL_PLANE__API_KEY=${API_KEY}
AWS_DEFAULT_REGION=${REGION}
ENV

echo "── one core for the measurement ───────────────────────"
/opt/cxxprobe-worker/deploy/bench-isolate-cpus.sh
. /etc/cxxprobe-worker/cpus.env
sed -i "s/^  cpu: .*/  cpu: ${BENCH_CPU}/" config/bench.yaml

mkdir -p /var/lib/cxxprobe-worker/workspaces /var/lib/cxxprobe-worker/packages \
         /var/run/cxxprobe-worker

echo "── service ────────────────────────────────────────────"
# Its own slice, which keeps every online CPU, so the measured child can be
# pinned to BENCH_CPU; the worker's own threads stay on the housekeeping core.
cat > /etc/systemd/system/bench.slice <<UNIT
[Slice]
AllowedCPUs=$(cat /sys/devices/system/cpu/online)
UNIT
cat > /etc/systemd/system/cxxprobe-bencher.service <<UNIT
[Unit]
Description=cxxprobe benchmark worker, one job at a time
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Slice=bench.slice
CPUAffinity=${HOUSEKEEPING//,/ }
WorkingDirectory=/opt/cxxprobe-worker
EnvironmentFile=/etc/cxxprobe-worker/secrets.env
ExecStart=/usr/local/bin/uv run --frozen cxxprobe-worker run --env bench
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=900

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now cxxprobe-bencher

sleep 60
if systemctl is-active --quiet cxxprobe-bencher; then
  echo "bencher alive after 60s"
  cat /var/run/cxxprobe-worker/health.json || true
else
  echo "BENCHER FAILED TO STAY UP"
  journalctl -u cxxprobe-bencher -n 40 --no-pager || true
  exit 1
fi
