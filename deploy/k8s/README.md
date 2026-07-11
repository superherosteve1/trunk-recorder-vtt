# Remote Kubernetes deploy (mirrors open-webui on this cluster)

Trunk Recorder + HackRFs stay on the Mac. VTT runs in the cluster with an NFS
volume for SQLite + audio (same Synology pattern as `open-webui`).

## Layout


| File                  | Purpose                                                    |
| --------------------- | ---------------------------------------------------------- |
| `namespace.yaml`      | `sdr-trunk-vtt` namespace                                  |
| `pv-pvc.yaml`         | Static NFS PV/PVC → `192.168.1.162:/volume1/sdr-trunk-vtt` |
| `configmap-env.yaml`  | Non-secret env (Whisper URLs, CORA, compression)           |
| `secret.example.yaml` | Template only — use `create-secret.sh`                     |
| `deployment.yaml`     | Single-replica Deployment, PVC at `/data`                  |
| `service.yaml`        | LoadBalancer on port 8080                                  |
| `kustomization.yaml`  | `kubectl apply -k` entrypoint                              |
| `create-secret.sh`    | Create Secret from repo `.env`                             |
| `seed-nfs.sh`         | Copy onto a locally mounted share (SMB OK; Mac NFS not recommended) |
| `seed-via-node.sh`    | Seed via k3s-node1 NFS mount (preferred from Mac)                  |




## 1. NAS share

On the Synology (or NFS server), create shared folder:

```text
/volume1/sdr-trunk-vtt
```

Enable NFS (v4.1) for the k3s nodes, same as `/volume1/open-webui`.

## 2. Get the image onto the cluster

### Option A — build locally and import on `k3s-node1` (no GHCR)

This cluster’s nodes are **linux/arm64** (Raspberry Pi) and use the **Docker** runtime
(`docker://…`), not containerd — so use `docker load`, not `k3s ctr`.

On the Mac (from `api/`), build for the Pi architecture:

```bash
docker build --platform linux/arm64 -t ghcr.io/billyjenkinsiii/sdr-trunk-vtt:latest .
docker save ghcr.io/billyjenkinsiii/sdr-trunk-vtt:latest | gzip > /tmp/sdr-trunk-vtt.tar.gz
scp /tmp/sdr-trunk-vtt.tar.gz YOU@192.168.8.204:/tmp/
```

On **k3s-node1** (`192.168.8.204` — where open-webui runs):

```bash
gunzip -c /tmp/sdr-trunk-vtt.tar.gz | sudo docker load
sudo docker images | grep sdr-trunk-vtt
```

`deployment.yaml` already pins `nodeSelector: kubernetes.io/hostname: k3s-node1`
and `imagePullPolicy: IfNotPresent`, so that node will use the loaded image.

### Option B — push to GHCR

```bash
cd api
docker build -t ghcr.io/billyjenkinsiii/sdr-trunk-vtt:latest .
echo 'YOUR_GITHUB_PAT' | docker login ghcr.io -u billyjenkinsiii --password-stdin
docker push ghcr.io/billyjenkinsiii/sdr-trunk-vtt:latest
```

Use a **GitHub** PAT (`write:packages`), not a Docker Hub token. If the package is private, create a pull secret (do not put tokens in git):

```bash
kubectl -n sdr-trunk-vtt create secret docker-registry ghcr-cred \
  --docker-server=ghcr.io \
  --docker-username=billyjenkinsiii \
  --docker-password=YOUR_GITHUB_PAT
```

Then add under `deployment.spec.template.spec`:

```yaml
imagePullSecrets:
  - name: ghcr-cred
```
## 3. Seed config onto NFS

**Do not use NFS from the Mac** — macOS ↔ Synology NFS is unreliable
(`RPC struct is bad` / `invalid file system`). k3s nodes mount NFS fine; seed
through a node (or SMB).

### Recommended — seed via k3s-node1

```bash
# default NODE=billbixby@192.168.8.204 — override if needed:
# NODE=you@192.168.8.204 ./deploy/k8s/seed-via-node.sh
./deploy/k8s/seed-via-node.sh
```

That packs talkgroups / GIS / docs on the Mac, SCPs to the Pi, mounts
`192.168.1.162:/volume1/sdr-trunk-vtt` there, and copies files into place.

### Alternative — SMB from the Mac

Finder → Connect to Server → `smb://192.168.1.162/sdr-trunk-vtt`  
(grant your DSM user Read/Write on the share), then:

```bash
NFS_MOUNT=/Volumes/sdr-trunk-vtt ./deploy/k8s/seed-nfs.sh
```

That writes:

```text
/data/talk_groups.csv
/data/districts.json
/data/trunk-recorder.json
/data/gis/*.geojson
/data/docs/*.md
/data/audio/          (created empty; filled by uploads)
```

Re-run the seed script after talkgroup CSV or `districts.json` updates if you want the cluster
catalog/map config refreshed (local auto-add writes the repo file on the Mac, not the NAS).

### District map config on NFS

`districts.json` lists agencies, GeoJSON filenames under `/data/gis`, and talkgroup →
district mappings. The checked-in `config/districts.json` (Denver/Aurora) is the default.
For another municipality, replace or edit that file on the share and place matching
`.geojson` files in `/data/gis` — see the main README “District map (pluggable GIS)”
section (including KML → GeoJSON conversion). Omit agencies/files to hide the map.

## 4. Apply to the cluster

```bash
kubectl apply -f deploy/k8s/namespace.yaml
./deploy/k8s/create-secret.sh
kubectl apply -k deploy/k8s
kubectl -n sdr-trunk-vtt rollout status deploy/sdr-trunk-vtt
kubectl -n sdr-trunk-vtt get svc sdr-trunk-vtt-tcp
```

Note the LoadBalancer EXTERNAL-IP (same MetalLB/klipper pool as open-webui,
`192.168.8.x`). The Service listens on **8088** externally (container still
8080) so it does not collide with open-webui’s host port 8080.

Until EXTERNAL-IP is assigned you can also use the NodePort, e.g.
`http://192.168.8.204:<nodePort>`.

## 5. Point Trunk Recorder at the remote VTT

On the Mac:

```bash
export VTT_API_URL=http://<EXTERNAL-IP>:8088
export VTT_API_KEY='(same as API_KEY secret)'
# upload.sh + tr-encrypted-relay.py already use these
```

Health check:

```bash
curl -sS "http://<EXTERNAL-IP>:8088/health"
```

## Site branding

Edit `configmap-env.yaml` (or patch the ConfigMap) for another municipality:

- `SITE_TITLE` / `SITE_SUBTITLE` / optional `SITE_NOTICE`
- `RECORDS_REQUEST_ENABLED` and `RECORDS_REQUEST_BUTTON_LABEL` (`CORA`, `FOIA`, …)
- `SITE_SHOW_RECORDS_HELP` — talkgroup-ID help link when the records helper is on

Then `kubectl apply -k deploy/k8s` and restart the deployment. Rebuild the image only when application code changes.



## Notes

- **replicas: 1** — SQLite is not multi-writer safe.
- Whisper stays on `192.168.8.59` (pods must reach that LAN IP, as open-webui
already does for Ollama).
- To migrate existing local data, copy `calls.db` and `audio/` into the NFS
share before or after first start (stop the pod while copying the DB).
- Optional: mount the same NFS share on the Mac and set
`TR_TALKGROUPS_CSV=/Volumes/sdr-trunk-vtt/talk_groups.csv` so live auto-add
updates the cluster catalog without re-seeding.

