#!/usr/bin/env bash
# Build (optional), load image on k3s-node1 (optional), apply manifests, restart pod.
#
# Typical Mac workflow after code changes:
#   ./deploy/k8s/deploy.sh --build --load
#
# Apply + restart only (image already on node):
#   ./deploy/k8s/deploy.sh
#
# Env overrides:
#   NODE=user@192.168.8.204
#   NS=sdr-trunk-vtt
#   DEPLOY=trunk-recorder-vtt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS="${NS:-sdr-trunk-vtt}"
DEPLOY="${DEPLOY:-trunk-recorder-vtt}"
NODE="${NODE:-user@192.168.8.204}"
IMAGE="${IMAGE:-trunk-recorder-vtt:latest}"
ARCH="${ARCH:-linux/arm64}"
TAR="/tmp/trunk-recorder-vtt.tar.gz"

do_build=false
do_load=false
do_apply=true
do_restart=true

usage() {
  sed -n '2,12p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) do_build=true ;;
    --load) do_load=true ;;
    --no-apply) do_apply=false ;;
    --no-restart) do_restart=false ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
  shift
done

if $do_build; then
  echo "Building $IMAGE ($ARCH) from api/ …"
  docker build --platform "$ARCH" -t "$IMAGE" "$ROOT/api"
fi

if $do_load; then
  echo "Saving and copying image to $NODE …"
  docker save "$IMAGE" | gzip > "$TAR"
  scp "$TAR" "$NODE:/tmp/trunk-recorder-vtt.tar.gz"
  echo "Loading on node (docker load) …"
  ssh "$NODE" 'gunzip -c /tmp/trunk-recorder-vtt.tar.gz | sudo docker load'
  ssh "$NODE" "sudo docker images | grep trunk-recorder-vtt || true"
fi

if $do_apply; then
  echo "Applying kustomize from $ROOT/deploy/k8s …"
  kubectl apply -f "$ROOT/deploy/k8s/namespace.yaml"
  if [[ -x "$ROOT/deploy/k8s/create-secret.sh" ]]; then
    "$ROOT/deploy/k8s/create-secret.sh" || true
  fi
  kubectl apply -k "$ROOT/deploy/k8s"
fi

# Remove legacy duplicate from the old deployment name (safe if absent).
kubectl -n "$NS" delete deploy/sdr-trunk-vtt svc/sdr-trunk-vtt-tcp --ignore-not-found

if $do_restart; then
  echo "Restarting deploy/$DEPLOY (required after docker load — tag stays :latest) …"
  kubectl -n "$NS" rollout restart "deploy/$DEPLOY"
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=180s
fi

echo ""
kubectl -n "$NS" get pods -l "app=$DEPLOY" -o wide
kubectl -n "$NS" get svc trunk-recorder-vtt-tcp
echo ""
echo "Health: curl -sS http://192.168.8.204:8088/health"
