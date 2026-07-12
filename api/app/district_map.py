"""Render district activity choropleth as SVG or PNG.

Mirrors the dashboard map colors/projection so embeds and CORA packets can
snapshot the same view without a browser.
"""

from __future__ import annotations

import html
import io
from typing import Any

from app.districts import (
    district_id_properties,
    get_district_activity,
    list_agencies,
    load_agency_geojson,
)


def _district_fill(intensity: float, has_mapping: bool) -> str:
    if not has_mapping:
        return "#334155"
    if not intensity:
        return "#1e3a5f"
    if intensity < 0.25:
        return "#1d4ed8"
    if intensity < 0.5:
        return "#2563eb"
    if intensity < 0.75:
        return "#3b82f6"
    if intensity < 0.95:
        return "#f59e0b"
    return "#f97316"


def _walk_coords(value: Any, sink: list[tuple[float, float]]) -> None:
    if not isinstance(value, list) or not value:
        return
    if isinstance(value[0], (int, float)):
        sink.append((float(value[0]), float(value[1])))
        return
    for child in value:
        _walk_coords(child, sink)


def _feature_bounds(features: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for feature in features:
        geometry = (feature or {}).get("geometry") or {}
        _walk_coords(geometry.get("coordinates"), points)
    if not points:
        return (-105.1, 39.55, -104.6, 39.95)
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return (min(lons), min(lats), max(lons), max(lats))


def _project(
    lon: float,
    lat: float,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    pad: float = 8.0,
) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = bounds
    x = pad + ((lon - min_lon) / max(max_lon - min_lon, 1e-9)) * (width - pad * 2)
    y = pad + ((max_lat - lat) / max(max_lat - min_lat, 1e-9)) * (height - pad * 2)
    return (x, y)


def _ring_to_path(
    ring: list[Any],
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    if not ring or len(ring) < 2:
        return ""
    parts: list[str] = []
    for index, pt in enumerate(ring):
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        x, y = _project(float(pt[0]), float(pt[1]), bounds, width, height)
        parts.append(f"{'M' if index == 0 else 'L'}{x:.1f} {y:.1f}")
    if not parts:
        return ""
    return " ".join(parts) + " Z"


def _geometry_to_path(
    geometry: dict[str, Any] | None,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    if not geometry:
        return ""
    geo_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if geo_type == "Polygon":
        return " ".join(_ring_to_path(ring, bounds, width, height) for ring in coords)
    if geo_type == "MultiPolygon":
        chunks: list[str] = []
        for poly in coords:
            for ring in poly or []:
                path = _ring_to_path(ring, bounds, width, height)
                if path:
                    chunks.append(path)
        return " ".join(chunks)
    return ""


def _ring_to_pixels(
    ring: list[Any],
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for pt in ring or []:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        x, y = _project(float(pt[0]), float(pt[1]), bounds, width, height)
        points.append((int(round(x)), int(round(y))))
    return points


def _geometry_polygons(
    geometry: dict[str, Any] | None,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
) -> list[list[tuple[int, int]]]:
    if not geometry:
        return []
    geo_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    polys: list[list[tuple[int, int]]] = []
    if geo_type == "Polygon":
        # Outer ring only for fill (holes omitted for snapshot simplicity).
        if coords:
            pts = _ring_to_pixels(coords[0], bounds, width, height)
            if len(pts) >= 3:
                polys.append(pts)
    elif geo_type == "MultiPolygon":
        for poly in coords:
            if not poly:
                continue
            pts = _ring_to_pixels(poly[0], bounds, width, height)
            if len(pts) >= 3:
                polys.append(pts)
    return polys


def _feature_district_id(agency: str, feature: dict[str, Any]) -> str | None:
    props = feature.get("properties") or {}
    raw = None
    for key in district_id_properties():
        if props.get(key) not in (None, ""):
            raw = props.get(key)
            break
    try:
        num = int(float(raw))
    except (TypeError, ValueError):
        return None
    return f"{agency}-{num}"


def _collect_features(
    stats: dict[str, Any],
    *,
    agency_filter: str | None = None,
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in (stats.get("districts") or [])}
    agencies = [
        a
        for a in (stats.get("agencies") or list_agencies())
        if a and a.get("id") and a.get("available") is not False
    ]
    if agency_filter:
        agencies = [a for a in agencies if a["id"] == agency_filter]

    features: list[dict[str, Any]] = []
    for agency in agencies:
        agency_id = agency["id"]
        geo = load_agency_geojson(agency_id)
        if not geo or not geo.get("features"):
            continue
        for feature in geo["features"]:
            district_id = _feature_district_id(agency_id, feature)
            if not district_id:
                continue
            features.append(
                {
                    "agency": agency_id,
                    "id": district_id,
                    "feature": feature,
                    "stats": by_id.get(district_id),
                }
            )
    return features


def render_district_map_svg(
    *,
    minutes: int = 60,
    agency: str | None = None,
    width: int = 840,
    height: int = 520,
) -> str:
    """Return an SVG document for current district activity."""
    width = max(240, min(int(width), 2400))
    height = max(160, min(int(height), 1800))
    stats = get_district_activity(minutes=minutes)
    items = _collect_features(stats, agency_filter=agency)
    if not items:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
            f'<rect width="100%" height="100%" fill="#0f172a"/>'
            f'<text x="50%" y="50%" fill="#94a3b8" text-anchor="middle" '
            f'font-family="system-ui,sans-serif" font-size="16">'
            f"District GeoJSON not available</text></svg>"
        )

    map_height = height - 56
    bounds = _feature_bounds([item["feature"] for item in items])
    paths: list[str] = []
    for item in items:
        counts = (item.get("stats") or {}).get("counts") or {}
        total = int(counts.get("total") or 0)
        intensity = float((item.get("stats") or {}).get("intensity") or 0)
        talkgroups = (item.get("stats") or {}).get("talkgroups") or []
        has_mapping = bool(talkgroups)
        label = (item.get("stats") or {}).get("label") or item["id"]
        d = _geometry_to_path(item["feature"].get("geometry"), bounds, width, map_height)
        if not d:
            continue
        fill = _district_fill(intensity, has_mapping)
        title = (
            f"{label}: {total} calls in {stats.get('minutes')}m "
            f"(enc {counts.get('encrypted', 0)}, clear {counts.get('completed', 0)}, "
            f"fail {counts.get('failed', 0)})"
            if has_mapping
            else f"{label}: {(item.get('stats') or {}).get('note') or 'no district talkgroup'}"
        )
        paths.append(
            f'<path d="{d}" fill="{fill}" stroke="#0f172a" stroke-width="0.8">'
            f"<title>{html.escape(title)}</title></path>"
        )

    hot = sorted(
        [
            d
            for d in (stats.get("districts") or [])
            if (d.get("counts") or {}).get("total", 0) > 0
            and (agency is None or d.get("agency") == agency)
        ],
        key=lambda d: int((d.get("counts") or {}).get("total") or 0),
        reverse=True,
    )[:3]
    hot_text = (
        " · ".join(f"{d.get('label')} {d.get('counts', {}).get('total')}" for d in hot)
        if hot
        else "no district TG activity"
    )
    title = html.escape(
        f"District activity · last {stats.get('minutes')}m · {hot_text}"
    )
    legend = (
        '<g font-family="system-ui,sans-serif" font-size="11" fill="#cbd5e1">'
        f'<rect x="12" y="{height - 40}" width="14" height="14" fill="#1e3a5f" rx="2"/>'
        f'<text x="30" y="{height - 29}">Quiet</text>'
        f'<rect x="90" y="{height - 40}" width="14" height="14" fill="#2563eb" rx="2"/>'
        f'<text x="108" y="{height - 29}">Busy</text>'
        f'<rect x="160" y="{height - 40}" width="14" height="14" fill="#f59e0b" rx="2"/>'
        f'<text x="178" y="{height - 29}">Hottest</text>'
        f'<text x="12" y="{height - 12}" fill="#94a3b8">{title}</text>'
        "</g>"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Police district activity">'
        f'<rect width="100%" height="100%" fill="#0f172a"/>'
        f"{''.join(paths)}"
        f"{legend}"
        f"</svg>"
    )


def render_district_map_png(
    *,
    minutes: int = 60,
    agency: str | None = None,
    width: int = 840,
    height: int = 520,
) -> bytes:
    """Rasterize the district choropleth with Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    width = max(240, min(int(width), 2400))
    height = max(160, min(int(height), 1800))
    stats = get_district_activity(minutes=minutes)
    items = _collect_features(stats, agency_filter=agency)

    image = Image.new("RGB", (width, height), "#0f172a")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    if not items:
        draw.text(
            (width // 2, height // 2),
            "District GeoJSON not available",
            fill="#94a3b8",
            anchor="mm",
            font=font,
        )
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    map_height = height - 56
    bounds = _feature_bounds([item["feature"] for item in items])
    for item in items:
        intensity = float((item.get("stats") or {}).get("intensity") or 0)
        talkgroups = (item.get("stats") or {}).get("talkgroups") or []
        fill = _district_fill(intensity, bool(talkgroups))
        for poly in _geometry_polygons(
            item["feature"].get("geometry"), bounds, width, map_height
        ):
            draw.polygon(poly, fill=fill, outline="#0f172a")

    hot = sorted(
        [
            d
            for d in (stats.get("districts") or [])
            if (d.get("counts") or {}).get("total", 0) > 0
            and (agency is None or d.get("agency") == agency)
        ],
        key=lambda d: int((d.get("counts") or {}).get("total") or 0),
        reverse=True,
    )[:3]
    hot_text = (
        " · ".join(f"{d.get('label')} {d.get('counts', {}).get('total')}" for d in hot)
        if hot
        else "no district TG activity"
    )
    caption = f"District activity · last {stats.get('minutes')}m · {hot_text}"

    legend = [
        ("#1e3a5f", "Quiet"),
        ("#2563eb", "Busy"),
        ("#f59e0b", "Hottest"),
    ]
    x = 12
    y = height - 40
    for color, label in legend:
        draw.rectangle((x, y, x + 14, y + 14), fill=color)
        draw.text((x + 18, y + 1), label, fill="#cbd5e1", font=font)
        x += 78
    draw.text((12, height - 18), caption, fill="#94a3b8", font=font)

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_district_map_payload_json(
    *,
    minutes: int = 60,
    agency: str | None = None,
) -> dict[str, Any]:
    """Debug helper: stats + feature count used by the image renderer."""
    stats = get_district_activity(minutes=minutes)
    items = _collect_features(stats, agency_filter=agency)
    return {
        "minutes": stats.get("minutes"),
        "agency": agency,
        "feature_count": len(items),
        "districts": stats.get("districts"),
        "agencies": stats.get("agencies"),
    }
