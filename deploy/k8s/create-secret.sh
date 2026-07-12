#!/usr/bin/env bash
# Create/update the VTT API secret from the repo .env (API_KEY) or prompts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS="${NS:-sdr-trunk-vtt}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

API_KEY="${API_KEY:-}"
WHISPER_API_KEY="${WHISPER_API_KEY:-}"
DATABASE_URL="${DATABASE_URL:-}"

_read_env_key() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Only pull the keys we need; .env may contain other exports.
  if [[ -z "$API_KEY" ]]; then
    API_KEY="$(_read_env_key API_KEY)"
  fi
  if [[ -z "$WHISPER_API_KEY" ]]; then
    WHISPER_API_KEY="$(_read_env_key WHISPER_API_KEY)"
  fi
  if [[ -z "$DATABASE_URL" ]]; then
    DATABASE_URL="$(_read_env_key DATABASE_URL)"
  fi
  set +a
fi

if [[ -z "$API_KEY" || "$API_KEY" == "change-me" || "$API_KEY" == change-me-to-a-secret ]]; then
  echo "Set API_KEY in the environment or in $ENV_FILE" >&2
  exit 1
fi

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

# DATABASE_URL is optional: empty keeps the pod on SQLite at /data/calls.db.
kubectl -n "$NS" create secret generic sdr-trunk-vtt-secrets \
  --from-literal=API_KEY="$API_KEY" \
  --from-literal=WHISPER_API_KEY="${WHISPER_API_KEY:-}" \
  --from-literal=DATABASE_URL="${DATABASE_URL:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Applied secret sdr-trunk-vtt-secrets in namespace $NS"
if [[ -n "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is set (Postgres mode after rollout restart)."
else
  echo "DATABASE_URL empty — API will use SQLite at /data/calls.db."
fi
