#!/bin/bash
# Trunk Recorder uploadScript — posts each completed call to sdr-trunk-vtt.
#
# Trunk Recorder passes:
#   $1  path to .wav
#   $2  path to call .json
#   $3  path to .m4a (only when compressWav is enabled)
#
# Install: copy to your trunk-recorder config directory and chmod +x
# Configure in config.json per system:
#   "audioArchive": true,
#   "callLog": true,
#   "uploadScript": "./upload.sh"

set -eo pipefail

# Load only upload-related vars from repo .env (do not source whole file — WHISPER_PROMPT has spaces)
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_env_file="${_script_dir}/../.env"
if [[ -f "$_env_file" ]]; then
  while IFS='=' read -r _key _val; do
    case "$_key" in
      API_KEY|VTT_API_KEY|VTT_API_URL|MIN_CALL_LENGTH|VTT_LOCAL_TRANSCRIBE)
        # Strip optional surrounding quotes from .env values
        _val="${_val%\"}" ; _val="${_val#\"}"
        _val="${_val%\'}" ; _val="${_val#\'}"
        export "${_key}=${_val}"
        ;;
    esac
  done < <(grep -E '^(API_KEY|VTT_API_KEY|VTT_API_URL|MIN_CALL_LENGTH|VTT_LOCAL_TRANSCRIBE)=' "$_env_file")
fi

# Edge mode: transcribe locally, then POST completed package to the API
if [[ "${VTT_LOCAL_TRANSCRIBE:-0}" == "1" ]]; then
  exec "${_script_dir}/upload-transcribed.sh" "$@"
fi

wav="$1"
json="$2"

if ! [[ -f "$wav" ]] || ! [[ -f "$json" ]]; then
  echo "upload.sh: missing wav or json — ensure audioArchive and callLog are true" >&2
  exit 1
fi

# Point at your sdr-trunk-vtt instance
VTT_API_URL="${VTT_API_URL:-http://127.0.0.1:8080}"
VTT_API_KEY="${VTT_API_KEY:-${API_KEY:-change-me}}"

# Optional: skip short calls before uploading (server also enforces MIN_CALL_LENGTH)
MIN_CALL_LENGTH="${MIN_CALL_LENGTH:-2}"
call_length="$(jq -r '.call_length // 0' "$json" 2>/dev/null || echo 0)"
if awk "BEGIN {exit !($call_length < $MIN_CALL_LENGTH)}"; then
  exit 0
fi

# Optional talkgroup filter — uncomment and edit to limit transcription
# TALKGROUP="$(jq -r '.talkgroup' "$json")"
# case "$TALKGROUP" in
#   101|102|911) ;;
#   *) exit 0 ;;
# esac

curl_args=(
  -sS
  --connect-timeout 5
  --max-time 30
  --request POST
  --url "${VTT_API_URL}/calls"
  --header "Authorization: Bearer ${VTT_API_KEY}"
  --header "Content-Type: multipart/form-data"
  --form "call_audio=@${wav}"
  --form "call_json=@${json}"
)

# Run in background when invoked by trunk-recorder so recording is not blocked
if [[ "$(ps -o comm= "$PPID" 2>/dev/null || true)" =~ ^(recorder|trunk-recorder)$ ]]; then
  curl "${curl_args[@]}" >/dev/null 2>&1 &
  disown
else
  curl "${curl_args[@]}"
fi
