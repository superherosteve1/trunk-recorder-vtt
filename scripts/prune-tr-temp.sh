#!/bin/bash
# Prune Trunk Recorder tempDir / captureDir after files age out.
#
# After upload.sh copies audio into sdr-trunk-vtt, TR still keeps originals under
# captureDir (and scratch under tempDir). This script deletes aged files only.
#
# Usage:
#   ./scripts/prune-tr-temp.sh
#   TR_MAX_AGE_HOURS=12 ./scripts/prune-tr-temp.sh
#   TR_CONFIG=config.json TR_MAX_AGE_HOURS=24 ./scripts/prune-tr-temp.sh --dry-run
#
# Cron example (every hour):
#   0 * * * * cd /path/to/sdr-trunk-vtt && ./scripts/prune-tr-temp.sh >>/tmp/tr-prune.log 2>&1

set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo_dir="$(cd "${_script_dir}/.." && pwd)"
TR_CONFIG="${TR_CONFIG:-${_repo_dir}/config.json}"
TR_MAX_AGE_HOURS="${TR_MAX_AGE_HOURS:-24}"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h)
      sed -n '2,20p' "$0"
      exit 0
      ;;
  esac
done

if ! [[ "$TR_MAX_AGE_HOURS" =~ ^[0-9]+$ ]] || [[ "$TR_MAX_AGE_HOURS" -lt 1 ]]; then
  echo "prune-tr-temp: TR_MAX_AGE_HOURS must be a positive integer" >&2
  exit 1
fi

if [[ ! -f "$TR_CONFIG" ]]; then
  echo "prune-tr-temp: config not found: $TR_CONFIG" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "prune-tr-temp: jq is required" >&2
  exit 1
fi

temp_dir="$(jq -r '.tempDir // empty' "$TR_CONFIG")"
capture_dir="$(jq -r '.captureDir // empty' "$TR_CONFIG")"

prune_dir() {
  local label="$1"
  local dir="$2"
  if [[ -z "$dir" ]]; then
    echo "prune-tr-temp: no ${label} configured; skipping"
    return 0
  fi
  if [[ ! -d "$dir" ]]; then
    echo "prune-tr-temp: ${label} missing (${dir}); skipping"
    return 0
  fi

  local count
  count="$(find "$dir" -type f -mmin "+$((TR_MAX_AGE_HOURS * 60))" 2>/dev/null | wc -l | tr -d ' ')"
  echo "prune-tr-temp: ${label}=${dir} age>${TR_MAX_AGE_HOURS}h files=${count}"

  if [[ "$count" -eq 0 ]]; then
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    find "$dir" -type f -mmin "+$((TR_MAX_AGE_HOURS * 60))" -print
    return 0
  fi

  find "$dir" -type f -mmin "+$((TR_MAX_AGE_HOURS * 60))" -delete
  # Remove empty date/system folders left behind.
  find "$dir" -type d -empty -delete 2>/dev/null || true
}

echo "prune-tr-temp: config=${TR_CONFIG} dry_run=${DRY_RUN}"
prune_dir "tempDir" "$temp_dir"
prune_dir "captureDir" "$capture_dir"
echo "prune-tr-temp: done"
