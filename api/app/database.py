import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.audio_dir.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                system_name TEXT,
                talkgroup INTEGER,
                talkgroup_tag TEXT,
                src INTEGER,
                src_tag TEXT,
                freq REAL,
                call_length REAL,
                wav_path TEXT NOT NULL,
                json_path TEXT,
                metadata_json TEXT,
                transcript TEXT,
                backend_used TEXT,
                error_message TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_talkgroup ON calls(talkgroup)"
        )


@contextmanager
def get_db():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _is_placeholder_catalog_item(item: dict[str, Any]) -> bool:
    tag = str(item.get("talkgroup_tag") or "").strip().lower()
    description = str(item.get("description") or "").strip().lower()
    return tag.startswith("unknown") or description in {"", "unknown"}


def lookup_talkgroup_entry(talkgroup: int) -> dict[str, Any] | None:
    for item in load_talkgroups_catalog(settings.data_dir / "talk_groups.csv"):
        if int(item["talkgroup"]) == talkgroup:
            return item
    return None


def lookup_talkgroup_tag(talkgroup: int) -> str | None:
    item = lookup_talkgroup_entry(talkgroup)
    if not item:
        return None
    tag = item.get("talkgroup_tag") or ""
    return tag or None


def classify_call_addressing(
    *,
    talkgroup: int,
    src: int | None = None,
) -> dict[str, Any]:
    """Infer group vs unit-to-unit from TG ID + catalog.

    Trunk Recorder logs both group and unit-to-unit grants under ``TG:``.
    For unit-to-unit, that field is typically the target radio ID.
    """
    entry = lookup_talkgroup_entry(talkgroup)
    known_group = bool(entry) and not _is_placeholder_catalog_item(entry)
    src_known = src is not None and src > 0

    if known_group:
        return {
            "call_type": "group",
            "target": None,
            "addressing_confidence": "high",
        }

    # Unknown / placeholder TG ID with a real source RID → likely private call.
    if src_known:
        return {
            "call_type": "unit_to_unit",
            "target": talkgroup,
            "addressing_confidence": "medium",
        }

    return {
        "call_type": "unknown",
        "target": talkgroup,
        "addressing_confidence": "low",
    }


