#!/bin/bash
# Run Trunk Recorder and forward skipped-recording log lines to sdr-trunk-vtt.
#
# Usage (from repo root):
#   ./scripts/run-trunk-recorder.sh [config.json]
#
# Replaces: trunk-recorder config.json
# Stdout/stderr are unchanged; encrypted / unknown-TG hits are POSTed to the VTT API.
# Unknown TGs are also appended to talk_groups.csv when TR_AUTO_ADD_UNKNOWN_TG=1 (default).

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${1:-config.json}"
LOG_FILE="${TR_LOG_FILE:-/tmp/tr-recorder.log}"

if ! command -v trunk-recorder >/dev/null 2>&1; then
  echo "run-trunk-recorder.sh: trunk-recorder not found in PATH" >&2
  exit 1
fi

exec trunk-recorder "$CONFIG" 2>&1 | tee -a "$LOG_FILE" | python3 "$SCRIPT_DIR/tr-encrypted-relay.py"
