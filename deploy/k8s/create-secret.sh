#!/usr/bin/env bash
# Create/update the VTT API secret from the repo .env (API_KEY) or prompts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS="${NS:-sdr-trunk-vtt}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

API_KEY="${API_KEY:-}"
WHISPER_API_KEY="${WHISPER_API_KEY:-}"

if [[ -z "$API_KEY" && -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Only pull the keys we need; .env may contain other exports.
  API_KEY="$(grep -E '^API_KEY=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  WHISPER_API_KEY="$(grep -E '^WHISPER_API_KEY=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true)"
  set +a
fi

if [[ -z "$API_KEY" || "$API_KEY" == "change-me" || "$API_KEY" == change-me-to-a-secret ]]; then
  echo "Set API_KEY in the environment or in $ENV_FILE" >&2
  exit 1
fi

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

kubectl -n "$NS" create secret generic sdr-trunk-vtt-secrets \
  --from-literal=API_KEY="$API_KEY" \
  --from-literal=WHISPER_API_KEY="${WHISPER_API_KEY:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Applied secret sdr-trunk-vtt-secrets in namespace $NS"
