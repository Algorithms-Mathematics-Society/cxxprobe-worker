#!/usr/bin/env bash
#
# Give one physical core to the measured process and nothing else.
#
# Sourced by bench-user-data.sh, and usable on its own on any bench host.
# Without a reboot (no isolcpus): every existing systemd slice, every IRQ
# and the kernel's unbound workqueues are confined to the housekeeping CPUs;
# the bench service's slice keeps all CPUs so cxxprobe can pin a measured
# process to BENCH_CPU, whose sibling then has nothing scheduled on it.
#
# Topology comes from sysfs, never assumed: hyperthread numbering differs
# between hosts, and on c7i.xlarge the pairs are CPUs 0+2 and 1+3, so "0-1"
# would be two different cores. Housekeeping is CPU 0's whole physical core;
# the measured CPU is the first CPU of another core. Both are written to
# /etc/cxxprobe-worker/cpus.env for the service unit and the worker config.
#
#   bench-isolate-cpus.sh            # choose from the topology
#   HOUSEKEEPING=0,2 BENCH_CPU=1 bench-isolate-cpus.sh
set -euo pipefail

topology() { cat "/sys/devices/system/cpu/cpu$1/topology/thread_siblings_list"; }
expand() {
  python3 -c 'import sys
out = []
for part in sys.argv[1].split(","):
    a, _, b = part.partition("-")
    out += range(int(a), int(b or a) + 1)
print(" ".join(map(str, out)))' "$1"
}

HOUSEKEEPING="${HOUSEKEEPING:-$(topology 0)}"
if [ -z "${BENCH_CPU:-}" ]; then
  for cpu in $(expand "$(cat /sys/devices/system/cpu/online)"); do
    if ! [[ " $(expand "$HOUSEKEEPING") " == *" $cpu "* ]]; then
      BENCH_CPU=$cpu
      break
    fi
  done
fi
if [ -z "${BENCH_CPU:-}" ]; then
  echo "no CPU outside the housekeeping core ${HOUSEKEEPING}: this host cannot isolate a measurement" >&2
  exit 1
fi

for slice in system.slice user.slice init.scope; do
  systemctl set-property --runtime "$slice" "AllowedCPUs=${HOUSEKEEPING}" || true
done

# irqbalance would move interrupts back onto the measured core.
systemctl disable --now irqbalance 2>/dev/null || true
for irq in /proc/irq/*/smp_affinity_list; do
  echo "$HOUSEKEEPING" > "$irq" 2>/dev/null || true
done
if [ -w /sys/devices/virtual/workqueue/cpumask ]; then
  # The cpumask file takes a hex mask; CPUs 0,2 -> 5.
  python3 - "$HOUSEKEEPING" > /sys/devices/virtual/workqueue/cpumask <<'PY'
import sys
mask = 0
for part in sys.argv[1].split(","):
    a, _, b = part.partition("-")
    for cpu in range(int(a), int(b or a) + 1):
        mask |= 1 << cpu
print(f"{mask:x}")
PY
fi

# The measured core runs at a fixed, maximum frequency when the host allows
# it (Nitro guests usually expose no cpufreq; that is fine).
for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [ -e "$gov" ] && echo performance > "$gov" 2>/dev/null || true
done

SIBLINGS=$(topology "$BENCH_CPU")
mkdir -p /etc/cxxprobe-worker
printf 'HOUSEKEEPING=%s\nBENCH_CPU=%s\nBENCH_CORE=%s\n' "$HOUSEKEEPING" "$BENCH_CPU" "$SIBLINGS" \
  > /etc/cxxprobe-worker/cpus.env
echo "bench cpu ${BENCH_CPU} (core ${SIBLINGS}); housekeeping ${HOUSEKEEPING}"
