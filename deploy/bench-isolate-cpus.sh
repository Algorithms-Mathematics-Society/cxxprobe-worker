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
#   HOUSEKEEPING=0-1 BENCH_CPU=2 bench-isolate-cpus.sh
set -euo pipefail

HOUSEKEEPING="${HOUSEKEEPING:-0-1}"
BENCH_CPU="${BENCH_CPU:-2}"

for slice in system.slice user.slice init.scope; do
  systemctl set-property --runtime "$slice" "AllowedCPUs=${HOUSEKEEPING}" || true
done

# irqbalance would move interrupts back onto the measured core.
systemctl disable --now irqbalance 2>/dev/null || true
for irq in /proc/irq/*/smp_affinity_list; do
  echo "$HOUSEKEEPING" > "$irq" 2>/dev/null || true
done
if [ -w /sys/devices/virtual/workqueue/cpumask ]; then
  # The cpumask file takes a hex mask; CPUs 0-1 -> 3.
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
  echo performance > "$gov" 2>/dev/null || true
done

SIBLINGS=$(cat "/sys/devices/system/cpu/cpu${BENCH_CPU}/topology/thread_siblings_list")
echo "bench cpu ${BENCH_CPU} (core ${SIBLINGS}); housekeeping ${HOUSEKEEPING}"
