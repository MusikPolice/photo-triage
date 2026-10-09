#!/usr/bin/env bash
# A read-only report on a host's hardware and existing load, for sizing the
# Compose stack's CPU and memory caps to fit its headroom (dev-environment §5,
# "The Compose stack"). Run it on the server, not in dev:
#   bash host_report.sh [MINUTES]      default 10
# It changes nothing and needs no sudo; the container sections need the docker
# group. It samples the load for MINUTES, so run it once when the server is
# busiest and once when it's quiet. Writes ~/host-report-<host>-<time>.txt.
set -uo pipefail

minutes="${1:-10}"
[[ "$minutes" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 [MINUTES]" >&2; exit 2; }
out="$HOME/host-report-$(hostname -s)-$(date +%Y%m%d-%H%M).txt"
have() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n===== %s =====\n' "$1"; }
docker_ok=false
if have docker && docker info >/dev/null 2>&1; then docker_ok=true; fi
no_docker() { echo "skipped: docker isn't installed or isn't reachable by this user"; }

echo "Sampling for $minutes minute(s); writing $out"
{
  section "When"
  date
  uptime

  section "OS"
  head -4 /etc/os-release
  uname -r

  section "CPU"
  lscpu | grep -Ei 'model name|^cpu\(s\)|thread|core|socket|mhz'

  section "Memory"
  free -h
  grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree|Committed_AS' /proc/meminfo

  section "Swap and out-of-memory kills (last 30 days)"
  swapon --show
  echo "swappiness: $(cat /proc/sys/vm/swappiness)"
  kernel_log="$(journalctl -k --since "-30 days" 2>/dev/null)"
  if [[ -z "$kernel_log" ]]; then
    echo "unknown: this user can't read the kernel log (the adm or systemd-journal group can)"
  else
    grep -i 'out of memory' <<<"$kernel_log" | tail -5 || echo "none in the kernel log"
  fi

  section "Disks"
  df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs
  findmnt -no SOURCE,FSTYPE,OPTIONS /mnt/pictures 2>/dev/null || echo "/mnt/pictures isn't a mount"

  section "Docker"
  if $docker_ok; then
    docker version --format 'Engine {{.Server.Version}}'
    docker info --format 'cgroup v{{.CgroupVersion}} ({{.CgroupDriver}}), root {{.DockerRootDir}}, {{.NCPU}} CPUs, {{.MemTotal}} bytes'
  else
    no_docker
  fi

  section "Containers and their limits (cpus in billionths, memory in bytes; 0 = none)"
  if $docker_ok; then
    docker ps --format '{{.Names}}' | while read -r name; do
      docker inspect "$name" --format '{{.Name}}  image={{.Config.Image}} cpus={{.HostConfig.NanoCpus}} cpuset={{.HostConfig.CpusetCpus}} memory={{.HostConfig.Memory}} restart={{.HostConfig.RestartPolicy.Name}} restarts={{.RestartCount}} oom_killed={{.State.OOMKilled}} started={{.State.StartedAt}}'
    done
  else
    no_docker
  fi

  section "Container usage now"
  if $docker_ok; then
    docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
  else
    no_docker
  fi

  section "Top processes by CPU"
  ps -eo pid,comm,%cpu,%mem,rss --sort=-%cpu | head -15

  section "Top processes by memory (rss in KB)"
  ps -eo pid,comm,%cpu,%mem,rss --sort=-rss | head -15

  section "History (sysstat)"
  if have sar; then
    sar -u | tail -30
    sar -r | tail -30
  else
    echo "sysstat isn't installed"
  fi

  # Both samples run for MINUTES side by side; the containers' goes to a file
  # and is printed after.
  containers="$(mktemp)"
  if $docker_ok; then
    for _ in $(seq "$minutes"); do
      date +%T
      docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
      sleep 60
    done >"$containers" 2>&1 &
  else
    no_docker >"$containers"
  fi

  section "Load over $minutes minute(s) (vmstat every 10 s)"
  vmstat -w 10 $((minutes * 6))
  wait

  section "Container usage, every minute"
  cat "$containers"
  rm -f "$containers"
} >"$out" 2>&1

echo "Wrote $out"