def insert_skipped_activity(
    *,
    status: str,
    system_name: str,
    talkgroup: int,
    freq: float,
    src: int | None = None,
    event_time: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    now = event_time or _utc_now()
    talkgroup_tag = lookup_talkgroup_tag(talkgroup)
    addressing = classify_call_addressing(talkgroup=talkgroup, src=src)
    payload = metadata or {}
    payload.update(
        {
            "short_name": system_name,
            "talkgroup": talkgroup,
            "talkgroup_tag": talkgroup_tag,
            "freq": freq,
            "src": src,
            "skip_reason": status,
            **addressing,
        }
    )
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO calls (
                created_at, updated_at, status, system_name, talkgroup,
                talkgroup_tag, src, src_tag, freq, call_length,
                wav_path, json_path, metadata_json, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, '', NULL, ?, 0)
            """,
            (
                now,
                now,
                status,
                system_name,
                talkgroup,
                talkgroup_tag,
                src,
                freq,
                json.dumps(payload),
            ),
        )
        return int(cursor.lastrowid)


def insert_encrypted_activity(
    *,
    system_name: str,
    talkgroup: int,
    freq: float,
    src: int,
    event_time: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    payload = metadata or {}
    payload["encrypted"] = True
    return insert_skipped_activity(
        status="encrypted",
        system_name=system_name,
        talkgroup=talkgroup,
        freq=freq,
        src=src,
        event_time=event_time,
        metadata=payload,
    )


def insert_unknown_talkgroup_activity(
    *,
    system_name: str,
    talkgroup: int,
    freq: float,
    event_time: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    payload = metadata or {}
    payload["unknown_talkgroup"] = True
    return insert_skipped_activity(
        status="unknown_talkgroup",
        system_name=system_name,
        talkgroup=talkgroup,
        freq=freq,
        event_time=event_time,
        metadata=payload,
    )


def insert_call(
    *,
    wav_path: Path,
    json_path: Path | None,
    metadata: dict[str, Any],
) -> int:
    now = _utc_now()
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO calls (
                created_at, updated_at, status, system_name, talkgroup,
                talkgroup_tag, src, src_tag, freq, call_length,
                wav_path, json_path, metadata_json, retry_count
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                now,
                now,
                metadata.get("short_name"),
                metadata.get("talkgroup"),
                metadata.get("talkgroup_tag"),
                metadata.get("src"),
                metadata.get("src_tag"),
                metadata.get("freq"),
                metadata.get("call_length"),
                str(wav_path),
                str(json_path) if json_path else None,
                json.dumps(metadata),
            ),
        )
        return int(cursor.lastrowid)


def get_call(call_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return dict(row) if row else None


def _normalize_time_bound(value: str | None) -> str | None:
    """Normalize ISO-ish timestamps for lexicographic SQLite comparisons."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        # Fall back to a truncated comparable prefix.
        return text[:19].replace(" ", "T")


def list_calls(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    talkgroup: int | None = None,
    system_name: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    transcript_query: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM calls"
    params: list[Any] = []
    clauses: list[str] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if talkgroup is not None:
        clauses.append("talkgroup = ?")
        params.append(talkgroup)
    if system_name:
        clauses.append("system_name = ?")
        params.append(system_name)
    after = _normalize_time_bound(created_after)
    before = _normalize_time_bound(created_before)
    if after:
        clauses.append("substr(replace(created_at, ' ', 'T'), 1, 19) >= ?")
        params.append(after)
    if before:
        clauses.append("substr(replace(created_at, ' ', 'T'), 1, 19) <= ?")
        params.append(before)
    if transcript_query:
        needle = transcript_query.strip()
        if needle:
            # Escape LIKE wildcards so user input is matched literally.
            escaped = (
                needle.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append("transcript LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_talkgroup_stats(*, system_name: str | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT talkgroup,
               COUNT(*) AS call_count,
               MAX(created_at) AS last_call_at
        FROM calls
        WHERE talkgroup IS NOT NULL
    """
    params: list[Any] = []
    if system_name:
        query += " AND system_name = ?"
        params.append(system_name)
    query += """
        GROUP BY talkgroup
        ORDER BY last_call_at DESC
    """
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_system_stats(*, active_within_minutes: int = 5) -> list[dict[str, Any]]:
    """Systems from Trunk Recorder config plus recent call activity."""
    configured: list[dict[str, Any]] = []
    config_path = settings.trunk_recorder_config
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            for index, system in enumerate(payload.get("systems") or []):
                short_name = system.get("shortName") or f"system-{index}"
                configured.append(
                    {
                        "name": short_name,
                        "type": system.get("type"),
                        "control_channels": system.get("control_channels") or [],
                        "configured": True,
                    }
                )
        except (OSError, json.JSONDecodeError, TypeError):
            configured = []

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT system_name AS name,
                   COUNT(*) AS call_count,
                   MAX(created_at) AS last_call_at
            FROM calls
            WHERE system_name IS NOT NULL AND system_name != ''
            GROUP BY system_name
            ORDER BY last_call_at DESC
            """
        ).fetchall()
    activity = {row["name"]: dict(row) for row in rows}

    systems: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in configured:
        name = item["name"]
        seen.add(name)
        merged = dict(item)
        if name in activity:
            merged.update(activity[name])
        else:
            merged.setdefault("call_count", 0)
            merged.setdefault("last_call_at", None)
        systems.append(merged)

    for name, stat in activity.items():
        if name in seen:
            continue
        systems.append(
            {
                "name": name,
                "type": None,
                "control_channels": [],
                "configured": False,
                **stat,
            }
        )

    now = datetime.now(timezone.utc)
    for system in systems:
        last_call_at = system.get("last_call_at")
        active = False
        if last_call_at:
            try:
                parsed = datetime.fromisoformat(str(last_call_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                age_minutes = (now - parsed.astimezone(timezone.utc)).total_seconds() / 60
                active = age_minutes <= active_within_minutes
            except ValueError:
                active = False
        system["active"] = active

    systems.sort(
        key=lambda item: (
            0 if item.get("active") else 1,
            -(item.get("call_count") or 0),
            str(item.get("name") or ""),
        )
    )
    return systems


def get_top_talkgroup_activity(*, hours: int = 6, limit: int = 8) -> list[dict[str, Any]]:
    catalog = {
        int(item["talkgroup"]): item
        for item in load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")
        if item.get("talkgroup") is not None
    }
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT talkgroup, COUNT(*) AS count
            FROM calls
            WHERE talkgroup IS NOT NULL
              AND created_at >= datetime('now', '-{int(hours)} hours')
            GROUP BY talkgroup
            ORDER BY count DESC, talkgroup ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        talkgroup = int(row["talkgroup"])
        catalog_item = catalog.get(talkgroup) or {}
        tag = str(catalog_item.get("talkgroup_tag") or "").strip() or f"TG {talkgroup}"
        results.append(
            {
                "talkgroup": talkgroup,
                "talkgroup_tag": tag,
                "count": int(row["count"] or 0),
            }
        )
    return results


def get_hourly_talkgroup_activity(*, hours: int = 6, talkgroup: int) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT strftime('%Y-%m-%d %H:00', created_at) AS bucket,
                   COUNT(*) AS count
            FROM calls
            WHERE talkgroup = ?
              AND created_at >= datetime('now', '-{int(hours)} hours')
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (talkgroup,),
        ).fetchall()
        return [{"bucket": row["bucket"], "count": row["count"]} for row in rows]


def _encrypted_family_key(talkgroup: int | None, category: str | None) -> str:
    cat = (category or "").strip()
    if cat and cat.lower() != "unknown":
        return f"cat:{cat}"
    if talkgroup is None:
        return "unknown"
    return f"band:{int(talkgroup) // 100 * 100}"


def get_encrypted_anomalies(
    *,
    window_minutes: int = 15,
    baseline_days: int = 14,
    min_recent: int = 5,
    rate_threshold: float = 3.0,
    sibling_rate_threshold: float = 2.0,
    limit: int = 8,
) -> dict[str, Any]:
    """Score encrypted-tempo anomalies vs same weekday/hour baselines.

    Returns possible-incident candidates from grant-rate spikes, multi-TG
    co-activation within a family, and unique RID surges. This is tempo only —
    not content classification.
    """
    window_minutes = max(5, min(int(window_minutes), 60))
    baseline_days = max(3, min(int(baseline_days), 30))
    min_recent = max(3, int(min_recent))
    rate_threshold = max(1.5, float(rate_threshold))
    sibling_rate_threshold = max(1.5, float(sibling_rate_threshold))
    limit = max(1, min(int(limit), 25))

    catalog = {
        int(item["talkgroup"]): item
        for item in load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")
        if item.get("talkgroup") is not None
    }

    with get_db() as conn:
        recent_rows = conn.execute(
            f"""
            SELECT talkgroup,
                   system_name,
                   MAX(talkgroup_tag) AS talkgroup_tag,
                   COUNT(*) AS recent_count,
                   COUNT(DISTINCT CASE
                     WHEN src IS NOT NULL AND src > 0 THEN src
                   END) AS unique_srcs
            FROM calls
            WHERE status = 'encrypted'
              AND talkgroup IS NOT NULL
              AND created_at >= datetime('now', '-{window_minutes} minutes')
            GROUP BY talkgroup, system_name
            HAVING recent_count >= ?
            ORDER BY recent_count DESC
            """,
            (min_recent,),
        ).fetchall()

        baseline_rows = conn.execute(
            f"""
            SELECT talkgroup,
                   AVG(hourly_count) AS avg_hourly,
                   COUNT(*) AS sample_days
            FROM (
              SELECT talkgroup,
                     date(created_at) AS day,
                     COUNT(*) AS hourly_count
              FROM calls
              WHERE status = 'encrypted'
                AND talkgroup IS NOT NULL
                AND created_at >= datetime('now', '-{baseline_days} days')
                AND created_at < datetime('now', '-{window_minutes} minutes')
                AND strftime('%w', created_at) = strftime('%w', 'now')
                AND strftime('%H', created_at) = strftime('%H', 'now')
              GROUP BY talkgroup, day
            )
            GROUP BY talkgroup
            """
        ).fetchall()

        history_row = conn.execute(
            f"""
            SELECT MIN(created_at) AS first_at,
                   COUNT(*) AS encrypted_total,
                   CAST(
                     (julianday('now', '-{window_minutes} minutes') - julianday(MIN(created_at)))
                     * 24 * 60 AS REAL
                   ) AS history_minutes
            FROM calls
            WHERE status = 'encrypted'
              AND created_at < datetime('now', '-{window_minutes} minutes')
            """
        ).fetchone()

        history_minutes = float((history_row["history_minutes"] if history_row else 0) or 0)
        # Use actual elapsed history, not the configured baseline_days span.
        windows_in_history = max(history_minutes / window_minutes, 1.0)
        fallback_rows = conn.execute(
            f"""
            SELECT talkgroup,
                   COUNT(*) AS total_count,
                   COUNT(*) * 1.0 / ? AS avg_per_window
            FROM calls
            WHERE status = 'encrypted'
              AND talkgroup IS NOT NULL
              AND created_at >= datetime('now', '-{baseline_days} days')
              AND created_at < datetime('now', '-{window_minutes} minutes')
            GROUP BY talkgroup
            """,
            (windows_in_history,),
        ).fetchall()

    baseline_by_tg = {
        int(row["talkgroup"]): {
            "avg_hourly": float(row["avg_hourly"] or 0),
            "sample_days": int(row["sample_days"] or 0),
        }
        for row in baseline_rows
        if row["talkgroup"] is not None
    }
    fallback_by_tg = {
        int(row["talkgroup"]): {
            "avg_per_window": float(row["avg_per_window"] or 0),
            "total_count": int(row["total_count"] or 0),
        }
        for row in fallback_rows
        if row["talkgroup"] is not None
    }

    encrypted_total = int((history_row["encrypted_total"] if history_row else 0) or 0)
    cold_start = encrypted_total < 500

    window_scale = window_minutes / 60.0
    candidates: list[dict[str, Any]] = []
    for row in recent_rows:
        talkgroup = int(row["talkgroup"])
        recent_count = int(row["recent_count"] or 0)
        unique_srcs = int(row["unique_srcs"] or 0)
        catalog_item = catalog.get(talkgroup) or {}
        category = str(catalog_item.get("category") or "").strip()
        tag = (
            str(row["talkgroup_tag"] or "").strip()
            or str(catalog_item.get("talkgroup_tag") or "").strip()
            or f"TG {talkgroup}"
        )
        weekday_baseline = baseline_by_tg.get(talkgroup) or {
            "avg_hourly": 0.0,
            "sample_days": 0,
        }
        fallback = fallback_by_tg.get(talkgroup) or {
            "avg_per_window": 0.0,
            "total_count": 0,
        }

        baseline_source = "none"
        expected = 0.0
        sample_days = weekday_baseline["sample_days"]
        if sample_days >= 2 and weekday_baseline["avg_hourly"] > 0:
            expected = weekday_baseline["avg_hourly"] * window_scale
            baseline_source = "weekday_hour"
        elif fallback["total_count"] >= 20 and fallback["avg_per_window"] > 0:
            expected = fallback["avg_per_window"]
            sample_days = max(sample_days, 1)
            baseline_source = "overall"
        elif fallback["total_count"] > 0:
            expected = max(fallback["avg_per_window"], 0.5)
            baseline_source = "sparse"
        else:
            expected = 0.0
            baseline_source = "none"

        rate_ratio = (recent_count / expected) if expected > 0 else None
        family = _encrypted_family_key(talkgroup, category)
        candidates.append(
            {
                "talkgroup": talkgroup,
                "talkgroup_tag": tag,
                "system_name": row["system_name"],
                "category": category or None,
                "family": family,
                "recent_count": recent_count,
                "unique_srcs": unique_srcs,
                "expected_count": round(expected, 2) if expected else None,
                "rate_ratio": round(rate_ratio, 2) if rate_ratio is not None else None,
                "baseline_sample_days": sample_days,
                "baseline_source": baseline_source,
                "window_minutes": window_minutes,
            }
        )

    family_elevated: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        ratio = item["rate_ratio"]
        # Without a usable baseline, treat all busy TGs as "elevated" for sibling checks.
        if ratio is None or ratio >= sibling_rate_threshold or (
            item["baseline_source"] in {"none", "sparse"} and item["recent_count"] >= min_recent
        ):
            family_elevated.setdefault(item["family"], []).append(item)

    anomalies: list[dict[str, Any]] = []
    for item in candidates:
        siblings = [
            sibling
            for sibling in family_elevated.get(item["family"], [])
            if sibling["talkgroup"] != item["talkgroup"]
        ]
        sibling_count = len(siblings)
        reasons: list[str] = []
        score = 0.0
        ratio = item["rate_ratio"]
        has_rate_baseline = item["baseline_source"] in {"weekday_hour", "overall"}

        if (
            has_rate_baseline
            and ratio is not None
            and ratio >= rate_threshold
            and item["recent_count"] >= min_recent
        ):
            label = (
                "weekday/hour"
                if item["baseline_source"] == "weekday_hour"
                else "recent average"
            )
            reasons.append(f"{ratio:.1f}× {label}")
            score += min(ratio, 10.0)

        if sibling_count >= 1 and (
            (ratio is not None and ratio >= sibling_rate_threshold)
            or item["baseline_source"] in {"none", "sparse"}
        ):
            # During cold-start, require stronger co-activation so busy dispatch
            # alone does not look like an incident.
            if cold_start and sibling_count < 2:
                pass
            else:
                reasons.append(f"{sibling_count + 1} related TGs elevated together")
                score += 1.5 + min(sibling_count, 4) * 0.75

        rid_floor = 6 if cold_start else 4
        if item["unique_srcs"] >= rid_floor and item["recent_count"] >= min_recent:
            reasons.append(f"{item['unique_srcs']} distinct RIDs keyed up")
            score += min(item["unique_srcs"] / 2.0, 4.0)

        # Cold-start / rare TG: little baseline history but sudden activity.
        rare_floor = max(min_recent + 6, 12) if cold_start else max(min_recent + 2, 8)
        if (
            item["baseline_source"] in {"none", "sparse"}
            and item["recent_count"] >= rare_floor
            and item["unique_srcs"] >= rid_floor
            and sibling_count >= (2 if cold_start else 0)
        ):
            reasons.append("rarely active TG suddenly busy")
            score += 2.0

        if not reasons:
            continue

        # Without a rate baseline, require at least two independent signals.
        if not has_rate_baseline and len(reasons) < 2:
            continue

        confidence = "low"
        if score >= 7 or (
            has_rate_baseline
            and ratio is not None
            and ratio >= rate_threshold
            and sibling_count >= 1
        ):
            confidence = "high"
        elif score >= 4:
            confidence = "medium"

        anomalies.append(
            {
                **item,
                "sibling_count": sibling_count,
                "sibling_talkgroups": [
                    {
                        "talkgroup": sibling["talkgroup"],
                        "talkgroup_tag": sibling["talkgroup_tag"],
                        "recent_count": sibling["recent_count"],
                        "rate_ratio": sibling["rate_ratio"],
                    }
                    for sibling in sorted(
                        siblings, key=lambda value: value["recent_count"], reverse=True
                    )[:5]
                ],
                "score": round(score, 2),
                "confidence": confidence,
                "reasons": reasons,
            }
        )

    anomalies.sort(key=lambda item: (item["score"], item["recent_count"]), reverse=True)
    anomalies = anomalies[:limit]
    high = sum(1 for item in anomalies if item["confidence"] == "high")
    medium = sum(1 for item in anomalies if item["confidence"] == "medium")

    return {
        "window_minutes": window_minutes,
        "baseline_days": baseline_days,
        "generated_at": _utc_now(),
        "cold_start": cold_start,
        "anomaly_count": len(anomalies),
        "high_count": high,
        "medium_count": medium,
        "active": len(anomalies) > 0,
        "anomalies": anomalies,
    }



def load_talkgroups_catalog(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.is_file():
        return []

    catalog: dict[int, dict[str, Any]] = {}
    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                talkgroup = int(row["Decimal"])
            except (KeyError, TypeError, ValueError):
                continue
            candidate = {
                "talkgroup": talkgroup,
                "talkgroup_tag": row.get("Alpha Tag") or row.get("Tag") or "",
                "description": row.get("Description") or "",
                "category": row.get("Category") or "",
                "call_count": 0,
            }
            existing = catalog.get(talkgroup)
            if existing is None or (
                _is_placeholder_catalog_item(existing)
                and not _is_placeholder_catalog_item(candidate)
            ):
                catalog[talkgroup] = candidate
    return list(catalog.values())


def claim_pending_call() -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM calls
            WHERE status = 'pending'
               OR (status = 'failed' AND retry_count < ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (settings.max_retries,),
        ).fetchone()
        if not row:
            return None

        now = _utc_now()
        conn.execute(
            "UPDATE calls SET status = 'processing', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        return dict(row)


def mark_call_completed(
    call_id: int,
    *,
    transcript: str,
    backend_used: str,
    wav_path: Path | str | None = None,
) -> None:
    now = _utc_now()
    with get_db() as conn:
        if wav_path is not None:
            conn.execute(
                """
                UPDATE calls
                SET status = 'completed',
                    updated_at = ?,
                    transcript = ?,
                    backend_used = ?,
                    error_message = NULL,
                    wav_path = ?
                WHERE id = ?
                """,
                (now, transcript, backend_used, str(wav_path), call_id),
            )
        else:
            conn.execute(
                """
                UPDATE calls
                SET status = 'completed',
                    updated_at = ?,
                    transcript = ?,
                    backend_used = ?,
                    error_message = NULL
                WHERE id = ?
                """,
                (now, transcript, backend_used, call_id),
            )


def update_call_audio_path(call_id: int, *, wav_path: Path | str) -> None:
    now = _utc_now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE calls
            SET wav_path = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(wav_path), now, call_id),
        )


def claim_completed_wav_for_compression() -> dict[str, Any] | None:
    """Pick one completed call still stored as WAV for background compression."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM calls
            WHERE status = 'completed'
              AND wav_path LIKE '%.wav'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def mark_call_failed(call_id: int, *, error_message: str, increment_retry: bool) -> None:
    now = _utc_now()
    with get_db() as conn:
        if increment_retry:
            conn.execute(
                """
                UPDATE calls
                SET status = 'failed',
                    updated_at = ?,
                    error_message = ?,
                    transcript = '',
                    retry_count = retry_count + 1
                WHERE id = ?
                """,
                (now, error_message, call_id),
            )
        else:
            conn.execute(
                """
                UPDATE calls
                SET status = 'failed',
                    updated_at = ?,
                    error_message = ?,
                    transcript = ''
                WHERE id = ?
                """,
                (now, error_message, call_id),
            )


def count_calls_by_status() -> dict[str, int]:
    statuses = (
        "pending",
        "processing",
        "completed",
        "failed",
        "encrypted",
        "unknown_talkgroup",
    )
    counts = {status: 0 for status in statuses}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM calls GROUP BY status"
        ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["count"])
    return counts


def get_system_outcome_stats(*, hours: int | None = None) -> dict[str, Any]:
    """Per-system mix of encrypted, transcribed (completed), and failed calls."""
    hours_clause = ""
    if hours is not None:
        hours = max(1, min(int(hours), 168))
        hours_clause = f" AND created_at >= datetime('now', '-{hours} hours')"

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(system_name, ''), 'Unknown') AS system_name,
                   SUM(CASE WHEN status = 'encrypted' THEN 1 ELSE 0 END) AS encrypted,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS transcribed,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM calls
            WHERE status IN ('encrypted', 'completed', 'failed')
              {hours_clause}
            GROUP BY COALESCE(NULLIF(system_name, ''), 'Unknown')
            ORDER BY (encrypted + transcribed + failed) DESC, system_name ASC
            """
        ).fetchall()

    systems = []
    totals = {"encrypted": 0, "transcribed": 0, "failed": 0}
    for row in rows:
        encrypted = int(row["encrypted"] or 0)
        transcribed = int(row["transcribed"] or 0)
        failed = int(row["failed"] or 0)
        total = encrypted + transcribed + failed
        if total <= 0:
            continue
        systems.append(
            {
                "system_name": row["system_name"],
                "encrypted": encrypted,
                "transcribed": transcribed,
                "failed": failed,
                "total": total,
                "encrypted_pct": round(100.0 * encrypted / total, 1),
                "transcribed_pct": round(100.0 * transcribed / total, 1),
                "failed_pct": round(100.0 * failed / total, 1),
            }
        )
        totals["encrypted"] += encrypted
        totals["transcribed"] += transcribed
        totals["failed"] += failed

    return {
        "hours": hours,
        "systems": systems,
        "totals": {
            **totals,
            "total": totals["encrypted"] + totals["transcribed"] + totals["failed"],
        },
    }