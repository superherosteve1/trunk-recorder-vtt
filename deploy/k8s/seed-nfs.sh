#!/usr/bin/env bash
# Seed the NFS share with talkgroups, GIS, docs, and trunk-recorder config.
#
# Prerequisites:
#   - NAS shared folder exists: /volume1/sdr-trunk-vtt
#   - Mounted locally, e.g.:
#       mkdir -p /Volumes/sdr-trunk-vtt
#       mount -t nfs -o vers=4.1 192.168.1.162:/volume1/sdr-trunk-vtt /Volumes/sdr-trunk-vtt
#
# Usage:
#   NFS_MOUNT=/Volumes/sdr-trunk-vtt ./deploy/k8s/seed-nfs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NFS_MOUNT="${NFS_MOUNT:-/Volumes/sdr-trunk-vtt}"

if [[ ! -d "$NFS_MOUNT" ]]; then
  echo "NFS mount not found: $NFS_MOUNT" >&2
  echo "Create/mount the Synology share first (see deploy/k8s/README.md)." >&2
  exit 1
fi

mkdir -p "$NFS_MOUNT/audio" "$NFS_MOUNT/gis" "$NFS_MOUNT/docs"

cp -f "$ROOT/config/talk_groups.csv" "$NFS_MOUNT/talk_groups.csv"
cp -f "$ROOT/config/districts.json" "$NFS_MOUNT/districts.json"
cp -f "$ROOT/config.json" "$NFS_MOUNT/trunk-recorder.json"
cp -f "$ROOT/GIS/denver_police_districts.geojson" "$NFS_MOUNT/gis/"
cp -f "$ROOT/GIS/aurora_police_districts.geojson" "$NFS_MOUNT/gis/"
cp -f "$ROOT/docs/"*.md "$NFS_MOUNT/docs/" 2>/dev/null || true

# App expects GIS under DATA_DIR/gis
echo "Seeded $NFS_MOUNT"
ls -la "$NFS_MOUNT"
ls -la "$NFS_MOUNT/gis" "$NFS_MOUNT/docs"
