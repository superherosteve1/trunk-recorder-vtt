#!/usr/bin/env bash
# Seed the share without mounting NFS on the Mac.
# Packs config on the Mac, then mounts NFS on k3s-node1 and copies files there.
#
# Usage:
#   ./deploy/k8s/seed-via-node.sh
#   NODE=user@192.168.8.204 NFS_EXPORT=192.168.1.162:/volume1/sdr-trunk-vtt ./deploy/k8s/seed-via-node.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NODE="${NODE:-user@192.168.8.204}"
NFS_EXPORT="${NFS_EXPORT:-192.168.1.162:/volume1/sdr-trunk-vtt}"
REMOTE_MNT="${REMOTE_MNT:-/mnt/sdr-trunk-vtt}"
STAGING="/tmp/vtt-seed-$$"

# Avoid AppleDouble (._*) and xattr noise when extracting on Linux.
export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1

mkdir -p "$STAGING/gis" "$STAGING/docs"
cp -f "$ROOT/config/talk_groups.csv" "$STAGING/talk_groups.csv"
cp -f "$ROOT/config/districts.json" "$STAGING/districts.json"
if [[ -f "$ROOT/config/whisper-jargon.txt" ]]; then
  cp -f "$ROOT/config/whisper-jargon.txt" "$STAGING/whisper-jargon.txt"
else
  cp -f "$ROOT/config/whisper-jargon.example.txt" "$STAGING/whisper-jargon.txt"
fi
cp -f "$ROOT/config.json" "$STAGING/trunk-recorder.json"
cp -f "$ROOT/GIS/denver_police_districts.geojson" "$STAGING/gis/"
cp -f "$ROOT/GIS/aurora_police_districts.geojson" "$STAGING/gis/"
cp -f "$ROOT/docs/"*.md "$STAGING/docs/" 2>/dev/null || true

TAR="/tmp/vtt-seed.tgz"
tar --format=ustar -C "$STAGING" -czf "$TAR" .
rm -rf "$STAGING"

echo "Copying seed archive to $NODE …"
scp "$TAR" "$NODE:/tmp/vtt-seed.tgz"

echo "Seeding NFS export on node ($NFS_EXPORT) …"
ssh -t "$NODE" bash -s <<EOF
set -euo pipefail
sudo mkdir -p "$REMOTE_MNT"
if ! mountpoint -q "$REMOTE_MNT"; then
  sudo mount -t nfs -o vers=4.1 "$NFS_EXPORT" "$REMOTE_MNT"
fi
sudo mkdir -p "$REMOTE_MNT/audio" "$REMOTE_MNT/gis" "$REMOTE_MNT/docs"
sudo tar -C "$REMOTE_MNT" -xzf /tmp/vtt-seed.tgz
# Drop any leftover AppleDouble junk from older seeds
sudo find "$REMOTE_MNT" -name '._*' -delete 2>/dev/null || true
sudo chmod -R a+rX "$REMOTE_MNT/talk_groups.csv" "$REMOTE_MNT/whisper-jargon.txt" "$REMOTE_MNT/districts.json" "$REMOTE_MNT/trunk-recorder.json" "$REMOTE_MNT/gis" "$REMOTE_MNT/docs" || true
echo "Seeded:"
ls -la "$REMOTE_MNT"
ls -la "$REMOTE_MNT/gis" "$REMOTE_MNT/docs"
EOF

rm -f "$TAR"
echo "Done. Mac NFS is not required."
