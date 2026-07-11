"""Police-district talkgroup mapping and activity aggregation.

District agencies, GeoJSON filenames, and talkgroup maps are loaded from
districts.json (see config/districts.json). Defaults preserve Denver/Aurora.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import get_db, load_talkgroups_catalog

_DISTRICT_NUM_RE = re.compile(
    r"\b(?:district|dist)\s*(\d+)\b|\b(?:disp|tac|district)\s*(\d+)\b",
    re.IGNORECASE,
)

_config_cache: dict[str, Any] | None = None
_config_mtime: float | None = None
_config_path_used: Path | None = None

_DEFAULT_ID_PROPERTIES = ["DIST_NUM", "POLICE_DISTRICT", "DISTRICT", "district"]


def gis_dir() -> Path:
    return settings.gis_dir


def resolve_districts_config_path() -> Path:
    """Prefer /data/districts.json; fall back to repo config/districts.json."""
    primary = settings.districts_config_path
    if primary.is_file():
        return primary
    # api/app/districts.py -> parents[2] = repo root when running from source
    repo_fallback = Path(__file__).resolve().parents[2] / "config" / "districts.json"
    if repo_fallback.is_file():
        return repo_fallback
    # Docker image without bind-mount: also try alongside DATA_DIR parent patterns
    alt = settings.data_dir.parent / "config" / "districts.json"
    if alt.is_file():
        return alt
    return primary


def load_districts_config(*, force: bool = False) -> dict[str, Any]:
    """Load districts.json with mtime cache."""
    global _config_cache, _config_mtime, _config_path_used
    path = resolve_districts_config_path()
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None

    if (
        not force
        and _config_cache is not None
        and _config_path_used == path
        and _config_mtime == mtime
    ):
        return _config_cache

    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"agencies": [], "districts": []}
    else:
        payload = {"agencies": [], "districts": []}

    if not isinstance(payload, dict):
        payload = {"agencies": [], "districts": []}
    payload.setdefault("district_id_properties", list(_DEFAULT_ID_PROPERTIES))
    payload.setdefault("agencies", [])
    payload.setdefault("districts", [])

    _config_cache = payload
    _config_mtime = mtime
    _config_path_used = path
    return payload


def list_agencies() -> list[dict[str, Any]]:
    agencies = []
    for raw in load_districts_config().get("agencies") or []:
        agency_id = str(raw.get("id") or "").strip()
        if not agency_id:
            continue
        agencies.append(
            {
                "id": agency_id,
                "label": str(raw.get("label") or agency_id).strip() or agency_id,
                "geojson": str(raw.get("geojson") or "").strip(),
                "catalog_keywords": [
                    str(k).lower()
                    for k in (raw.get("catalog_keywords") or [])
                    if str(k).strip()
                ],
                "geojson_url": f"/gis/{agency_id}.geojson",
            }
        )
    return agencies


def district_id_properties() -> list[str]:
    props = load_districts_config().get("district_id_properties") or _DEFAULT_ID_PROPERTIES
    return [str(p) for p in props if str(p).strip()]


def geojson_path(agency: str) -> Path | None:
    agency = (agency or "").strip()
    for item in list_agencies():
        if item["id"] != agency:
            continue
        filename = item.get("geojson") or ""
        if not filename:
            return None
        # Prevent path traversal
        name = Path(filename).name
        path = gis_dir() / name
        return path if path.is_file() else None
    return None


def load_agency_geojson(agency: str) -> dict[str, Any] | None:
    path = geojson_path(agency)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _configured_districts() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in load_districts_config().get("districts") or []:
        district_id = str(raw.get("id") or "").strip()
        agency = str(raw.get("agency") or "").strip()
        if not district_id or not agency:
            continue
        talkgroups = [int(tg) for tg in (raw.get("talkgroups") or [])]
        primary = raw.get("primary_talkgroup")
        item: dict[str, Any] = {
            "id": district_id,
            "agency": agency,
            "district": raw.get("district"),
            "label": str(raw.get("label") or district_id),
            "talkgroups": talkgroups,
            "primary_talkgroup": int(primary) if primary is not None else None,
        }
        if raw.get("note"):
            item["note"] = str(raw["note"])
        out.append(item)
    return out


def _agency_for_catalog_blob(blob_lower: str) -> str | None:
    for agency in list_agencies():
        for keyword in agency.get("catalog_keywords") or []:
            if keyword and keyword in blob_lower:
                return agency["id"]
    return None


def district_definitions() -> list[dict[str, Any]]:
    """Return district defs, merging any extra TGs discovered from the catalog."""
    catalog = load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")
    by_id = {item["id"]: dict(item) for item in _configured_districts()}
    for item in by_id.values():
        item["talkgroups"] = list(item.get("talkgroups") or [])

    for row in catalog:
        talkgroup = row.get("talkgroup")
        if talkgroup is None:
            continue
        blob = " ".join(
            str(row.get(key) or "")
            for key in ("talkgroup_tag", "description", "category")
        )
        match = _DISTRICT_NUM_RE.search(blob)
        if not match:
            continue
        district_num = int(match.group(1) or match.group(2))
        agency = _agency_for_catalog_blob(blob.lower())
        if not agency:
            continue
        district_id = f"{agency}-{district_num}"
        item = by_id.get(district_id)
        if not item:
            continue
        tg = int(talkgroup)
        if tg not in item["talkgroups"]:
            item["talkgroups"].append(tg)

    return list(by_id.values())


def get_district_activity(*, minutes: int = 60) -> dict[str, Any]:
    """Aggregate recent call counts onto police districts via talkgroup mapping."""
    minutes = max(5, min(int(minutes), 24 * 60))
    definitions = district_definitions()
    agencies = list_agencies()
    tg_to_district: dict[int, str] = {}
    for item in definitions:
        for talkgroup in item.get("talkgroups") or []:
            tg_to_district[int(talkgroup)] = item["id"]

    talkgroups = sorted(tg_to_district)
    counts_by_district: dict[str, dict[str, int]] = {
        item["id"]: {
            "total": 0,
            "encrypted": 0,
            "completed": 0,
            "failed": 0,
            "other": 0,
        }
        for item in definitions
    }
    talkgroup_counts: dict[int, int] = {}

    if talkgroups:
        placeholders = ",".join("?" for _ in talkgroups)
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT talkgroup, status, COUNT(*) AS count
                FROM calls
                WHERE talkgroup IN ({placeholders})
                  AND created_at >= datetime('now', '-{minutes} minutes')
                GROUP BY talkgroup, status
                """,
                talkgroups,
            ).fetchall()

        for row in rows:
            talkgroup = int(row["talkgroup"])
            district_id = tg_to_district.get(talkgroup)
            if not district_id:
                continue
            count = int(row["count"] or 0)
            status_name = str(row["status"] or "")
            bucket = counts_by_district[district_id]
            bucket["total"] += count
            talkgroup_counts[talkgroup] = talkgroup_counts.get(talkgroup, 0) + count
            if status_name in ("encrypted", "completed", "failed"):
                bucket[status_name] += count
            else:
                bucket["other"] += count

    max_total = max((bucket["total"] for bucket in counts_by_district.values()), default=0)
    districts = []
    for item in definitions:
        counts = counts_by_district[item["id"]]
        intensity = (counts["total"] / max_total) if max_total else 0.0
        districts.append(
            {
                **item,
                "counts": counts,
                "intensity": round(intensity, 3),
                "talkgroup_counts": {
                    str(tg): talkgroup_counts.get(int(tg), 0)
                    for tg in item.get("talkgroups") or []
                },
            }
        )

    gis_availability = {
        agency["id"]: geojson_path(agency["id"]) is not None for agency in agencies
    }
    agency_payload = [
        {
            "id": agency["id"],
            "label": agency["label"],
            "geojson_url": agency["geojson_url"],
            "available": gis_availability.get(agency["id"], False),
        }
        for agency in agencies
    ]

    return {
        "minutes": minutes,
        "max_total": max_total,
        "district_id_properties": district_id_properties(),
        "gis": gis_availability,
        "agencies": agency_payload,
        "districts": districts,
    }
