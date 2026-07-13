# Remote Kubernetes deploy (mirrors open-webui on this cluster)

Trunk Recorder + HackRFs stay on the Mac. VTT runs in the cluster with an NFS
volume for audio/CSV/GIS. Call metadata uses Postgres when `DATABASE_URL` is set
(see Postgres cutover below).

## Layout

| File                  | Purpose                                                    |
| --------------------- | ---------------------------------------------------------- |
| `namespace.yaml`      | `sdr-trunk-vtt` namespace                                  |
| `pv-pvc.yaml`         | Static NFS PV/PVC → `192.168.1.162:/volume1/sdr-trunk-vtt` |
| `configmap-env.yaml`  | Non-secret env (Whisper URLs, CORA, compression)           |
| `secret.example.yaml` | Template only — use `create-secret.sh`                   |
| `deployment.yaml`     | `trunk-recorder-vtt` Deployment, PVC at `/data`            |
| `service.yaml`        | LoadBalancer `trunk-recorder-vtt-tcp` on port **8088**     |
| `kustomization.yaml`  | `kubectl apply -k` entrypoint                              |
| `deploy.sh`           | **Recommended** build/load/apply/restart helper            |
| `create-secret.sh`    | Create Secret from repo `.env`                             |
| `seed-nfs.sh`         | Copy onto a locally mounted share (SMB OK; Mac NFS not recommended) |
| `seed-via-node.sh`    | Seed via k3s-node1 NFS mount (preferred from Mac)          |

## Quick deploy (after code changes)

From the **repo root** (not `api/`):

```bash
# Build on Mac, copy to k3s-node1, apply manifests, restart pod
./deploy/k8s/deploy.sh --build --load
```

Or step by step:

```bash
cd api
docker build --platform linux/arm64 -t trunk-recorder-vtt:latest .
docker save trunk-recorder-vtt:latest | gzip > /tmp/trunk-recorder-vtt.tar.gz
scp /tmp/trunk-recorder-vtt.tar.gz superhero@192.168.8.204:/tmp/
ssh superhero@192.168.8.204 'gunzip -c /tmp/trunk-recorder-vtt.tar.gz | sudo docker load'

cd ..   # repo root
kubectl apply -k deploy/k8s
kubectl -n sdr-trunk-vtt rollout restart deploy/trunk-recorder-vtt
kubectl -n sdr-trunk-vtt rollout status deploy/trunk-recorder-vtt
```

### Important

1. **`kubectl apply -k deploy/k8s` must run from repo root** — not from `api/`.
2. **Always `rollout restart` after `docker load`** — the image tag stays `:latest`, so Kubernetes will not pull a new layer until the pod is recreated.
3. **Deployment name is `trunk-recorder-vtt`** (namespace stays `sdr-trunk-vtt`). If you previously created `deploy/sdr-trunk-vtt`, delete it:
   ```bash
   kubectl -n sdr-trunk-vtt delete deploy/sdr-trunk-vtt svc/sdr-trunk-vtt-tcp --ignore-not-found
   ```
4. **LoadBalancer** is `trunk-recorder-vtt-tcp` on **8088** → `http://192.168.8.204:8088`.

## 1. NAS share

Create shared folder:

```text
/volume1/sdr-trunk-vtt
```

Enable NFS (v4.1) for the k3s nodes.

## 2. Get the image onto the cluster

Nodes are **linux/arm64** (Raspberry Pi) with **Docker** runtime — use `docker load`, not `k3s ctr`.

`deployment.yaml` pins `nodeSelector: kubernetes.io/hostname: k3s-node1` and
`imagePullPolicy: IfNotPresent`.

### Option B — push to GHCR

Tag and push `ghcr.io/YOUR_USER/trunk-recorder-vtt:latest`, update `kustomization.yaml`
`newName`/`newTag`, and add `imagePullSecrets` to the Deployment.

## 3. Seed config onto NFS

Use `./deploy/k8s/seed-via-node.sh` (preferred from Mac).

## 4. Apply to the cluster

```bash
kubectl apply -f deploy/k8s/namespace.yaml
./deploy/k8s/create-secret.sh
./deploy/k8s/deploy.sh          # or apply + restart manually (above)
kubectl -n sdr-trunk-vtt get svc trunk-recorder-vtt-tcp
curl -sS http://192.168.8.204:8088/health
```

## 5. Point Trunk Recorder at the remote VTT

On the Mac:

```bash
export VTT_API_URL=http://192.168.8.204:8088
export VTT_API_KEY='(same as API_KEY in .env / Secret)'
```

## Postgres cutover

See main README and `docs/postgres-schema.sql`. Secret: `./deploy/k8s/create-secret.sh`.

## Site branding

Edit `configmap-env.yaml`, then `kubectl apply -k deploy/k8s` and
`kubectl -n sdr-trunk-vtt rollout restart deploy/trunk-recorder-vtt`.
