# trunk-recorder-vtt

Dockerized transcription pipeline for [Trunk Recorder](https://github.com/TrunkRecorder/trunk-recorder). Each recorded call (WAV + JSON metadata) is ingested via an `uploadScript`, queued, and transcribed using a Whisper or faster-whisper endpoint on your network.

## Architecture

```
HackRF / SDR
     │
     ▼
Trunk Recorder ──uploadScript──▶ sdr-trunk-vtt (Docker)
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
           Whisper endpoint                  faster-whisper endpoint
        (OpenAI-compatible)                  (HTTP multipart API)
                    │                                   │
                    └─────────────────┬─────────────────┘
                                      ▼
                              SQLite + web dashboard
                                      │
                         (optional) WAV → MP3 after transcription
```

## Quick start

### 1. Configure endpoints

```bash
cp .env.example .env
```

Edit `.env` with your network endpoints:

```env
API_KEY=your-secret-key
TRANSCRIPTION_BACKEND=openai          # or faster_whisper
TRANSCRIPTION_FALLBACK=true           # try the other backend on failure

WHISPER_API_URL=http://192.168.1.50:9000/v1/audio/transcriptions
FASTER_WHISPER_API_URL=http://192.168.1.50:8000/transcribe
```

If your Whisper services run on the same machine as Docker, `host.docker.internal` is preconfigured in `docker-compose.yml` as a fallback default.

### 2. Start the service

```bash
docker compose up -d --build
```

- Dashboard: http://localhost:8080
- API docs: http://localhost:8080/docs
- Health: http://localhost:8080/health

### 3. Wire up Trunk Recorder

Use [examples/trunk-recorder-system.json](examples/trunk-recorder-system.json) as your Trunk Recorder `config.json`. It is derived from `sdr-trunk-import.json` (Denver-Aurora multisite, dual HackRF) with VTT upload settings added.

Recommended directory layout (paths in the config are relative to this folder):

```
~/trunk-recorder/
├── config.json              # copy from examples/trunk-recorder-system.json
├── upload.sh                # copy from scripts/upload.sh
├── config/
│   └── talk_groups.csv      # your talkgroup list from the other system
└── recordings/              # captureDir (was /app/media if TR ran in Docker)
```

```bash
mkdir -p ~/trunk-recorder/config ~/trunk-recorder/recordings
cp examples/trunk-recorder-system.json ~/trunk-recorder/config.json
cp scripts/upload.sh ~/trunk-recorder/
cp /path/from/old/system/config/talk_groups.csv ~/trunk-recorder/config/
chmod +x ~/trunk-recorder/upload.sh
```

If Trunk Recorder still runs in Docker on the new host, set `captureDir` back to `/app/media` and mount that volume in your TR container.

Set environment variables for the upload script (shell profile, systemd unit, or wrapper):

```bash
export VTT_API_URL=http://127.0.0.1:8080   # host running sdr-trunk-vtt
export VTT_API_KEY=your-secret-key
```

Each system block already includes `audioArchive`, `callLog`, and `uploadScript: "./upload.sh"`.

### Encrypted channel activity

**Important:** This stack never records or decrypts encrypted voice. Encrypted dashboard rows are metadata only (Trunk Recorder skipped the call). Recording or decrypting encrypted radio traffic you are not authorized to receive is generally a federal felony — see [docs/faq-encrypted-activity.md](docs/faq-encrypted-activity.md) for how the pipeline stays metadata-only.

Trunk Recorder **does not** run `uploadScript` when it skips a call — it only logs lines like `Not Recording: ENCRYPTED - src: …` or `Not Recording: TG not in Talkgroup File`. To show that activity on the dashboard (no audio, no transcription), pipe TR output through the activity relay:

```bash
chmod +x scripts/run-trunk-recorder.sh scripts/tr-encrypted-relay.py
./scripts/run-trunk-recorder.sh config.json
```

Or manually:

```bash
trunk-recorder config.json 2>&1 | ./scripts/tr-encrypted-relay.py
```

Encrypted hits appear in the call table with status **encrypted** and a lock icon. Unknown talk groups appear as **unknown_talkgroup** with a clipboard icon — use these to find TGs to add to `talk_groups.csv`. Both are included in the activity chart and talk group filters. Set `VTT_API_URL` and `VTT_API_KEY` (or `API_KEY`) the same as for `upload.sh`. Optional: `TR_LOCAL_TIMEZONE=America/Denver` (default) for log timestamps.

### Auto-add unknown talkgroups

With `recordUnknown: false`, Trunk Recorder will not record a talkgroup until it appears in `talk_groups.csv`. The activity relay can append placeholder rows when it sees `TG not in Talkgroup File`:

```csv
{id},Unknown {id},D,Unknown {id},Interop,Unknown,
```

| Variable | Default | Description |
|----------|---------|-------------|
| `TR_AUTO_ADD_UNKNOWN_TG` | `1` | Append Unknown placeholders on discovery (`0` to disable) |
| `TR_TALKGROUPS_CSV` | *(from config)* | Override path to `talk_groups.csv` |
| `TR_CONFIG_JSON` | `config.json` | Used to resolve `talkgroupsFile` when CSV path is not set |

Mode **D** means clear voice may be recorded after you **restart Trunk Recorder** (CSV is loaded at startup). Encrypted calls are still skipped — placeholders do not enable decryption. Replace `Unknown …` labels later via RadioReference / CORA when you learn the real name.

Backfill gaps already logged in the dashboard (API or local SQLite):

```bash
./scripts/sync-unknown-talkgroups.py --dry-run
./scripts/sync-unknown-talkgroups.py --min-hits 3
./scripts/sync-unknown-talkgroups.py --sqlite /path/to/calls.db
```

Then restart Trunk Recorder so it reloads the CSV.

## Dashboard features

- Live call table with talkgroup / system filters, quick-filter chips, and date/time + transcript search
- Queue stats, talkgroup activity chart, per-system outcome mix, encrypted-tempo anomaly badge
- Police district choropleth (GeoJSON under `GIS/` + `config/districts.json`)
- Configurable site title / notice and CORA/FOIA clipboard on encrypted rows (metadata only — no decryption)
- Non-encrypted calls are recorded, transcribed, then optionally recompressed for storage

### Site branding

Defaults match Denver/Aurora. For another municipality, set env (or the k8s ConfigMap) without code changes:

```env
SITE_TITLE=Example County Trunk Monitor
SITE_SUBTITLE=Trunk Recorder transcription dashboard
RECORDS_REQUEST_ENABLED=true
RECORDS_REQUEST_BUTTON_LABEL=FOIA
RECORDS_REQUEST_TITLE=FOIA audio retrieval request
RECORDS_REQUEST_CONTACT_LABEL=FOIA
SITE_SHOW_RECORDS_HELP=false
```

Set `RECORDS_REQUEST_ENABLED=false` to hide the per-row clipboard button and the records help-nav link.

### District map (pluggable GIS)

The choropleth is driven by:

1. One or more **GeoJSON** files under `GIS/` (mounted at `/data/gis`)
2. **`config/districts.json`** (mounted at `/data/districts.json`) listing agencies, filenames, and talkgroup → district mappings
3. Your live **`config/talk_groups.csv`** (not committed — copy from [`config/talk_groups.example.csv`](config/talk_groups.example.csv), which lists only the Denver/Aurora district talkgroups mapped in `districts.json`)

```bash
cp config/talk_groups.example.csv config/talk_groups.csv
# then expand with your full RadioReference / local catalog as needed
```

Default config ships Denver + Aurora. To add another municipality:

1. Put polygons in `GIS/your_city_districts.geojson` (FeatureCollection of Polygon/MultiPolygon).
2. Each feature needs a numeric district id in one of the property names listed in `district_id_properties` (default: `DIST_NUM`, `POLICE_DISTRICT`, `DISTRICT`, `district`). Feature ids become `{agency}-{num}` (e.g. `boulder-2`).
3. Edit `config/districts.json` — add an agency and district rows:

```json
{
  "district_id_properties": ["DIST_NUM", "DISTRICT", "district"],
  "agencies": [
    {
      "id": "boulder",
      "label": "Boulder",
      "geojson": "boulder_police_districts.geojson",
      "catalog_keywords": ["boulder", "bpd"]
    }
  ],
  "districts": [
    {
      "id": "boulder-1",
      "agency": "boulder",
      "district": 1,
      "label": "BPD Dist 1",
      "talkgroups": [12345],
      "primary_talkgroup": 12345
    }
  ]
}
```

Omit agencies (or leave GeoJSON files out) to disable the map — the panel shows an empty state.

**KML / KMZ:** runtime does not parse KML. Convert first, then point `geojson` at the result:

```bash
# GDAL / ogr2ogr
ogr2ogr -f GeoJSON GIS/city_districts.geojson city_districts.kml

# or mapshaper (npm)
npx mapshaper city_districts.kml -o format=geojson GIS/city_districts.geojson
```

If the KML uses folders/layers, flatten to one FeatureCollection and ensure district numbers land in a property named in `district_id_properties`.

## Audio storage compression

After a call is transcribed, the worker can convert the archived WAV to a smaller format (default **MP3 @ 32 kbps mono**) and delete the WAV. Existing completed WAVs are backfilled gradually when the worker is idle.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_COMPRESS` | `true` | Recompress after successful transcription |
| `AUDIO_FORMAT` | `mp3` | `mp3`, `ogg`, or `opus` |
| `AUDIO_BITRATE` | `32k` | Encoder bitrate |

Requires `ffmpeg` in the API container (installed by the Dockerfile).

## Prune Trunk Recorder temp / capture dirs

`upload.sh` copies each call into sdr-trunk-vtt, but Trunk Recorder still keeps originals under `tempDir` / `captureDir` (for example `/tmp/tr/t` and `/tmp/tr/r`). Prune aged files so those directories do not grow without bound:

```bash
chmod +x scripts/prune-tr-temp.sh
./scripts/prune-tr-temp.sh --dry-run          # list only
TR_MAX_AGE_HOURS=24 ./scripts/prune-tr-temp.sh
```

Cron example (hourly, keep 24 hours):

```bash
0 * * * * cd /path/to/sdr-trunk-vtt && TR_MAX_AGE_HOURS=24 ./scripts/prune-tr-temp.sh >>/tmp/tr-prune.log 2>&1
```

Paths are read from `config.json` (`tempDir`, `captureDir`). Override with `TR_CONFIG=/path/to/config.json`.

## Endpoint compatibility

### OpenAI-compatible Whisper (`TRANSCRIPTION_BACKEND=openai`)

Works with any server exposing `POST /v1/audio/transcriptions` with multipart `file` upload and JSON `{"text": "..."}` response (OpenAI API, whisper.cpp servers, many local proxies).

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_API_URL` | — | Full URL to transcriptions endpoint |
| `WHISPER_API_KEY` | (empty) | Optional Bearer token |
| `WHISPER_MODEL` | `whisper-1` | Model name sent to the API |
| `WHISPER_LANGUAGE` | `en` | Language hint |
| `WHISPER_PROMPT` | dispatch terms | Improves scanner jargon accuracy |

### faster-whisper HTTP (`TRANSCRIPTION_BACKEND=faster_whisper`)

Expects `POST` with multipart field `audio` and optional `language` form field. Supports common response shapes:

- `{"text": "..."}`
- `{"transcription": "..."}`
- `{"segments": [{"text": "..."}]}`

| Variable | Default | Description |
|----------|---------|-------------|
| `FASTER_WHISPER_API_URL` | — | Full URL to transcribe endpoint |
| `FASTER_WHISPER_LANGUAGE` | `en` | Language hint |

If your faster-whisper server uses a different field name or path, set `FASTER_WHISPER_API_URL` to match (e.g. `http://host:8080/v1/audio/transcriptions` and use `TRANSCRIPTION_BACKEND=openai` if it is OpenAI-compatible).

## API

### `POST /calls`

Ingest a call for transcription. Used by the upload script.

```bash
curl -X POST http://localhost:8080/calls \
  -H "Authorization: Bearer your-secret-key" \
  -F "call_audio=@call.wav" \
  -F "call_json=@call.json"
```

### `GET /calls`

List calls with transcripts and status. Optional filters: `talkgroup`, `system`, `from`, `to`, `q` (transcript substring), `status`, `limit`, `offset`.

### `GET /calls/{id}`

Get a single call record.

### `GET /calls/{id}/audio`

Stream archived audio (WAV or compressed format).

### `GET /health`

Service health and queue counts.

### `GET /stats/activity`

Top talkgroups or hourly timeline for a selected talkgroup.

### `GET /stats/system-outcomes`

Per-system encrypted / transcribed / failed mix.

### `GET /stats/encrypted-anomalies`

Heuristic encrypted-tempo anomalies vs weekday/hour baseline.

### `GET /stats/districts`

Police-district activity via talkgroup → district mapping. Includes `agencies` (id, label, geojson_url, available) and `district_id_properties` for the map UI.

### `GET /gis/{agency_id}.geojson`

District polygons for a configured agency (from `districts.json` + mounted `GIS/`). Legacy URLs `/gis/{denver|aurora}-police-districts.geojson` still work.

### `POST /events/encrypted`

Log encrypted-channel activity (metadata only — no WAV). Used by `scripts/tr-encrypted-relay.py`.

```bash
curl -X POST http://localhost:8080/events/encrypted \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"system_name":"Denver","talkgroup":35058,"freq":858.7375,"src":823692}'
```

### `POST /events/unknown-talkgroup`

Log activity for talk groups missing from `talk_groups.csv` (metadata only — no WAV).

```bash
curl -X POST http://localhost:8080/events/unknown-talkgroup \
  -H "Authorization: Bearer your-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"system_name":"Aurora","talkgroup":39707,"freq":859.9875}'
```

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `VTT_PORT` | `8080` | Host port |
| `API_KEY` | `change-me` | Bearer token for `/calls` (disabled when left as default) |
| `TRANSCRIPTION_BACKEND` | `openai` | Primary backend: `openai` or `faster_whisper` |
| `TRANSCRIPTION_FALLBACK` | `true` | Fall back to the other backend on error |
| `MIN_CALL_LENGTH` | `2` | Skip calls shorter than N seconds |
| `TRANSCRIPTION_TIMEOUT` | `300` | Per-request timeout in seconds |
| `MAX_RETRIES` | `3` | Retry failed transcriptions |
| `AUDIO_COMPRESS` | `true` | Recompress WAV after transcription |
| `AUDIO_FORMAT` | `mp3` | Storage format: `mp3`, `ogg`, or `opus` |
| `AUDIO_BITRATE` | `32k` | Compressed audio bitrate |
| `SITE_TITLE` | `Denver / Aurora Trunk Monitor` | Dashboard `<h1>` / browser title |
| `SITE_SUBTITLE` | `Trunk Recorder transcription dashboard` | Line under the title |
| `SITE_NOTICE` | *(built-in legal/ops notice)* | Header notice (FAQ link always appended) |
| `SITE_SHOW_RECORDS_HELP` | `true` | Help-nav link to talkgroup ID draft when records helper is on |
| `RECORDS_REQUEST_ENABLED` | `true` | Show public-records clipboard helper on encrypted rows |
| `RECORDS_REQUEST_BUTTON_LABEL` | `CORA` | Button label (e.g. `FOIA`) |
| `RECORDS_REQUEST_TITLE` | CORA audio retrieval request | Clipboard title line |
| `RECORDS_REQUEST_CONTACT_LABEL` | `CORA` | Contact label in clipboard text |

Data is persisted in the Docker volume `vtt-data` (SQLite database and archived audio/JSON files). Talkgroups CSV, `districts.json`, GIS GeoJSON, and `docs/` are bind-mounted from the repo. Project help pages are at `/help/{name}` (for example [`/help/faq-encrypted-activity`](http://localhost:8080/help/faq-encrypted-activity) and [`/help/cora-talkgroup-identification`](http://localhost:8080/help/cora-talkgroup-identification); `/faq/encrypted` is an alias for the encrypted FAQ).

## Upload script filtering

Edit `scripts/upload.sh` to filter by talkgroup before upload:

```bash
TALKGROUP="$(jq -r '.talkgroup' "$json")"
case "$TALKGROUP" in
  101|102|911) ;;
  *) exit 0 ;;
esac
```

## Development

Run locally without Docker:

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=../data
export WHISPER_API_URL=http://localhost:9000/v1/audio/transcriptions
# Optional local compression (requires ffmpeg on PATH)
export AUDIO_COMPRESS=true
uvicorn app.main:app --reload --port 8080
```

## License

GPL-3.0 (compatible with Trunk Recorder)

## Remote Kubernetes

Manifests that mirror this cluster’s `open-webui` pattern (NFS PV + LoadBalancer)
live under [deploy/k8s/](deploy/k8s/README.md). Trunk Recorder stays on the Mac;
only the VTT API/dashboard runs remotely.
