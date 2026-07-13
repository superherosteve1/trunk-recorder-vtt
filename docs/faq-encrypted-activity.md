# FAQ: Encrypted activity without recording encrypted audio

This FAQ explains how Denver/Aurora Trunk Monitor can show that encrypted talkgroups are busy **without recording, storing, or decrypting encrypted voice**.

**This is not legal advice.** Federal and state wiretap / electronic-communications laws are serious. If you operate a scanner or logger, know the rules that apply to you. Intentionally intercepting and decrypting encrypted radio traffic you are not authorized to receive is generally treated as a **federal felony** under the Wiretap Act / Electronic Communications Privacy Act (see especially [18 U.S.C. § 2511](https://www.law.cornell.edu/uscode/text/18/2511) and the definition of communications that are not “readily accessible to the general public” in [18 U.S.C. § 2510(16)](https://www.law.cornell.edu/uscode/text/18/2510), which excludes scrambled or encrypted signals).

---

## Short answer

| What happens | Encrypted call | Clear (unencrypted) call |
|---|---|---|
| Trunk Recorder records WAV? | **No** — it skips with `Not Recording: ENCRYPTED` | Yes (if the talkgroup is configured to record) |
| `uploadScript` runs? | **No** | Yes → audio + JSON go to VTT |
| Dashboard row? | Yes — status **encrypted**, lock icon | Yes — queued / transcribed / playable |
| Audio on disk in VTT? | **Never** | Yes (then optionally compressed to MP3) |
| Decryption / keys? | **Never** | N/A |

Encrypted rows are **metadata only**: time, system, talkgroup, frequency, and (when logged) source radio ID. There is no ciphertext file and no clear audio to play.

---

## Why can we see encrypted activity at all?

On a P25 trunked system, the **control channel** advertises grants and channel activity in the clear so radios know when to join a talkgroup. That signaling is what Trunk Recorder (and any trunking scanner) uses to follow the system.

When a grant is for an **encrypted** voice channel, Trunk Recorder’s normal behavior is:

1. Detect that the call is encrypted.
2. **Refuse to record** voice audio.
3. Print a log line such as:

   `Not Recording: ENCRYPTED - src: 850811`

It does **not** run `uploadScript` for that skip. So the VTT upload path never receives a WAV for encrypted traffic.

This project’s activity relay (`scripts/tr-encrypted-relay.py`) only **reads that skip log line** and POSTs a small JSON event to `POST /events/encrypted`. Nothing in that path opens a voice channel file, stores encrypted bits, or attempts decryption.

```
Control channel (public signaling)
        │
        ▼
Trunk Recorder decides: encrypted → do not record
        │
        ├── log: "Not Recording: ENCRYPTED …"
        │         │
        │         ▼
        │   tr-encrypted-relay.py  →  POST /events/encrypted
        │         │
        │         ▼
        │   SQLite row (metadata) → dashboard lock icon
        │
        └── no WAV, no uploadScript, no Whisper
```

---

## What metadata is stored for encrypted hits?

Typical fields (from the TR log / API payload):

- **When** the skip was observed (timestamp)
- **System / site** name (e.g. Denver, Aurora)
- **Talkgroup** ID (and label from `talk_groups.csv` when known)
- **Frequency** of the grant (MHz)
- **Source RID** when TR includes `src:` in the log line
- Internal VTT **record ID** (for our database only — not an agency CAD/logger ID)

What is **not** stored:

- Encrypted voice samples or bitstream dumps
- Decryption keys, keystreams, or key IDs used to recover audio
- Transcripts of encrypted content (there is nothing to transcribe)

Dashboard players are disabled for `encrypted` (and `unknown_talkgroup`) rows. The UI shows a lock and optional CORA clipboard helper — not an audio control.

---

## Is “logging that encryption happened” the same as recording encrypted communications?

No. This system records the fact that Trunk Recorder **declined** to capture voice because the call was marked encrypted. That is operational metadata derived from TR’s own skip message, not a recording of the protected voice payload.

Recording or decrypting the encrypted voice content itself is exactly what this pipeline is built to **avoid**.

---

## How do clear (unencrypted) calls differ?

For talkgroups configured to record and not marked encrypted:

1. Trunk Recorder writes WAV + call JSON under `captureDir`.
2. `uploadScript` (`scripts/upload.sh`) posts them to VTT.
3. The worker queues transcription (Whisper / faster-whisper).
4. After success, audio may be recompressed (e.g. MP3) for storage.

Only that clear path produces playable audio and transcripts on the dashboard.

---

## What about the CORA / records-request button?

Encrypted rows can offer a clipboard template for a **public-records request** to the responsible agency. That text identifies the call using the same publicly observable metadata (time, system, talkgroup/RIDs, frequency) so an agency custodian can locate **their** logger export.

It explicitly does **not** ask for decryption keys. Keys stay under agency control. Any clear audio of encrypted traffic must come from a lawful agency process — this scanner pipeline is not capable of decryption and can only provide unencrypted control channel metadata.

For a **news incident spanning many encrypted grants**, use the dashboard **CORA dossier** (or `GET /stats/incident-dossier`): set From/To for the incident window and copy one locator packet listing the busiest talkgroups and source radio IDs. That helps target a CORA/FOIA request without listening to encrypted voice.

See also [CORA draft: identify unknown talkgroups](/help/cora-talkgroup-identification) for talkgroup-label requests (identity of TGs, not keys or encrypted audio).

---

## What about “unknown talkgroup” rows?

Those are also **not recordings**. Trunk Recorder skipped because the TG was missing from `talk_groups.csv` (`Not Recording: TG not in Talkgroup File`). The relay posts metadata to `POST /events/unknown-talkgroup` so you can decide whether to add the TG for future **clear** recording. Same rule applies: if that TG later carries encrypted voice, TR still will not record it as encrypted audio through this stack.

---

## How do charts and “encrypted tempo” work?

Activity charts, system-outcome bars, district heat (where mapped), and encrypted-tempo anomalies all count **database events** — including encrypted metadata rows. They answer questions like “how often did encrypted grants appear on this talkgroup?” They do **not** imply that encrypted voice was captured or understood.

### Encrypted tempo (dashboard badge) — full calculation

**What it measures:** grant *tempo* on encrypted talkgroups — how many `status = encrypted` metadata rows arrived in a short window compared with recent history. It is **not** incident detection from voice content; it flags *possible* busy periods worth opening a CORA dossier.

**Default window:** last **15 minutes** (configurable via `GET /stats/encrypted-anomalies?window_minutes=…`).

**Step 1 — recent activity:** for each `(talkgroup, system_name)` pair, count encrypted grants in the window and distinct source RIDs (`src`). Ignore pairs with fewer than **5** grants in the window (`min_recent`).

**Step 2 — baseline (prior 14 days, excluding the current window):**

| Priority | Source | When used |
|---|---|---|
| 1 | **Weekday/hour** | Same talkgroup, same weekday and hour (UTC), averaged over matching days; needs **≥2 sample days** |
| 2 | **Overall** | All encrypted grants on that talkgroup in the baseline period, scaled to the window size; needs **≥20** historical grants |
| 3 | **Sparse** | Some history but not enough for a reliable rate |
| 4 | **None** | New or rarely seen talkgroup |

**Step 3 — signals (a talkgroup needs at least one; without a rate baseline it needs two):**

1. **Rate spike** — recent count ≥ **3×** the expected count (weekday/hour or overall baseline) and ≥5 grants.
2. **Related TGs elevated** — another talkgroup in the same **family** is also hot. Family = CSV `Category` when set (e.g. `Denver Police`, `Police`), else talkgroup band (e.g. `35000`, `39500`). Denver and Aurora police are **different families** (`Denver Police` vs `Police`).
3. **Many RIDs** — **≥4** distinct source radio IDs keyed up in the window (≥6 during cold-start).
4. **Rare TG surge** — little baseline history but a sudden burst (higher floors during cold-start).

**Step 4 — confidence:** combined score from the signals above → `low` / `medium` / `high`. Results are sorted by score; the API returns up to 8 and reserves slots so **each Trunk Recorder system** (e.g. Denver, Aurora) can appear once when it has a qualifying hit.

**Cold-start mode:** while total encrypted history in the database is **<500** rows, scoring is stricter (e.g. co-activation needs more related talkgroups; higher RID floors). The badge shows **learning baseline** until that threshold is passed.

**Why Aurora may rarely appear**

- **Volume:** Denver encrypted grants usually outnumber Aurora; higher-scoring Denver talkgroups fill most slots.
- **System name:** each row uses Trunk Recorder’s `[System]` from the skip log (e.g. `[Denver]`, `[Aurora]`). Aurora must be monitored and relayed (`tr-encrypted-relay.py`) for those rows to exist.
- **Thresholds:** Aurora district dispatch must hit ≥5 encrypted grants in 15 minutes *and* pass spike / co-activation / RID rules. Quiet periods show **quiet**, not an error.
- **UI:** the badge lists **system · talkgroup tag · rate** (e.g. `Aurora · APD District 1 · 3.2×`) and a footnote with encrypted grant counts per system in the window.

Use **Search → CORA dossier** (or `GET /stats/incident-dossier?system=Aurora&from=…&to=…`) to inspect Aurora encrypted activity even when nothing is flagged as an anomaly.

---

## Does the API also reject encrypted audio on upload?

Yes — as a second line of defense. `POST /calls` will return **HTTP 400** (and not store the call) when:

1. **Metadata** says the grant was encrypted (`encrypted` / `enc` / non-clear P25 `algid`, etc.), or
2. **WAV PCM entropy** looks like noise/ciphertext (optional; default on).

Honest Trunk Recorder clients never hit this path for encrypted grants — they skip recording and never run `uploadScript`. The gate exists for misconfigured or malicious feeders. Catalog Mode values like `DE` / `TE` are **not** used alone (they mean encrypt-*capable*, not that this call was encrypted).

Encrypted **activity** still belongs on `POST /events/encrypted` (metadata only). Tune via `REJECT_ENCRYPTED_UPLOADS`, `REJECT_ENCRYPTED_AUDIO_ENTROPY`, and `ENCRYPTED_AUDIO_ENTROPY_THRESHOLD`.

---

## Operator checklist (keep the system lawful by design)

1. Run Trunk Recorder so encrypted calls are **skipped**, not force-recorded.
2. Use `./scripts/run-trunk-recorder.sh` (or pipe TR through `tr-encrypted-relay.py`) if you want encrypted **activity** on the dashboard.
3. Never point custom tools at encrypted voice channels to dump or crack audio.
4. Treat CORA/clipboard helpers as requests for **agency-held** records, not as a decryption workflow.
5. Keep `talk_groups.csv` and record/skip policy intentional: only configure recording for traffic you are allowed to monitor in the clear.
6. Leave `REJECT_ENCRYPTED_UPLOADS=true` so `POST /calls` cannot archive encrypted-looking WAVs.

---

## Related docs

- [CORA draft: identify unknown talkgroups](/help/cora-talkgroup-identification)
- Relay implementation: `scripts/tr-encrypted-relay.py`
- API: `POST /events/encrypted` (metadata only — no WAV)
- Ingest reject gate: `api/app/encryption_guard.py`
