#!/usr/bin/env bash
set -euo pipefail

ts="$(date +%Y%m%d-%H%M%S)"
out_dir="${1:-$HOME/crash-logs/$ts}"
mkdir -p "$out_dir"

run_journalctl() {
  if sudo -n true 2>/dev/null; then
    sudo journalctl "$@"
  else
    journalctl "$@"
  fi
}

{
  echo "Collected at: $(date -Is)"
  echo "Host: $(hostname)"
  echo "Kernel: $(uname -r)"
  echo
  echo "=== Last boots ==="
  journalctl --list-boots || true
  echo
  echo "=== Cmdline ==="
  cat /proc/cmdline || true
} >"$out_dir/summary.txt"

run_journalctl -b -1 -p warning..alert --no-pager >"$out_dir/prevboot-warn-alert.log" || true
run_journalctl -k -b -1 --no-pager >"$out_dir/prevboot-kernel.log" || true
run_journalctl -b -1 --no-pager >"$out_dir/prevboot-full.log" || true

ls -lah /var/crash >"$out_dir/var-crash-ls.txt" 2>&1 || true
ls -lah /sys/fs/pstore >"$out_dir/pstore-ls.txt" 2>&1 || true

if [ -d /var/crash ]; then
  tar -czf "$out_dir/var-crash.tar.gz" -C / var/crash 2>/dev/null || true
fi

if [ -d /sys/fs/pstore ]; then
  tar -czf "$out_dir/pstore.tar.gz" -C / sys/fs/pstore 2>/dev/null || true
fi

echo "Crash logs collected in: $out_dir"
