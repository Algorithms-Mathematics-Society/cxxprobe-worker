#!/usr/bin/env bash
#
# Contest-day Spot worker bootstrap.
#
# Runs once, as root, on a bare Ubuntu 24.04 x86_64 AMI. Everything a judge
# host needs, from nothing to draining the queue, in about 90 seconds.
#
# **Why x86_64.** The worker itself is Python and would run anywhere, but it
# shells out to `/usr/local/bin/cxxprobe`, and the only published build of
# that is `cxxprobe-x86_64-linux`. The instance type is `c7i-flex.large`
# because the account's Free Tier plan permits nothing larger — see
# ~/infra.md.
#
# **No credentials here.** This file is visible to anyone who can read the
# launch template. The instance role supplies AWS access; the control-plane
# key comes from SSM at the bottom.
set -euxo pipefail

exec > >(tee /var/log/spot-bootstrap.log) 2>&1

CXXPROBE_VERSION="${CXXPROBE_VERSION:-v0.14.0}"
WORKER_REF="${WORKER_REF:-main}"
REGION=ap-south-1

echo "── packages ───────────────────────────────────────────"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# `cgroup-tools` because cxxprobe creates a cgroup per run; `unzip` for the
# AWS CLI; g++ is what it actually compiles submissions with.
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl unzip git g++ cgroup-tools python3 python3-venv

echo "── aws cli ────────────────────────────────────────────"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp
/tmp/aws/install --update
rm -rf /tmp/awscli.zip /tmp/aws

echo "── the judge binary ───────────────────────────────────"
# Pinned to a release, checksum-verified. A worker that silently judged with
# a different cxxprobe than the rest of the fleet would produce verdicts
# nobody could reproduce.
curl -fsSL -o /usr/local/bin/cxxprobe \
  "https://github.com/Algorithms-Mathematics-Society/cxxprobe/releases/download/${CXXPROBE_VERSION}/cxxprobe-x86_64-linux"
curl -fsSL -o /tmp/cxxprobe.sha256 \
  "https://github.com/Algorithms-Mathematics-Society/cxxprobe/releases/download/${CXXPROBE_VERSION}/cxxprobe-x86_64-linux.sha256"
echo "$(cat /tmp/cxxprobe.sha256)  /usr/local/bin/cxxprobe" | sha256sum -c -
chmod +x /usr/local/bin/cxxprobe
/usr/local/bin/cxxprobe --version

echo "── the worker ─────────────────────────────────────────"
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
git clone --depth 1 --branch "$WORKER_REF" \
  https://github.com/Algorithms-Mathematics-Society/cxxprobe-worker.git /opt/cxxprobe-worker
cd /opt/cxxprobe-worker
# `--frozen` so a contest-day boot can never resolve a dependency tree that
# was not the tested one.
uv sync --frozen --extra aws

echo "── identity ───────────────────────────────────────────"
# IMDSv2. The token call is what makes this work on an instance with
# HttpTokens=required, which is the default worth keeping.
IMDS_TOKEN=$(curl -fsSL -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
INSTANCE_ID=$(curl -fsSL -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id)
# Twelve identical hosts are indistinguishable in the control plane
# otherwise, and "which worker dropped that job" stops being answerable.
sed -i "s/^worker_id: .*/worker_id: spot-${INSTANCE_ID}/" config/spot.yaml

echo "── secrets ────────────────────────────────────────────"
mkdir -p /etc/cxxprobe-worker
API_KEY=$(aws ssm get-parameter --region "$REGION" \
  --name /ams/prod/internal_api_secret --with-decryption \
  --query 'Parameter.Value' --output text)
install -m 600 /dev/null /etc/cxxprobe-worker/secrets.env
cat > /etc/cxxprobe-worker/secrets.env <<EOF
CXXPROBE_WORKER_CONTROL_PLANE__API_KEY=${API_KEY}
# The queue and the bucket both live in ap-south-1. A worker in Oregon must
# talk to those, not to its own region's — boto3 would otherwise default to
# wherever the instance happens to be and find nothing.
AWS_DEFAULT_REGION=${REGION}
EOF

echo "── directories ────────────────────────────────────────"
# `packages` sits beside `workspaces` because the cache outlives any one
# job: a package fetched for the first submission serves every one after it,
# which is what makes a cross-region fleet affordable. See packages.py.
mkdir -p /var/lib/cxxprobe-worker/workspaces /var/lib/cxxprobe-worker/packages \
         /var/run/cxxprobe-worker

echo "── service ────────────────────────────────────────────"
cat > /etc/systemd/system/cxxprobe-worker.service <<'EOF'
[Unit]
Description=cxxprobe judging worker (contest-day spot fleet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Root because creating a cgroup root needs privilege — the same reason the
# long-lived worker runs as root. See ~/infra.md.
User=root
WorkingDirectory=/opt/cxxprobe-worker
EnvironmentFile=/etc/cxxprobe-worker/secrets.env
ExecStart=/usr/local/bin/uv run --frozen cxxprobe-worker run --env spot
Restart=always
RestartSec=5
# Graceful shutdown: SIGTERM stops the worker claiming new jobs and lets the
# in-flight ones finish. 300s matches the judge timeout, so the longest
# legitimate job can complete rather than being killed and redelivered.
KillSignal=SIGTERM
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cxxprobe-worker

echo "── spot interruption handler ──────────────────────────"
# EC2 gives two minutes' notice. Stopping the worker on that notice converts
# a hard kill — which would drop in-flight jobs back to the queue after the
# 900s visibility timeout, i.e. a candidate staring at "queued" for fifteen
# minutes — into a graceful drain that finishes them.
cat > /usr/local/bin/spot-drain <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while true; do
  TOKEN=$(curl -fsSL -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null || true)
  if [ -n "${TOKEN:-}" ] && curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" \
       http://169.254.169.254/latest/meta-data/spot/instance-action >/dev/null 2>&1; then
    logger -t spot-drain "interruption notice received; draining"
    systemctl stop cxxprobe-worker
    exit 0
  fi
  sleep 5
done
EOF
chmod +x /usr/local/bin/spot-drain
cat > /etc/systemd/system/spot-drain.service <<'EOF'
[Unit]
Description=Drain the judging worker on a Spot interruption notice

[Service]
Type=simple
ExecStart=/usr/local/bin/spot-drain
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now spot-drain

echo "── ready ──────────────────────────────────────────────"
# `is-active` immediately after `enable --now` proves nothing: a unit that
# exits 2 on every start reports "active" in the window between restarts.
# That is precisely how a fleet crash-looped for twenty minutes on
# 2026-09-18 while every signal said healthy. So wait past a couple of
# restart intervals and check it is *still* up, and dump the journal if not
# — the console log is the only thing anyone can read on a box with no key.
sleep 30
if systemctl is-active --quiet cxxprobe-worker; then
  echo "worker alive after 30s"
  journalctl -u cxxprobe-worker -n 5 --no-pager -o cat || true
else
  echo "WORKER FAILED TO STAY UP"
  systemctl status cxxprobe-worker --no-pager || true
  journalctl -u cxxprobe-worker -n 40 --no-pager || true
  exit 1
fi
