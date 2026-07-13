#!/bin/bash
# Trunk Recorder uploadScript — local Whisper, then POST completed call to VTT API.
#
# Use when the API runs in the cloud (or with TRANSCRIPTION_WORKER_ENABLED=false)
# and transcription stays on this machine (electricity only).
#
# Trunk Recorder passes:
#   $1  path to .wav
#   $2  path to call .json
#   $3  path to .m4a (only when compressWav is enabled)
#
# Configure in config.json per system:
#   "uploadScript": "./upload-transcribed.sh"
#
# Required tools: curl, jq, ffmpeg, and a local OpenAI-compatible Whisper HTTP API.

set -eo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_env_file="${_script_dir}/../.env"
if [[ -f "$_env_file" ]]; then
  while IFS='=' read -r _key _val; do
    case "$_key" in
      API_KEY|VTT_API_KEY|VTT_API_URL|MIN_CALL_LENGTH|WHISPER_API_URL|WHISPER_API_KEY|WHISPER_MODEL|WHISPER_LANGUAGE|WHISPER_PROMPT|WHISPER_JARGON_PATH|DATA_DIR|AUDIO_BITRATE|TRANSCRIPTION_TIMEOUT|BACKEND_USED)
        _val="${_val%\"}" ; _val="${_val#\"}"
        _val="${_val%\'}" ; _val="${_val#\'}"
        export "${_key}=${_val}"
        ;;
    esac
  done < <(grep -E '^(API_KEY|VTT_API_KEY|VTT_API_URL|MIN_CALL_LENGTH|WHISPER_API_URL|WHISPER_API_KEY|WHISPER_MODEL|WHISPER_LANGUAGE|WHISPER_PROMPT|WHISPER_JARGON_PATH|DATA_DIR|AUDIO_BITRATE|TRANSCRIPTION_TIMEOUT|BACKEND_USED)=' "$_env_file")
fi

wav="$1"
json="$2"

if ! [[ -f "$wav" ]] || ! [[ -f "$json" ]]; then
  echo "upload-transcribed.sh: missing wav or json — ensure audioArchive and callLog are true" >&2
  exit 1
fi

for cmd in curl jq ffmpeg; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "upload-transcribed.sh: required command not found: $cmd" >&2
    exit 1
  fi
done

VTT_API_URL="${VTT_API_URL:-http://127.0.0.1:8080}"
VTT_API_KEY="${VTT_API_KEY:-${API_KEY:-change-me}}"
WHISPER_API_URL="${WHISPER_API_URL:-http://127.0.0.1:9000/v1/audio/transcriptions}"
WHISPER_MODEL="${WHISPER_MODEL:-whisper-1}"
WHISPER_LANGUAGE="${WHISPER_LANGUAGE:-en}"
WHISPER_PROMPT="${WHISPER_PROMPT:-Police fire EMS dispatch scanner radio. 10-4, copy, en route, responding, medic, unit, code.}"
DATA_DIR="${DATA_DIR:-${_script_dir}/../config}"
WHISPER_JARGON_PATH="${WHISPER_JARGON_PATH:-${DATA_DIR}/whisper-jargon.txt}"
_builder="${_script_dir}/build-whisper-prompt.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$_builder" ]]; then
  _resolved="$(python3 "$_builder" 2>/dev/null || true)"
  if [[ -n "$_resolved" ]]; then
    WHISPER_PROMPT="$_resolved"
  fi
fi
AUDIO_BITRATE="${AUDIO_BITRATE:-32k}"
TRANSCRIPTION_TIMEOUT="${TRANSCRIPTION_TIMEOUT:-300}"
BACKEND_USED="${BACKEND_USED:-local-openai-whisper}"

MIN_CALL_LENGTH="${MIN_CALL_LENGTH:-2}"
call_length="$(jq -r '.call_length // 0' "$json" 2>/dev/null || echo 0)"
if awk "BEGIN {exit !($call_length < $MIN_CALL_LENGTH)}"; then
  exit 0
fi

# Copy inputs so background work survives if TR moves/deletes the originals.
_work="$(mktemp -d "${TMPDIR:-/tmp}/vtt-edge.XXXXXX")"
cp "$wav" "${_work}/call.wav"
cp "$json" "${_work}/call.json"
wav="${_work}/call.wav"
json="${_work}/call.json"
mp3="${_work}/call.mp3"
transcript_json="${_work}/transcript.json"
transcript_txt="${_work}/transcript.txt"

do_transcribe_and_upload() {
  set -eo pipefail
  trap 'rm -rf "$_work"' EXIT

  whisper_args=(
    -sS
    --connect-timeout 10
    --max-time "${TRANSCRIPTION_TIMEOUT}"
    --request POST
    --url "${WHISPER_API_URL}"
    --form "file=@${wav};type=audio/wav"
    --form "model=${WHISPER_MODEL}"
    --form "language=${WHISPER_LANGUAGE}"
    --form "response_format=json"
    --form "prompt=${WHISPER_PROMPT}"
    --output "${transcript_json}"
  )
  if [[ -n "${WHISPER_API_KEY:-}" ]]; then
    whisper_args+=(--header "Authorization: Bearer ${WHISPER_API_KEY}")
  fi

  if ! curl "${whisper_args[@]}"; then
    echo "upload-transcribed.sh: Whisper request failed" >&2
    exit 1
  fi

  transcript="$(jq -r '.text // empty' "$transcript_json" 2>/dev/null || true)"
  if [[ -z "$transcript" ]]; then
    echo "upload-transcribed.sh: empty transcript from Whisper" >&2
    exit 1
  fi
  printf '%s' "$transcript" > "$transcript_txt"

  # Match server defaults: mono + configured bitrate
  ffmpeg -y -hide_banner -loglevel error \
    -i "$wav" -vn -ac 1 -codec:a libmp3lame -b:a "$AUDIO_BITRATE" \
    "$mp3"

  curl -sS \
    --connect-timeout 10 \
    --max-time 120 \
    --request POST \
    --url "${VTT_API_URL}/calls" \
    --header "Authorization: Bearer ${VTT_API_KEY}" \
    --form "call_audio=@${mp3};type=audio/mpeg;filename=call.mp3" \
    --form "call_json=@${json}" \
    --form "transcript=<${transcript_txt}" \
    --form "backend_used=${BACKEND_USED}"
}

# Background the whole pipeline when Trunk Recorder invokes us (Whisper can be slow).
if [[ "$(ps -o comm= "$PPID" 2>/dev/null || true)" =~ ^(recorder|trunk-recorder)$ ]]; then
  do_transcribe_and_upload >/dev/null 2>&1 &
  disown
else
  do_transcribe_and_upload
fi
