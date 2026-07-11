import html
import json
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.alerts import transcript_alert_emojis
from app.audio_storage import audio_media_type
from app.config import settings
from app.database import (
    classify_call_addressing,
    count_calls_by_status,
    get_call,
    get_db,
    get_encrypted_anomalies,
    get_hourly_talkgroup_activity,
    get_system_outcome_stats,
    get_top_talkgroup_activity,
    init_db,
    insert_call,
    insert_encrypted_activity,
    insert_unknown_talkgroup_activity,
    list_calls,
    list_system_stats,
    list_talkgroup_stats,
    load_talkgroups_catalog,
)
from app.districts import geojson_path, get_district_activity, list_agencies
from app.worker import worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


class EncryptedActivityEvent(BaseModel):
    system_name: str
    talkgroup: int = Field(..., ge=1)
    freq: float = Field(..., ge=0)
    src: int = -1
    event_time: str | None = None


class UnknownTalkgroupActivityEvent(BaseModel):
    system_name: str
    talkgroup: int = Field(..., ge=1)
    freq: float = Field(..., ge=0)
    event_time: str | None = None


NON_PLAYABLE_STATUSES = frozenset({"encrypted", "unknown_talkgroup"})


def verify_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> None:
    if not settings.api_key or settings.api_key == "change-me":
        return
    if credentials is None or credentials.credentials != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(
    title="trunk-recorder-vtt",
    description="Trunk Recorder call ingestion and Whisper transcription service",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    counts = count_calls_by_status()
    return {
        "status": "ok",
        "transcription_backend": settings.transcription_backend.value,
        "transcription_fallback": settings.transcription_fallback,
        "queue": counts,
    }


@app.post("/calls", dependencies=[Depends(verify_api_key)])
async def ingest_call(
    call_audio: UploadFile = File(...),
    call_json: UploadFile | None = File(None),
    call_metadata: str | None = Form(None),
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}

    if call_json is not None:
        raw = await call_json.read()
        metadata = json.loads(raw.decode("utf-8"))
    elif call_metadata:
        metadata = json.loads(call_metadata)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_json file or call_metadata form field is required",
        )

    call_length = float(metadata.get("call_length", 0) or 0)
    if call_length < settings.min_call_length:
        return {
            "accepted": False,
            "reason": f"call_length {call_length}s below minimum {settings.min_call_length}s",
        }

    call_uuid = uuid.uuid4().hex
    wav_filename = f"{call_uuid}.wav"
    json_filename = f"{call_uuid}.json"
    wav_path = settings.audio_dir / wav_filename
    json_path = settings.audio_dir / json_filename

    with wav_path.open("wb") as out:
        shutil.copyfileobj(call_audio.file, out)

    json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    call_id = insert_call(wav_path=wav_path, json_path=json_path, metadata=metadata)
    logger.info(
        "Queued call %s system=%s talkgroup=%s length=%.1fs",
        call_id,
        metadata.get("short_name"),
        metadata.get("talkgroup"),
        call_length,
    )

    return {
        "accepted": True,
        "call_id": call_id,
        "status": "pending",
        "wav_path": str(wav_path),
    }


@app.post("/events/encrypted", dependencies=[Depends(verify_api_key)])
async def ingest_encrypted_activity(event: EncryptedActivityEvent) -> dict[str, Any]:
    """Record encrypted-channel activity when Trunk Recorder skips recording."""
    call_id = insert_encrypted_activity(
        system_name=event.system_name,
        talkgroup=event.talkgroup,
        freq=event.freq,
        src=event.src,
        event_time=event.event_time,
    )
    logger.info(
        "Logged encrypted activity %s system=%s talkgroup=%s src=%s",
        call_id,
        event.system_name,
        event.talkgroup,
        event.src,
    )
    return {
        "accepted": True,
        "call_id": call_id,
        "status": "encrypted",
    }


@app.post("/events/unknown-talkgroup", dependencies=[Depends(verify_api_key)])
async def ingest_unknown_talkgroup_activity(
    event: UnknownTalkgroupActivityEvent,
) -> dict[str, Any]:
    """Record activity for talk groups missing from talk_groups.csv."""
    call_id = insert_unknown_talkgroup_activity(
        system_name=event.system_name,
        talkgroup=event.talkgroup,
        freq=event.freq,
        event_time=event.event_time,
    )
    logger.info(
        "Logged unknown talkgroup %s system=%s talkgroup=%s",
        call_id,
        event.system_name,
        event.talkgroup,
    )
    return {
        "accepted": True,
        "call_id": call_id,
        "status": "unknown_talkgroup",
    }


@app.get("/calls")
async def get_calls(
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    talkgroup: int | None = Query(None),
    system: str | None = Query(None),
    created_after: str | None = Query(None, alias="from"),
    created_before: str | None = Query(None, alias="to"),
    q: str | None = Query(
        None,
        max_length=200,
        description="Case-insensitive substring match against transcript text",
    ),
    alerts_only: bool = Query(
        False,
        description="Return calls whose transcripts match dashboard alert keywords",
    ),
) -> dict[str, Any]:
    calls = list_calls(
        limit=limit,
        offset=offset,
        status=status_filter,
        talkgroup=talkgroup,
        system_name=system,
        created_after=created_after,
        created_before=created_before,
        transcript_query=q,
        alerts_only=alerts_only,
    )
    return {
        "calls": _enrich_calls_with_category(calls),
        "limit": limit,
        "offset": offset,
        "talkgroup": talkgroup,
        "system": system,
        "from": created_after,
        "to": created_before,
        "q": q,
        "alerts_only": alerts_only,
    }


@app.get("/systems")
async def get_systems(
    active_within_minutes: int = Query(5, ge=1, le=60),
) -> dict[str, Any]:
    systems = list_system_stats(active_within_minutes=active_within_minutes)
    return {
        "systems": systems,
        "active_within_minutes": active_within_minutes,
    }


@app.get("/talkgroups")
async def get_talkgroups(
    system: str | None = Query(None),
) -> dict[str, Any]:
    stats = {row["talkgroup"]: row for row in list_talkgroup_stats(system_name=system)}
    catalog = load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")

    if catalog:
        groups: list[dict[str, Any]] = []
        catalog_ids: set[int] = set()
        for item in catalog:
            talkgroup_id = int(item["talkgroup"])
            catalog_ids.add(talkgroup_id)
            merged = dict(item)
            if talkgroup_id in stats:
                # Prefer live CSV labels; only pull activity fields from DB history.
                merged["call_count"] = stats[talkgroup_id].get("call_count", 0)
                merged["last_call_at"] = stats[talkgroup_id].get("last_call_at")
            groups.append(merged)
        for talkgroup_id, stat in stats.items():
            if talkgroup_id not in catalog_ids:
                groups.append(dict(stat))
        groups.sort(
            key=lambda group: (group.get("call_count", 0), group["talkgroup"]),
            reverse=True,
        )
    else:
        groups = list_talkgroup_stats(system_name=system)

    return {"talkgroups": groups, "system": system}


@app.get("/stats/activity")
async def get_activity_stats(
    hours: int = Query(6, ge=1, le=48),
    talkgroup: int | None = Query(None),
) -> dict[str, Any]:
    if talkgroup is not None:
        buckets = get_hourly_talkgroup_activity(hours=hours, talkgroup=talkgroup)
        catalog_item = next(
            (
                item
                for item in load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")
                if item.get("talkgroup") == talkgroup
            ),
            None,
        )
        tag = (catalog_item or {}).get("talkgroup_tag")
        if not tag:
            with get_db() as conn:
                tag_row = conn.execute(
                    """
                    SELECT talkgroup_tag FROM calls
                    WHERE talkgroup = ? AND talkgroup_tag IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (talkgroup,),
                ).fetchone()
            tag = tag_row["talkgroup_tag"] if tag_row else None
        return {
            "mode": "timeline",
            "hours": hours,
            "talkgroup": talkgroup,
            "talkgroup_tag": tag,
            "buckets": buckets,
        }

    return {
        "mode": "talkgroups",
        "hours": hours,
        "talkgroups": get_top_talkgroup_activity(hours=hours, limit=12),
    }


@app.get("/stats/encrypted-anomalies")
async def encrypted_anomalies(
    window_minutes: int = Query(15, ge=5, le=60),
    baseline_days: int = Query(14, ge=3, le=30),
    limit: int = Query(8, ge=1, le=25),
) -> dict[str, Any]:
    """Heuristic encrypted-tempo anomalies (possible incident candidates)."""
    return get_encrypted_anomalies(
        window_minutes=window_minutes,
        baseline_days=baseline_days,
        limit=limit,
    )


@app.get("/stats/system-outcomes")
async def system_outcomes(
    hours: int | None = Query(None, ge=1, le=168),
) -> dict[str, Any]:
    """Per-system encrypted / transcribed / failed mix."""
    return get_system_outcome_stats(hours=hours)


@app.get("/stats/districts")
async def district_stats(
    minutes: int = Query(60, ge=5, le=1440),
) -> dict[str, Any]:
    """Police-district activity via talkgroup → district mapping."""
    return get_district_activity(minutes=minutes)


def _serve_agency_geojson(agency: str) -> JSONResponse:
    """Load GeoJSON for a configured agency id."""
    known = {item["id"] for item in list_agencies()}
    if agency not in known:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown agency")
    path = geojson_path(agency)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GeoJSON not mounted")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read GeoJSON: {exc}",
        ) from exc
    return JSONResponse(payload)


@app.get("/gis/{agency}-police-districts.geojson")
async def police_districts_geojson(agency: str) -> JSONResponse:
    """Legacy alias for denver/aurora GeoJSON URLs."""
    return _serve_agency_geojson(agency)


@app.get("/gis/{agency_id}.geojson")
async def agency_geojson(agency_id: str) -> JSONResponse:
    """Serve district polygons for a configured agency (from districts.json)."""
    return _serve_agency_geojson(agency_id)


def _docs_markdown_path(slug: str) -> Path | None:
    """Resolve an allowlisted docs/*.md file (Docker mount or repo checkout)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", slug):
        return None
    filename = f"{slug}.md"
    candidates = [
        settings.docs_dir / filename,
        Path(__file__).resolve().parents[2] / "docs" / filename,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _rewrite_doc_href(href: str) -> str:
    """Map repo-relative markdown links to served /help/ pages when possible."""
    if href.startswith(("http://", "https://", "/", "#", "mailto:")):
        return href
    path_part, _, fragment = href.partition("#")
    name = Path(path_part).name
    if name.endswith(".md"):
        slug = name[:-3]
        if _docs_markdown_path(slug) is not None:
            return f"/help/{slug}" + (f"#{fragment}" if fragment else "")
    return href


def _markdown_to_simple_html(markdown: str) -> str:
    """Minimal Markdown → HTML for project docs (no extra deps)."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    in_table = False
    in_list = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = " ".join(paragraph)
        out.append(f"<p>{_inline_md(text)}</p>")
        paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    def _inline_md(text: str) -> str:
        escaped = html.escape(text)

        def _link(match: re.Match[str]) -> str:
            label = match.group(1)
            href = _rewrite_doc_href(html.unescape(match.group(2)))
            return (
                f'<a href="{html.escape(href, quote=True)}" '
                f'rel="noopener noreferrer">{label}</a>'
            )

        escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return escaped

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_paragraph()
            close_list()
            close_table()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(html.escape(line) + "\n")
            i += 1
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            close_table()
            i += 1
            continue
        if line.startswith("|"):
            flush_paragraph()
            close_list()
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
                i += 1
                continue
            if not in_table:
                out.append("<table><thead><tr>")
                out.extend(f"<th>{_inline_md(c)}</th>" for c in cells)
                out.append("</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>")
                out.extend(f"<td>{_inline_md(c)}</td>" for c in cells)
                out.append("</tr>")
            i += 1
            continue
        close_table()
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline_md(heading.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("---"):
            flush_paragraph()
            close_list()
            out.append("<hr />")
            i += 1
            continue
        if re.match(r"^[-*]\s+", line):
            flush_paragraph()
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = re.sub(r"^[-*]\s+", "", line)
            out.append(f"<li>{_inline_md(item)}</li>")
            i += 1
            continue
        close_list()
        paragraph.append(line.strip())
        i += 1

    flush_paragraph()
    close_list()
    close_table()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _render_help_page(*, title: str, markdown: str) -> HTMLResponse:
    body = _markdown_to_simple_html(markdown)
    branding = settings.site_branding()
    records_help = ""
    if branding["show_records_help"]:
        label = html.escape(branding["records_request"]["button_label"])
        records_help = (
            f' · <a href="/help/cora-talkgroup-identification">{label} talkgroups</a>'
        )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
      background: #0b1220;
      color: #e2e8f0;
      line-height: 1.55;
    }}
    .wrap {{ max-width: 48rem; margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }}
    .nav {{ margin-bottom: 1.25rem; font-size: 0.9rem; }}
    .nav a {{ color: #60a5fa; }}
    h1 {{ font-size: 1.6rem; margin: 0 0 0.75rem; }}
    h2 {{ font-size: 1.2rem; margin: 1.75rem 0 0.6rem; color: #f8fafc; }}
    h3 {{ font-size: 1.05rem; margin: 1.35rem 0 0.5rem; color: #f1f5f9; }}
    p, li {{ color: #cbd5e1; }}
    strong {{ color: #f8fafc; }}
    a {{ color: #60a5fa; }}
    hr {{ border: 0; border-top: 1px solid #334155; margin: 1.5rem 0; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.88em;
      background: #1e293b;
      padding: 0.1rem 0.35rem;
      border-radius: 0.3rem;
    }}
    pre {{
      background: #111827;
      border: 1px solid #334155;
      border-radius: 0.65rem;
      padding: 0.85rem 1rem;
      overflow: auto;
    }}
    pre code {{ background: transparent; padding: 0; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 0.75rem 0 1rem;
      font-size: 0.92rem;
    }}
    th, td {{
      border: 1px solid #334155;
      padding: 0.45rem 0.6rem;
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: #1e293b; color: #f8fafc; }}
    ul {{ padding-left: 1.25rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <p class="nav"><a href="/">← Back to dashboard</a> · <a href="/help/faq-encrypted-activity">Encrypted FAQ</a>{records_help}</p>
    {body}
  </div>
</body>
</html>"""
    return HTMLResponse(content=page)


@app.get("/help/{slug}", response_class=HTMLResponse)
async def help_doc(slug: str) -> HTMLResponse:
    """Serve a markdown file from the mounted docs/ directory as HTML."""
    path = _docs_markdown_path(slug)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read document: {exc}",
        ) from exc
    title_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else slug
    return _render_help_page(title=title, markdown=markdown)


@app.get("/faq/encrypted", response_class=HTMLResponse)
async def faq_encrypted_activity() -> HTMLResponse:
    """Alias for the encrypted-activity FAQ."""
    return await help_doc("faq-encrypted-activity")


@app.get("/calls/{call_id}")
async def get_call_by_id(call_id: int) -> dict[str, Any]:
    call = get_call(call_id)
    if not call:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    return call


@app.get("/calls/{call_id}/audio")
async def get_call_audio(call_id: int) -> FileResponse:
    call = get_call(call_id)
    if not call:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")

    if call.get("status") in NON_PLAYABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No audio for skipped activity",
        )

    wav_path = Path(call["wav_path"])
    try:
        wav_path.resolve().relative_to(settings.audio_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")

    if not wav_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")

    return FileResponse(
        wav_path,
        media_type=audio_media_type(wav_path),
        content_disposition_type="inline",
    )


CATEGORY_EMOJI_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"police|sheriff|law|campus police", re.I), "🚓"),
    (re.compile(r"fire|ems|medic|health", re.I), "🚒"),
    (re.compile(r"detention|corrections|jail", re.I), "🏛️"),
    (re.compile(r"school|education", re.I), "🏫"),
    (re.compile(r"mutual aid|interop|public safety", re.I), "📡"),
    (re.compile(r"public works|street|traffic|parking|waste|water|utilities|solid waste", re.I), "🛠️"),
    (re.compile(r"parks|recreation|library|museum|amphitheatre|coliseum|convention|animal", re.I), "🏙️"),
    (re.compile(r"radio shop", re.I), "📻"),
)


def _category_emoji(category: str) -> str:
    if not category:
        return "📁"
    for pattern, emoji in CATEGORY_EMOJI_RULES:
        if pattern.search(category):
            return emoji
    return "📁"


def _talkgroup_category_map() -> dict[int, str]:
    catalog = load_talkgroups_catalog(settings.data_dir / "talk_groups.csv")
    return {
        int(item["talkgroup"]): str(item.get("category") or "")
        for item in catalog
        if item.get("talkgroup") is not None
    }


def _parse_metadata(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("metadata_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _enrich_calls_with_category(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories = _talkgroup_category_map()
    enriched: list[dict[str, Any]] = []
    for call in calls:
        item = dict(call)
        talkgroup = item.get("talkgroup")
        category = ""
        if talkgroup is not None:
            try:
                category = categories.get(int(talkgroup), "")
            except (TypeError, ValueError):
                category = ""
        item["category"] = category
        item["category_emoji"] = _category_emoji(category)

        metadata = _parse_metadata(item)
        call_type = metadata.get("call_type")
        target = metadata.get("target")
        # Backfill addressing for older encrypted/unknown rows.
        if not call_type and item.get("status") in NON_PLAYABLE_STATUSES and talkgroup is not None:
            addressing = classify_call_addressing(
                talkgroup=int(talkgroup),
                src=item.get("src"),
            )
            call_type = addressing["call_type"]
            target = addressing["target"]
            item["addressing_confidence"] = addressing.get("addressing_confidence")
        else:
            item["addressing_confidence"] = metadata.get("addressing_confidence")
        item["call_type"] = call_type or "group"
        item["target"] = target
        enriched.append(item)
    return enriched


def _transcript_alert_emojis(transcript: str) -> str:
    return transcript_alert_emojis(transcript)


def _format_transcript_cell(transcript: str, status: str) -> str:
    if transcript:
        preview = transcript[:180] + ("..." if len(transcript) > 180 else "")
        return html.escape(preview)
    if status == "encrypted":
        return (
            "<em class='encrypted-note' "
            "title='No voice was recorded; only control-channel metadata is stored'>"
            "no transcript — encrypted voice not recorded</em>"
        )
    if status == "unknown_talkgroup":
        return "<em class='unknown-tg-note'>not in CSV</em>"
    if status in ("pending", "processing"):
        return "<em>pending</em>"
    return ""


def _format_alerts_cell(transcript: str) -> str:
    emojis = _transcript_alert_emojis(transcript)
    if emojis:
        return f'<span class="alerts" title="Keyword alert">{emojis}</span>'
    return ""


def _format_src_cell(src: Any) -> str:
    if src is None or src == -1 or src == 0:
        return "—"
    return html.escape(str(src))


def _format_call_type_cell(call: dict[str, Any]) -> str:
    call_type = str(call.get("call_type") or "group")
    target = call.get("target")
    if call_type == "unit_to_unit":
        target_html = html.escape(str(target)) if target not in (None, "", 0, -1) else "?"
        return (
            f'<span class="call-type u2u" title="Likely unit-to-unit / private call">'
            f"📡→{target_html}</span>"
        )
    if call_type == "unknown":
        return '<span class="call-type unknown" title="Addressing unknown">?</span>'
    return '<span class="call-type group" title="Group talkgroup call">TG</span>'


def _format_category_cell(category: str, emoji: str) -> str:
    label = html.escape(category) if category else "<em>—</em>"
    title = html.escape(category or "Unknown category")
    return (
        f'<span class="category" title="{title}">'
        f'<span class="category-emoji">{emoji}</span>'
        f'<span class="category-label">{label}</span>'
        f"</span>"
    )


def _format_rid(value: Any) -> str:
    if value is None or value in (-1, 0, ""):
        return "unknown"
    return str(value)


def _records_request_text(call: dict[str, Any]) -> str:
    cfg = settings.records_request_config()
    created = str(call.get("created_at") or "unknown")
    system = str(call.get("system_name") or "unknown")
    talkgroup = call.get("talkgroup")
    tag = str(call.get("talkgroup_tag") or "").strip()
    category = str(call.get("category") or "").strip()
    src = _format_rid(call.get("src"))
    call_type = str(call.get("call_type") or "group")
    target = call.get("target")
    freq = call.get("freq")
    confidence = str(call.get("addressing_confidence") or "").strip()

    if call_type == "unit_to_unit":
        addressing = (
            f"Unit-to-unit / private call (inferred)\n"
            f"- Source RID: {src}\n"
            f"- Target RID: {_format_rid(target)}"
        )
    elif call_type == "unknown":
        addressing = (
            f"Addressing unknown\n"
            f"- Source RID: {src}\n"
            f"- Reported ID (TG field): {talkgroup if talkgroup is not None else 'unknown'}"
        )
    else:
        tg_label = str(talkgroup) if talkgroup is not None else "unknown"
        if tag:
            tg_label = f"{tg_label} ({tag})"
        addressing = (
            f"Group talkgroup call\n"
            f"- Talkgroup: {tg_label}\n"
            f"- Source RID: {src}"
        )

    freq_line = f"- Frequency: {freq} MHz" if freq not in (None, "") else "- Frequency: unknown"
    category_line = f"- Category: {category}" if category else "- Category: unknown"
    confidence_line = (
        f"- Addressing confidence: {confidence}" if confidence else None
    )

    lines = [
        cfg["title"],
        "",
        "Please produce audio (and chain-of-custody export) for the following encrypted P25 traffic:",
        "",
        f"- Our internal record ID (not an agency logger ID): {call.get('id', 'unknown')}",
        f"- Observed at (UTC): {created}",
        f"- System / site: {system}",
        freq_line,
        category_line,
        "- Encrypted: yes (no clear audio available from scanner capture)",
        "",
        addressing,
    ]
    if confidence_line:
        lines.append(confidence_line)
    lines.extend(
        [
            "",
            "Related to case/CAD #: ________________",
            f"Requestor / {cfg['contact_label']} contact: ________________",
            "Preferred format: WAV or vendor logger export with metadata sheet",
            "",
            "Notes: This request identifies the call from publicly observable trunking metadata",
            "(time, system/site, talkgroup or RIDs, frequency). Our internal record ID is for",
            "our tracking only and will not match agency CAD/logger identifiers.",
            "Decryption keys are not included as part of this request and must remain under agency control.",
        ]
    )
    return "\n".join(lines)


def _audio_player(call: dict[str, Any]) -> str:
    status = call.get("status")
    if status == "encrypted":
        cfg = settings.records_request_config()
        if not cfg["enabled"]:
            return (
                '<span class="encrypted-indicator" title="Encrypted — not recorded">🔒</span>'
            )
        request_text = html.escape(_records_request_text(call), quote=True)
        label = html.escape(cfg["button_label"])
        title = html.escape(f"Copy {cfg['button_label']} request to clipboard")
        return (
            '<span class="encrypted-actions">'
            '<span class="encrypted-indicator" title="Encrypted — not recorded">🔒</span>'
            f'<button type="button" class="copy-request" data-call-id="{call["id"]}" '
            f'data-request-text="{request_text}" '
            f'title="{title}">📋 {label}</button>'
            "</span>"
        )
    if status == "unknown_talkgroup":
        return (
            '<span class="unknown-tg-indicator" title="Talk group not in talk_groups.csv — add to enable recording">📋</span>'
        )
    call_id = call["id"]
    return (
        f'<audio controls preload="none" class="audio-player" '
        f'data-call-id="{call_id}" src="/calls/{call_id}/audio"></audio>'
    )


def _render_call_rows(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return '<tr><td colspan="13"><em>No calls yet</em></td></tr>'

    rows = []
    for call in _enrich_calls_with_category(calls):
        transcript = call.get("transcript") or ""
        preview_html = _format_transcript_cell(transcript, call["status"])
        alerts_html = _format_alerts_cell(transcript)
        tag_html = html.escape(str(call.get("talkgroup_tag") or "")) or "<em>—</em>"
        category_html = _format_category_cell(
            str(call.get("category") or ""),
            str(call.get("category_emoji") or "📁"),
        )
        rows.append(
            f"""
            <tr>
              <td>{call['id']}</td>
              <td>{html.escape(str(call.get('created_at') or ''))}</td>
              <td>{html.escape(str(call.get('system_name') or ''))}</td>
              <td>{html.escape(str(call.get('talkgroup') or ''))}</td>
              <td>{_format_call_type_cell(call)}</td>
              <td title="Radio / unit ID">{_format_src_cell(call.get('src'))}</td>
              <td>{html.escape(str(call.get('call_length') or ''))}</td>
              <td><span class="status {html.escape(call['status'])}">{html.escape(call['status'])}</span></td>
              <td>{_audio_player(call)}</td>
              <td class="tag">{tag_html}</td>
              <td class="category-cell">{category_html}</td>
              <td class="alerts">{alerts_html}</td>
              <td class="transcript">{preview_html}</td>
            </tr>
            """
        )
    return "".join(rows)


def _format_site_notice_html(notice: str) -> str:
    """Escape site notice; bold the standard lead sentence when present."""
    lead = "No decryption of encrypted communications takes place."
    text = (notice or "").strip()
    if text.startswith(lead):
        rest = text[len(lead) :].lstrip()
        return f"<strong>{html.escape(lead)}</strong> {html.escape(rest)}"
    return html.escape(text)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    calls = list_calls(limit=100)
    counts = count_calls_by_status()
    branding = settings.site_branding()
    site_title = html.escape(branding["title"])
    site_subtitle = html.escape(branding["subtitle"])
    notice_html = _format_site_notice_html(branding["notice"])

    # Local page markup — do not name this `html` (shadows the stdlib module).
    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{site_title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
    .page-header {{
      display: flex;
      gap: 1.5rem;
      align-items: flex-start;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-bottom: 1.5rem;
    }}
    .page-header-main {{ flex: 1 1 18rem; min-width: 0; }}
    h1 {{ margin: 0 0 0.25rem; }}
    .meta {{ color: #94a3b8; margin: 0; }}
    .notice {{
      flex: 1 1 22rem;
      max-width: 40rem;
      color: #cbd5e1;
      background: #1e293b;
      border-left: 3px solid #64748b;
      padding: 0.65rem 0.85rem;
      margin: 0;
      font-size: 0.82rem;
      line-height: 1.4;
    }}
    .notice strong {{ color: #e2e8f0; font-weight: 600; }}
    .notice a {{ color: #93c5fd; text-decoration: underline; text-underline-offset: 2px; }}
    .notice a:hover {{ color: #bfdbfe; }}
    .toolbar {{ display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap; margin-bottom: 1.25rem; }}
    .filter-block {{ flex: 1 1 22rem; max-width: 34rem; }}
    .filter-block label {{ display: block; color: #94a3b8; font-size: 0.8rem; margin-bottom: 0.35rem; text-transform: uppercase; letter-spacing: 0.04em; }}
    .combobox {{ position: relative; }}
    .combobox-input-wrap {{ display: flex; align-items: center; gap: 0.5rem; background: #1e293b; border: 1px solid #334155; border-radius: 0.65rem; padding: 0.45rem 0.65rem; }}
    .combobox-input-wrap:focus-within {{ border-color: #60a5fa; box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.2); }}
    .combobox-input-wrap input {{ flex: 1; background: transparent; border: 0; color: #e2e8f0; font: inherit; outline: none; min-width: 0; }}
    .combobox-input-wrap input::placeholder {{ color: #64748b; }}
    .filter-chip {{ display: inline-flex; align-items: center; gap: 0.35rem; background: #0f766e; color: #ccfbf1; border-radius: 999px; padding: 0.15rem 0.55rem; font-size: 0.8rem; white-space: nowrap; }}
    .filter-chip button {{ background: transparent; border: 0; color: inherit; cursor: pointer; font-size: 1rem; line-height: 1; padding: 0; }}
    .combobox-menu {{ position: absolute; z-index: 20; top: calc(100% + 0.35rem); left: 0; right: 0; max-height: 16rem; overflow: auto; background: #111827; border: 1px solid #334155; border-radius: 0.65rem; box-shadow: 0 16px 40px rgba(0, 0, 0, 0.35); }}
    .combobox-menu[hidden] {{ display: none; }}
    .combobox-option {{ display: block; width: 100%; text-align: left; background: transparent; border: 0; color: #e2e8f0; padding: 0.65rem 0.8rem; cursor: pointer; }}
    .combobox-option:hover, .combobox-option.active {{ background: #1e293b; }}
    .combobox-option strong {{ color: #f8fafc; }}
    .combobox-option span {{ display: block; color: #94a3b8; font-size: 0.8rem; margin-top: 0.15rem; }}
    .quick-filters {{
      display: flex;
      gap: 0.45rem;
      flex-wrap: wrap;
      align-content: flex-start;
      max-height: 9.5rem;
      overflow-y: auto;
      margin-bottom: 1.25rem;
      padding-right: 0.25rem;
      scrollbar-width: thin;
      scrollbar-color: #475569 transparent;
    }}
    .quick-filters::-webkit-scrollbar {{ width: 6px; }}
    .quick-filters::-webkit-scrollbar-thumb {{ background: #475569; border-radius: 999px; }}
    .quick-filter {{ background: #1e293b; border: 1px solid #334155; color: #cbd5e1; border-radius: 999px; padding: 0.35rem 0.75rem; font-size: 0.82rem; cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s, background 0.15s; }}
    .quick-filter:hover {{ border-color: #60a5fa; color: #f8fafc; }}
    .quick-filter.active {{ background: #1d4ed8; border-color: #60a5fa; color: #eff6ff; }}
    .quick-filter.activity {{
      border-color: #fbbf24;
      color: #fef3c7;
      box-shadow: 0 0 0 2px rgba(251, 191, 36, 0.35);
      animation: tg-activity-pulse 1.4s ease-in-out infinite;
    }}
    .quick-filter.activity.active {{
      border-color: #fbbf24;
      box-shadow: 0 0 0 2px rgba(251, 191, 36, 0.45);
    }}
    @keyframes tg-activity-pulse {{
      0%, 100% {{ box-shadow: 0 0 0 2px rgba(251, 191, 36, 0.25); }}
      50% {{ box-shadow: 0 0 0 4px rgba(251, 191, 36, 0.55); }}
    }}
    .filter-summary {{ color: #94a3b8; font-size: 0.9rem; padding-bottom: 0.2rem; }}
    .range-search {{
      display: flex;
      gap: 0.75rem;
      align-items: flex-end;
      flex-wrap: wrap;
      margin: 0 0 1.1rem;
      padding: 0.75rem 0.9rem;
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 0.65rem;
    }}
    .range-search .filter-block {{ flex: 0 1 auto; max-width: none; }}
    .range-search input[type="datetime-local"],
    .range-search input[type="search"] {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 0.5rem;
      color: #e2e8f0;
      font: inherit;
      padding: 0.45rem 0.6rem;
      min-width: 12.5rem;
    }}
    .range-search input[type="search"] {{
      min-width: 16rem;
      width: min(22rem, 100%);
    }}
    .range-search input[type="datetime-local"]:focus,
    .range-search input[type="search"]:focus {{
      outline: none;
      border-color: #60a5fa;
      box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.2);
    }}
    .range-actions {{ display: flex; gap: 0.45rem; align-items: center; padding-bottom: 0.05rem; }}
    .range-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      color: #cbd5e1;
      font-size: 0.88rem;
      cursor: pointer;
      user-select: none;
      padding-bottom: 0.35rem;
      white-space: nowrap;
    }}
    .range-toggle input {{ accent-color: #fbbf24; }}
    .range-actions button {{
      background: #1d4ed8;
      border: 1px solid #60a5fa;
      color: #eff6ff;
      border-radius: 0.5rem;
      padding: 0.45rem 0.85rem;
      font: inherit;
      cursor: pointer;
    }}
    .range-actions button.secondary {{
      background: #334155;
      border-color: #475569;
      color: #e2e8f0;
    }}
    .range-actions button:hover {{ filter: brightness(1.08); }}
    .range-hint {{ color: #64748b; font-size: 0.78rem; padding-bottom: 0.35rem; }}
    .range-active-note {{
      color: #fbbf24;
      font-size: 0.82rem;
      padding-bottom: 0.3rem;
    }}
    .view-toggles {{ display: flex; flex-wrap: wrap; gap: 0.85rem 1.1rem; align-items: center; }}
    .auto-play-toggle {{ display: inline-flex; align-items: center; gap: 0.45rem; color: #cbd5e1; font-size: 0.9rem; cursor: pointer; user-select: none; }}
    .auto-play-toggle input {{ accent-color: #60a5fa; }}
    .system-filters {{
      display: flex;
      gap: 0.45rem;
      flex-wrap: wrap;
      align-items: center;
      margin: 0 0 0.85rem;
    }}
    .system-filters-label {{
      color: #94a3b8;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-right: 0.15rem;
    }}
    .system-filter {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: #1e293b;
      border: 1px solid #334155;
      color: #cbd5e1;
      border-radius: 999px;
      padding: 0.35rem 0.8rem;
      font-size: 0.82rem;
      cursor: pointer;
    }}
    .system-filter:hover {{ border-color: #60a5fa; color: #f8fafc; }}
    .system-filter.active {{ background: #0f766e; border-color: #2dd4bf; color: #ccfbf1; }}
    .system-filter .dot {{
      width: 0.55rem;
      height: 0.55rem;
      border-radius: 999px;
      background: #64748b;
      flex: 0 0 auto;
    }}
    .system-filter .dot.active {{ background: #4ade80; box-shadow: 0 0 0 3px rgba(74, 222, 128, 0.2); }}
    .system-filter .count {{ opacity: 0.7; }}
    .system-filters-note {{ color: #64748b; font-size: 0.78rem; }}
    tr.playing {{ background: #1e3a5f !important; }}
    tr.playing td {{ border-bottom-color: #2563eb; }}
    .stats {{ display: flex; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap; align-items: stretch; }}
    .stat {{
      background: #1e293b;
      padding: 0.85rem 1.1rem;
      border-radius: 0.5rem;
      min-width: 7.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      gap: 0.35rem;
    }}
    .stat strong {{
      font-size: 0.75rem;
      font-weight: 600;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .stat [data-status] {{
      font-size: 2rem;
      font-weight: 700;
      line-height: 1.1;
      color: #f8fafc;
      font-variant-numeric: tabular-nums;
    }}
    .stats-label {{
      flex: 0 0 100%;
      color: #94a3b8;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin: -0.35rem 0 0.15rem;
    }}
    .anomaly-badge {{
      background: #1e293b;
      padding: 0.85rem 1.1rem;
      border-radius: 0.5rem;
      min-width: 9rem;
      max-width: 16rem;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      border: 1px solid #334155;
    }}
    .anomaly-badge.active {{ border-color: #f59e0b; background: #1c1917; }}
    .anomaly-badge.high {{ border-color: #f87171; }}
    .anomaly-badge strong {{
      font-size: 0.75rem;
      font-weight: 600;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .anomaly-badge.active strong {{ color: #fbbf24; }}
    .anomaly-badge.high strong {{ color: #fca5a5; }}
    .anomaly-summary {{
      font-size: 0.95rem;
      font-weight: 700;
      color: #e2e8f0;
      font-variant-numeric: tabular-nums;
    }}
    .anomaly-badge:not(.active) .anomaly-summary {{ color: #64748b; font-weight: 500; font-size: 0.85rem; }}
    .anomaly-list {{ display: flex; flex-direction: column; gap: 0.2rem; margin-top: 0.15rem; }}
    .anomaly-item {{
      appearance: none;
      background: transparent;
      border: 0;
      color: #cbd5e1;
      font: inherit;
      font-size: 0.72rem;
      text-align: left;
      padding: 0.15rem 0;
      cursor: pointer;
      line-height: 1.25;
    }}
    .anomaly-item:hover {{ color: #f8fafc; }}
    .anomaly-item .conf {{
      display: inline-block;
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      margin-right: 0.35rem;
      color: #94a3b8;
    }}
    .anomaly-item .conf.high {{ color: #f87171; }}
    .anomaly-item .conf.medium {{ color: #fbbf24; }}
    .anomaly-item .conf.low {{ color: #64748b; }}
    .activity-panel {{ flex: 2 1 22rem; min-width: 18rem; background: #1e293b; padding: 0.75rem 1rem; border-radius: 0.5rem; }}
    .activity-panel strong {{ display: block; margin-bottom: 0.5rem; font-size: 0.85rem; color: #e2e8f0; }}
    .activity-chart svg {{ width: 100%; height: auto; min-height: 160px; display: block; }}
    .activity-empty {{ color: #64748b; font-size: 0.85rem; padding: 1rem 0; }}
    .outcome-panel {{ flex: 1.4 1 16rem; min-width: 14rem; background: #1e293b; padding: 0.75rem 1rem; border-radius: 0.5rem; }}
    .outcome-panel strong {{ display: block; margin-bottom: 0.5rem; font-size: 0.85rem; color: #e2e8f0; }}
    .outcome-chart svg {{ width: 100%; height: auto; display: block; }}
    .outcome-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem 0.85rem;
      margin-top: 0.45rem;
      font-size: 0.72rem;
      color: #94a3b8;
    }}
    .outcome-legend span {{ display: inline-flex; align-items: center; gap: 0.3rem; }}
    .outcome-swatch {{
      width: 0.55rem;
      height: 0.55rem;
      border-radius: 2px;
      display: inline-block;
    }}
    .outcome-swatch.encrypted {{ background: #a78bfa; }}
    .outcome-swatch.transcribed {{ background: #4ade80; }}
    .outcome-swatch.failed {{ background: #f87171; }}
    .district-panel {{
      flex: 2 1 22rem;
      min-width: 18rem;
      background: #1e293b;
      padding: 0.75rem 1rem;
      border-radius: 0.5rem;
    }}
    .district-panel strong {{ display: block; margin-bottom: 0.35rem; font-size: 0.85rem; color: #e2e8f0; }}
    .district-meta {{ color: #64748b; font-size: 0.72rem; margin-bottom: 0.45rem; }}
    .district-map svg {{ width: 100%; height: auto; display: block; background: #0f172a; border-radius: 0.35rem; }}
    .district-map path {{
      stroke: #0f172a;
      stroke-width: 0.8;
      cursor: pointer;
      transition: fill 0.15s, stroke 0.15s;
    }}
    .district-map path:hover {{ stroke: #f8fafc; stroke-width: 1.4; }}
    .district-map path.active {{ stroke: #60a5fa; stroke-width: 1.8; }}
    .district-map path.unmapped {{ cursor: default; }}
    .district-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem 0.75rem;
      margin-top: 0.45rem;
      font-size: 0.72rem;
      color: #94a3b8;
    }}
    .district-legend span {{ display: inline-flex; align-items: center; gap: 0.3rem; }}
    .district-swatch {{
      width: 0.7rem;
      height: 0.55rem;
      border-radius: 2px;
      display: inline-block;
    }}
    table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 0.5rem; overflow: hidden; }}
    th, td {{ padding: 0.65rem 0.75rem; border-bottom: 1px solid #334155; text-align: left; vertical-align: top; }}
    th {{ background: #111827; }}
    th.sortable {{ cursor: pointer; user-select: none; }}
    th.sortable:hover {{ background: #1e293b; }}
    th.sorted {{ color: #f8fafc; }}
    .sort-indicator {{ color: #60a5fa; font-size: 0.7rem; }}
    .status.completed {{ color: #4ade80; }}
    .status.pending, .status.processing {{ color: #fbbf24; }}
    .status.failed {{ color: #f87171; }}
    .status.encrypted {{ color: #c4b5fd; }}
    .status.unknown_talkgroup {{ color: #fb923c; }}
    .encrypted-indicator {{ font-size: 1.1rem; }}
    .unknown-tg-indicator {{ font-size: 1.1rem; }}
    .encrypted-actions {{ display: inline-flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }}
    .copy-request {{
      background: #312e81;
      border: 1px solid #6366f1;
      color: #e0e7ff;
      border-radius: 999px;
      padding: 0.2rem 0.55rem;
      font-size: 0.72rem;
      cursor: pointer;
      white-space: nowrap;
    }}
    .copy-request:hover {{ background: #3730a3; color: #fff; }}
    .copy-request.copied {{ background: #065f46; border-color: #34d399; color: #d1fae5; }}
    .encrypted-note {{ color: #a78bfa; }}
    .unknown-tg-note {{ color: #fb923c; }}
    .tag {{ color: #94a3b8; font-size: 0.85rem; white-space: nowrap; max-width: 10rem; }}
    .category {{ display: inline-flex; align-items: center; gap: 0.35rem; white-space: nowrap; max-width: 12rem; }}
    .category-emoji {{ font-size: 1.05rem; line-height: 1; }}
    .category-label {{ color: #94a3b8; font-size: 0.82rem; overflow: hidden; text-overflow: ellipsis; }}
    .call-type {{ font-size: 0.78rem; font-weight: 600; white-space: nowrap; }}
    .call-type.group {{ color: #94a3b8; }}
    .call-type.u2u {{ color: #fbbf24; }}
    .call-type.unknown {{ color: #64748b; }}
    .alerts {{ font-size: 1.1rem; text-align: center; white-space: nowrap; width: 3.5rem; }}
    .transcript {{ max-width: 40rem; }}
    .audio-player {{ width: 14rem; max-width: 100%; height: 2rem; }}
    a {{ color: #60a5fa; }}
  </style>
</head>
<body>
  <div class="page-header">
    <div class="page-header-main">
      <h1>{site_title}</h1>
      <p class="meta">{site_subtitle} · Backend: <span id="transcription-backend">{settings.transcription_backend.value}</span></p>
    </div>
    <p class="notice">{notice_html} <a href="/faq/encrypted">How encrypted activity is shown (FAQ)</a></p>
  </div>
  <div class="stats" id="stats">
    <div class="stats-label">Queue · all talk groups</div>
    <div class="stat"><strong>Pending</strong><span data-status="pending">{counts.get('pending', 0)}</span></div>
    <div class="stat"><strong>Processing</strong><span data-status="processing">{counts.get('processing', 0)}</span></div>
    <div class="stat"><strong>Completed</strong><span data-status="completed">{counts.get('completed', 0)}</span></div>
    <div class="stat"><strong>Failed</strong><span data-status="failed">{counts.get('failed', 0)}</span></div>
    <div class="stat"><strong>Encrypted</strong><span data-status="encrypted">{counts.get('encrypted', 0)}</span></div>
    <div class="stat"><strong>Unknown TG</strong><span data-status="unknown_talkgroup">{counts.get('unknown_talkgroup', 0)}</span></div>
    <div class="anomaly-badge" id="anomaly-badge" title="Encrypted tempo vs weekday/hour baseline">
      <strong>Encrypted tempo</strong>
      <span class="anomaly-summary" id="anomaly-summary">checking…</span>
      <div class="anomaly-list" id="anomaly-list"></div>
    </div>
    <div class="activity-panel" id="activity-panel">
      <strong id="activity-title">Talk group activity (6h)</strong>
      <div class="activity-chart" id="activity-chart"></div>
    </div>
    <div class="outcome-panel" id="outcome-panel">
      <strong id="outcome-title">Outcomes by system</strong>
      <div class="outcome-chart" id="outcome-chart"></div>
      <div class="outcome-legend">
        <span><i class="outcome-swatch encrypted"></i>Encrypted</span>
        <span><i class="outcome-swatch transcribed"></i>Transcribed</span>
        <span><i class="outcome-swatch failed"></i>Failed</span>
      </div>
    </div>
    <div class="district-panel" id="district-panel">
      <strong>District activity</strong>
      <div class="district-meta" id="district-meta">Loading map…</div>
      <div class="district-map" id="district-map"></div>
      <div class="district-legend">
        <span><i class="district-swatch" style="background:#1e3a5f"></i>Quiet</span>
        <span><i class="district-swatch" style="background:#2563eb"></i>Busy</span>
        <span><i class="district-swatch" style="background:#f59e0b"></i>Hottest</span>
        <span>Click a district to filter</span>
      </div>
    </div>
  </div>
  <div class="system-filters" id="system-filters" aria-label="Trunk Recorder systems"></div>
  <div class="toolbar">
    <div class="filter-block">
      <label for="tg-search">Talk group</label>
      <div class="combobox" id="tg-combobox">
        <div class="combobox-input-wrap">
          <span class="filter-chip" id="tg-chip" hidden>
            <span id="tg-chip-label"></span>
            <button type="button" id="tg-clear" title="Clear filter">×</button>
          </span>
          <input id="tg-search" type="search" placeholder="Search by TG, tag, or description…" autocomplete="off" />
        </div>
        <div class="combobox-menu" id="tg-menu" hidden></div>
      </div>
    </div>
    <div class="filter-summary" id="filter-summary"></div>
    <div class="view-toggles">
      <label class="auto-play-toggle">
        <input type="checkbox" id="hide-encrypted" />
        Hide encrypted
      </label>
      <label class="auto-play-toggle">
        <input type="checkbox" id="hide-unknown-tg" />
        Hide not in CSV
      </label>
      <label class="auto-play-toggle">
        <input type="checkbox" id="auto-play-enabled" checked />
        Auto-play selected talk group
      </label>
    </div>
    <div class="filter-summary" id="auto-play-status"></div>
  </div>
  <div class="range-search" id="range-search">
    <div class="filter-block">
      <label for="range-from">From</label>
      <input id="range-from" type="datetime-local" />
    </div>
    <div class="filter-block">
      <label for="range-to">To</label>
      <input id="range-to" type="datetime-local" />
    </div>
    <div class="filter-block">
      <label for="transcript-q">Transcript</label>
      <input id="transcript-q" type="search" placeholder="e.g. shots fired, 10-50…" autocomplete="off" />
    </div>
    <label class="range-toggle" title="Show keyword-alert calls across all talk groups (or the selected TG)">
      <input type="checkbox" id="alerts-only" />
      Alerts only
    </label>
    <div class="range-actions">
      <button type="button" id="range-search-btn">Search</button>
      <button type="button" class="secondary" id="range-clear-btn">Clear</button>
    </div>
    <div class="range-hint">Uses the talk group / system filters above when set. Live updates pause while a search is active.</div>
    <div class="range-active-note" id="range-active-note" hidden></div>
  </div>
  <div class="quick-filters" id="quick-filters"></div>
  <table>
    <thead>
      <tr>
        <th class="sortable" data-sort="id">ID<span class="sort-indicator"></span></th>
        <th class="sortable sorted" data-sort="created_at">Created<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="system_name">System<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="talkgroup">TG<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="call_type">Type<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="src">Src<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="call_length">Length<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="status">Status<span class="sort-indicator"></span></th>
        <th>Audio</th>
        <th class="sortable" data-sort="talkgroup_tag">Tag<span class="sort-indicator"></span></th>
        <th class="sortable" data-sort="category">Category<span class="sort-indicator"></span></th>
        <th class="alerts-col">Alerts</th>
        <th class="sortable" data-sort="transcript">Transcript<span class="sort-indicator"></span></th>
      </tr>
    </thead>
    <tbody id="calls-body">
      {_render_call_rows(calls)}
    </tbody>
  </table>
  <p class="meta">Updated <span id="last-updated">just now</span> · API docs: <a href="/docs">/docs</a> · FAQ: <a href="/faq/encrypted">encrypted activity</a> · Health: <a href="/health">/health</a></p>
  <script>
    const POLL_MS = 5000;
    const QUEUE_POLL_MS = 1000;
    const CALLS_LIMIT = 150;
    const recordsRequest = {json.dumps(branding["records_request"])};
    const siteBranding = {json.dumps({
        "title": branding["title"],
        "subtitle": branding["subtitle"],
        "show_records_help": branding["show_records_help"],
    })};
    let callsData = [];
    let talkgroupsCatalog = [];
    let systemsCatalog = [];
    let selectedTalkgroup = null;
    let selectedSystem = null;
    let rangeFrom = null;
    let rangeTo = null;
    let transcriptQuery = null;
    let rangeSearchActive = false;
    let activeMenuIndex = -1;
    let sortKey = "created_at";
    let sortDir = "desc";
    let autoPlayEnabled = true;
    let hideEncrypted = false;
    let hideUnknownTg = false;
    let alertsOnly = false;
    let dashboardAbort = null;
    let callsAbort = null;
    let initialLoadDone = false;
    let knownCallIds = new Set();
    let knownTgActivityAt = new Map();
    let activityTalkgroups = new Map();
    const ACTIVITY_HIGHLIGHT_MS = 45000;
    let globalQueue = {{
      pending: {counts.get('pending', 0)},
      processing: {counts.get('processing', 0)},
      completed: {counts.get('completed', 0)},
      failed: {counts.get('failed', 0)},
      encrypted: {counts.get('encrypted', 0)},
      unknown_talkgroup: {counts.get('unknown_talkgroup', 0)},
    }};
    const QUEUE_STATUSES = [
      "pending",
      "processing",
      "completed",
      "failed",
      "encrypted",
      "unknown_talkgroup",
    ];
    let playQueue = [];
    let isPlayingQueue = false;
    let currentAutoPlayId = null;
    let audioUnlocked = false;

    const tgSearch = document.getElementById("tg-search");
    const tgMenu = document.getElementById("tg-menu");
    const tgChip = document.getElementById("tg-chip");
    const tgChipLabel = document.getElementById("tg-chip-label");
    const tgClear = document.getElementById("tg-clear");
    const quickFilters = document.getElementById("quick-filters");
    const systemFilters = document.getElementById("system-filters");
    const filterSummary = document.getElementById("filter-summary");
    const hideEncryptedToggle = document.getElementById("hide-encrypted");
    const hideUnknownTgToggle = document.getElementById("hide-unknown-tg");
    const alertsOnlyToggle = document.getElementById("alerts-only");
    const autoPlayToggle = document.getElementById("auto-play-enabled");
    const autoPlayStatus = document.getElementById("auto-play-status");

    function esc(text) {{
      const el = document.createElement("div");
      el.textContent = text ?? "";
      return el.innerHTML;
    }}

    function transcriptAlertEmojis(transcript) {{
      if (!transcript) return "";
      const rules = [
        {{ pattern: /\\b(working fire|structure fire|building fire|house fire|garage fire|vehicle fire|car fire|brush fire|wildfire|grass fire|smoke showing|flames|fully engulfed|reported fire|confirmed fire|active fire|fire at|on fire|smoke)\\b/i, emoji: "🔥" }},
        {{ pattern: /\\b(mental health|psychiatric|psych|5150|suicidal|suicide|behavioral|crisis)\\b/i, emoji: "🧠" }},
        {{ pattern: /\\b(stabbing|stabbed|stab wound|knife wound)\\b/i, emoji: "🔪" }},
        {{ pattern: /\\b(gunshot|gun shot|shots fired|shooting|shooter|gsw)\\b/i, emoji: "💥" }},
        {{ pattern: /\\b(overdose|overdosing|overdosed|od(?:ing|ed)?|narcan|naloxone|fentanyl(?:\\s+overdose)?|heroin(?:\\s+overdose)?)\\b/i, emoji: "💉" }},
        {{ pattern: /\\b(doa|dead on arrival|deceased|fatality|(?<!non-)fatal(?:ity)?|code black|obvious death|confirmed death|time of death|passed away|pronounced dead|body found|found deceased)\\b/i, emoji: "☠️" }},
        {{ pattern: /\\b(trauma(?:tic)?(?:\\s+injur(?:y|ies))?|injur(?:y|ies|ed)|patient down|unconscious|cardiac arrest|chest pain|mvc|mva|motor vehicle accident|motor vehicle crash)\\b/i, emoji: "🩹" }},
      ];
      const seen = new Set();
      const emojis = [];
      for (const {{ pattern, emoji }} of rules) {{
        if (seen.has(emoji)) continue;
        if (pattern.test(transcript)) {{
          seen.add(emoji);
          emojis.push(emoji);
        }}
      }}
      return emojis.join("");
    }}

    function formatTranscriptCell(transcript, status) {{
      if (transcript) {{
        const preview = transcript.length > 180 ? transcript.slice(0, 180) + "..." : transcript;
        return esc(preview);
      }}
      if (status === "encrypted") {{
        return "<em class='encrypted-note' title='No voice was recorded; only control-channel metadata is stored'>no transcript — encrypted voice not recorded</em>";
      }}
      if (status === "unknown_talkgroup") {{
        return "<em class='unknown-tg-note'>not in CSV</em>";
      }}
      if (status === "pending" || status === "processing") {{
        return "<em>pending</em>";
      }}
      return "";
    }}

    function formatAlertsCell(transcript) {{
      const alerts = transcriptAlertEmojis(transcript);
      return alerts ? `<span class="alerts" title="Keyword alert">${{alerts}}</span>` : "";
    }}

    function updateBackendLabel(health) {{
      const el = document.getElementById("transcription-backend");
      if (!el || !health) return;
      let label = health.transcription_backend || "unknown";
      if (health.transcription_fallback) label += " (fallback enabled)";
      el.textContent = label;
    }}

    function renderTimelineChart(buckets, hours) {{
      if (!buckets.length) {{
        return `<div class="activity-empty">No calls in the last ${{hours}} hours</div>`;
      }}
      const width = 420;
      const height = 160;
      const padX = 18;
      const padY = 20;
      const max = Math.max(1, ...buckets.map((b) => b.count));
      const barGap = 4;
      const barWidth = Math.max(
        8,
        (width - padX * 2 - barGap * (buckets.length - 1)) / buckets.length,
      );
      const bars = buckets.map((bucket, index) => {{
        const barHeight = (bucket.count / max) * (height - padY * 2);
        const x = padX + index * (barWidth + barGap);
        const y = height - padY - barHeight;
        const label = bucket.bucket ? bucket.bucket.slice(11, 16) : "";
        return `
          <g>
            <rect x="${{x}}" y="${{y}}" width="${{barWidth}}" height="${{barHeight}}" rx="3" fill="#60a5fa">
              <title>${{bucket.bucket}}: ${{bucket.count}} event${{bucket.count === 1 ? "" : "s"}}</title>
            </rect>
            <text x="${{x + barWidth / 2}}" y="${{height - 4}}" fill="#94a3b8" font-size="10" text-anchor="middle">${{label}}</text>
          </g>`;
      }}).join("");
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="xMidYMid meet" aria-label="Hourly talk group activity">${{bars}}</svg>`;
    }}

    function renderTalkgroupChart(items, hours) {{
      if (!items.length) {{
        return `<div class="activity-empty">No calls in the last ${{hours}} hours</div>`;
      }}
      const width = 420;
      const rowHeight = 22;
      const height = Math.max(160, items.length * rowHeight + 20);
      const padX = 118;
      const max = Math.max(1, ...items.map((item) => item.count));
      const rows = items.map((item, index) => {{
        const y = 10 + index * rowHeight;
        const barWidth = ((width - padX - 40) * item.count) / max;
        const label = item.talkgroup_tag || `TG ${{item.talkgroup}}`;
        const shortLabel = label.length > 16 ? `${{label.slice(0, 15)}}…` : label;
        return `
          <g>
            <text x="0" y="${{y + 13}}" fill="#94a3b8" font-size="12">${{shortLabel}}</text>
            <rect x="${{padX}}" y="${{y + 2}}" width="${{barWidth}}" height="14" rx="3" fill="#38bdf8">
              <title>${{label}} (${{item.talkgroup}}): ${{item.count}} event${{item.count === 1 ? "" : "s"}}</title>
            </rect>
            <text x="${{padX + barWidth + 6}}" y="${{y + 13}}" fill="#cbd5e1" font-size="12">${{item.count}}</text>
          </g>`;
      }}).join("");
      return `<svg viewBox="0 0 ${{width}} ${{height}}" preserveAspectRatio="xMidYMid meet" aria-label="Talk group activity">${{rows}}</svg>`;
    }}

    function renderActivityChart(payload) {{
      const container = document.getElementById("activity-chart");
      const title = document.getElementById("activity-title");
      if (!container || !title || !payload) return;

      if (payload.mode === "timeline") {{
        const label = payload.talkgroup_tag || `TG ${{payload.talkgroup}}`;
        title.textContent = `${{label}} · last ${{payload.hours}}h`;
        container.innerHTML = renderTimelineChart(payload.buckets || [], payload.hours);
        return;
      }}

      title.textContent = `Top talk groups · last ${{payload.hours}}h`;
      container.innerHTML = renderTalkgroupChart(payload.talkgroups || [], payload.hours);
    }}

    async function loadActivityChart() {{
      const params = new URLSearchParams({{ hours: "6" }});
      if (selectedTalkgroup != null) {{
        params.set("talkgroup", String(selectedTalkgroup));
      }}
      try {{
        const response = await fetch(`/stats/activity?${{params}}`);
        if (!response.ok) return;
        renderActivityChart(await response.json());
      }} catch (err) {{
        console.error("Activity chart refresh failed", err);
      }}
    }}

    function renderSystemOutcomeChart(payload) {{
      const container = document.getElementById("outcome-chart");
      const title = document.getElementById("outcome-title");
      if (!container || !title || !payload) return;

      const systems = payload.systems || [];
      const hoursLabel = payload.hours ? ` · last ${{payload.hours}}h` : "";
      title.textContent = `Outcomes by system${{hoursLabel}}`;

      if (!systems.length) {{
        container.innerHTML = `<div class="activity-empty">No outcome data yet</div>`;
        return;
      }}

      const width = 300;
      const rowHeight = 28;
      const labelWidth = 58;
      const barX = labelWidth + 8;
      const barMax = width - barX - 36;
      const height = systems.length * rowHeight + 8;
      const colors = {{
        encrypted: "#a78bfa",
        transcribed: "#4ade80",
        failed: "#f87171",
      }};

      const rows = systems.map((system, index) => {{
        const y = 4 + index * rowHeight;
        const total = Math.max(1, system.total || 0);
        const segments = [
          ["encrypted", system.encrypted || 0],
          ["transcribed", system.transcribed || 0],
          ["failed", system.failed || 0],
        ];
        let x = barX;
        const rects = segments.map(([key, count]) => {{
          const w = (count / total) * barMax;
          const rect = `<rect x="${{x}}" y="${{y + 4}}" width="${{Math.max(w, count ? 1.5 : 0)}}" height="12" fill="${{colors[key]}}">
            <title>${{esc(system.system_name)}} · ${{key}}: ${{count}} (${{((100 * count) / total).toFixed(1)}}%)</title>
          </rect>`;
          x += w;
          return rect;
        }}).join("");
        const name = String(system.system_name || "Unknown");
        const shortName = name.length > 9 ? `${{name.slice(0, 8)}}…` : name;
        return `
          <g>
            <text x="0" y="${{y + 14}}" fill="#94a3b8" font-size="11">${{esc(shortName)}}</text>
            ${{rects}}
            <text x="${{width}}" y="${{y + 14}}" fill="#cbd5e1" font-size="10" text-anchor="end">${{total}}</text>
          </g>`;
      }}).join("");

      container.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" aria-label="Outcomes by system">${{rows}}</svg>`;
    }}

    async function loadSystemOutcomeChart() {{
      try {{
        const response = await fetch("/stats/system-outcomes");
        if (!response.ok) return;
        renderSystemOutcomeChart(await response.json());
      }} catch (err) {{
        console.error("System outcome chart refresh failed", err);
      }}
    }}

    let districtGeoCache = {{}};
    let districtStatsCache = null;
    let selectedDistrictId = null;
    let districtIdProperties = ["DIST_NUM", "POLICE_DISTRICT", "DISTRICT", "district"];

    function projectLonLat(lon, lat, bounds, width, height, pad = 8) {{
      const [minLon, minLat, maxLon, maxLat] = bounds;
      const x = pad + ((lon - minLon) / Math.max(maxLon - minLon, 1e-9)) * (width - pad * 2);
      // SVG y grows downward; flip latitude.
      const y = pad + ((maxLat - lat) / Math.max(maxLat - minLat, 1e-9)) * (height - pad * 2);
      return [x, y];
    }}

    function ringToPath(ring, bounds, width, height) {{
      if (!ring || ring.length < 2) return "";
      return ring.map((pt, index) => {{
        const [x, y] = projectLonLat(pt[0], pt[1], bounds, width, height);
        return `${{index === 0 ? "M" : "L"}}${{x.toFixed(1)}} ${{y.toFixed(1)}}`;
      }}).join(" ") + " Z";
    }}

    function geometryToPath(geometry, bounds, width, height) {{
      if (!geometry) return "";
      const type = geometry.type;
      const coords = geometry.coordinates;
      if (type === "Polygon") {{
        return (coords || []).map((ring) => ringToPath(ring, bounds, width, height)).join(" ");
      }}
      if (type === "MultiPolygon") {{
        return (coords || [])
          .map((poly) => (poly || []).map((ring) => ringToPath(ring, bounds, width, height)).join(" "))
          .join(" ");
      }}
      return "";
    }}

    function featureBounds(features) {{
      let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
      const walk = (value) => {{
        if (!Array.isArray(value)) return;
        if (typeof value[0] === "number") {{
          const lon = value[0], lat = value[1];
          if (lon < minLon) minLon = lon;
          if (lat < minLat) minLat = lat;
          if (lon > maxLon) maxLon = lon;
          if (lat > maxLat) maxLat = lat;
          return;
        }}
        value.forEach(walk);
      }};
      for (const feature of features) walk(feature?.geometry?.coordinates);
      if (!Number.isFinite(minLon)) return [-105.1, 39.55, -104.6, 39.95];
      return [minLon, minLat, maxLon, maxLat];
    }}

    function districtFill(intensity, hasMapping) {{
      if (!hasMapping) return "#334155";
      if (!intensity) return "#1e3a5f";
      if (intensity < 0.25) return "#1d4ed8";
      if (intensity < 0.5) return "#2563eb";
      if (intensity < 0.75) return "#3b82f6";
      if (intensity < 0.95) return "#f59e0b";
      return "#f97316";
    }}

    function featureDistrictId(agency, feature) {{
      const props = feature?.properties || {{}};
      let raw;
      for (const key of districtIdProperties) {{
        if (props[key] != null && props[key] !== "") {{
          raw = props[key];
          break;
        }}
      }}
      const num = Number(raw);
      if (!Number.isFinite(num)) return null;
      return `${{agency}}-${{num}}`;
    }}

    function districtAgencyIds(stats) {{
      const fromAgencies = (stats?.agencies || [])
        .filter((a) => a && a.id && (a.available !== false))
        .map((a) => a.id);
      if (fromAgencies.length) return fromAgencies;
      return Object.keys(stats?.gis || {{}}).filter((id) => stats.gis[id]);
    }}

    function renderDistrictMap(stats) {{
      const container = document.getElementById("district-map");
      const meta = document.getElementById("district-meta");
      if (!container || !meta || !stats) return;

      if (Array.isArray(stats.district_id_properties) && stats.district_id_properties.length) {{
        districtIdProperties = stats.district_id_properties;
      }}

      const byId = Object.fromEntries((stats.districts || []).map((d) => [d.id, d]));
      const features = [];
      for (const agency of districtAgencyIds(stats)) {{
        const geo = districtGeoCache[agency];
        if (!geo?.features) continue;
        for (const feature of geo.features) {{
          const id = featureDistrictId(agency, feature);
          if (!id) continue;
          features.push({{ agency, id, feature, stats: byId[id] || null }});
        }}
      }}

      if (!features.length) {{
        meta.textContent = "District GeoJSON not available";
        container.innerHTML = `<div class="activity-empty">Mount GIS GeoJSON to enable the map</div>`;
        return;
      }}

      const width = 420;
      const height = 260;
      const bounds = featureBounds(features.map((item) => item.feature));
      const paths = features.map((item) => {{
        const counts = item.stats?.counts || {{}};
        const total = counts.total || 0;
        const intensity = item.stats?.intensity || 0;
        const hasMapping = Boolean(item.stats?.talkgroups?.length);
        const label = item.stats?.label || item.id;
        const d = geometryToPath(item.feature.geometry, bounds, width, height);
        const fill = districtFill(intensity, hasMapping);
        const active = selectedDistrictId === item.id ? " active" : "";
        const unmapped = hasMapping ? "" : " unmapped";
        const title = hasMapping
          ? `${{label}}: ${{total}} calls in ${{stats.minutes}}m (enc ${{counts.encrypted || 0}}, clear ${{counts.completed || 0}}, fail ${{counts.failed || 0}})`
          : `${{label}}: ${{item.stats?.note || "no district talkgroup on this trunk"}}`;
        return `<path class="${{active}}${{unmapped}}" data-district-id="${{esc(item.id)}}" data-primary-tg="${{item.stats?.primary_talkgroup ?? ""}}" d="${{d}}" fill="${{fill}}" title="${{esc(title)}}"></path>`;
      }}).join("");

      const hot = [...(stats.districts || [])]
        .filter((d) => (d.counts?.total || 0) > 0)
        .sort((a, b) => (b.counts.total || 0) - (a.counts.total || 0))
        .slice(0, 3)
        .map((d) => `${{d.label}} ${{d.counts.total}}`)
        .join(" · ");
      meta.textContent = hot
        ? `Last ${{stats.minutes}}m · ${{hot}}`
        : `Last ${{stats.minutes}}m · no district TG activity`;

      container.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" aria-label="Police district activity">${{paths}}</svg>`;
    }}

    async function ensureDistrictGeo(stats) {{
      const agencies = stats?.agencies || [];
      const targets = agencies.length
        ? agencies.filter((a) => a && a.id && a.available !== false)
        : districtAgencyIds(stats).map((id) => ({{ id, geojson_url: `/gis/${{id}}.geojson` }}));
      for (const agency of targets) {{
        const id = agency.id;
        if (districtGeoCache[id]) continue;
        const url = agency.geojson_url || `/gis/${{id}}.geojson`;
        try {{
          const response = await fetch(url);
          if (!response.ok) continue;
          districtGeoCache[id] = await response.json();
        }} catch (err) {{
          console.error(`Failed loading ${{id}} district GeoJSON`, err);
        }}
      }}
    }}

    async function loadDistrictMap() {{
      try {{
        const response = await fetch("/stats/districts?minutes=60");
        if (!response.ok) return;
        districtStatsCache = await response.json();
        await ensureDistrictGeo(districtStatsCache);
        renderDistrictMap(districtStatsCache);
      }} catch (err) {{
        console.error("District map refresh failed", err);
      }}
    }}

    function renderEncryptedAnomalies(payload) {{
      const badge = document.getElementById("anomaly-badge");
      const summary = document.getElementById("anomaly-summary");
      const list = document.getElementById("anomaly-list");
      if (!badge || !summary || !list || !payload) return;

      const anomalies = payload.anomalies || [];
      const high = payload.high_count || 0;
      const medium = payload.medium_count || 0;
      const active = Boolean(payload.active) && anomalies.length > 0;
      badge.classList.toggle("active", active);
      badge.classList.toggle("high", high > 0);

      if (!active) {{
        summary.textContent = payload.cold_start ? "learning baseline" : "quiet";
        list.innerHTML = "";
        badge.title = payload.cold_start
          ? "Encrypted history is still thin — anomaly scoring is conservative until weekday/hour baselines fill in"
          : `No encrypted tempo anomalies in the last ${{payload.window_minutes || 15}}m`;
        return;
      }}

      const total = payload.anomaly_count || anomalies.length;
      summary.textContent = `total: ${{total}}`;
      const coldNote = payload.cold_start ? " · learning baseline" : "";
      const breakdown = [];
      if (high) breakdown.push(`${{high}} high`);
      if (medium) breakdown.push(`${{medium}} med`);
      const other = total - high - medium;
      if (other > 0) breakdown.push(`${{other}} low`);
      badge.title = `Encrypted tempo anomalies · ${{breakdown.join(", ") || `${{total}} flagged`}} · last ${{payload.window_minutes || 15}}m vs ${{payload.baseline_days || 14}}d weekday/hour baseline${{coldNote}}`;

      list.innerHTML = anomalies.slice(0, 3).map((item) => {{
        const tag = item.talkgroup_tag || `TG ${{item.talkgroup}}`;
        const reason = (item.reasons || []).join("; ");
        const ratio = item.rate_ratio != null ? `${{item.rate_ratio}}×` : "";
        return `<button type="button" class="anomaly-item" data-tg="${{item.talkgroup}}" title="${{esc(reason)}}">
          <span class="conf ${{esc(item.confidence || "low")}}">${{esc(item.confidence || "low")}}</span>
          ${{esc(tag)}} ${{esc(ratio)}}
        </button>`;
      }}).join("");
    }}

    async function loadEncryptedAnomalies() {{
      try {{
        const response = await fetch("/stats/encrypted-anomalies?window_minutes=15&baseline_days=14&limit=8");
        if (!response.ok) return;
        renderEncryptedAnomalies(await response.json());
      }} catch (err) {{
        console.error("Encrypted anomaly refresh failed", err);
      }}
    }}

    function talkgroupLabel(group) {{
      const tag = group.talkgroup_tag || "Unknown";
      return `${{group.talkgroup}} · ${{tag}}`;
    }}

    function readTalkgroupFromUrl() {{
      const value = new URLSearchParams(window.location.search).get("tg");
      if (!value) return null;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }}

    function readSystemFromUrl() {{
      const value = new URLSearchParams(window.location.search).get("system");
      return value ? value : null;
    }}

    function syncFilterUrl() {{
      const url = new URL(window.location.href);
      if (selectedTalkgroup != null) {{
        url.searchParams.set("tg", String(selectedTalkgroup));
      }} else {{
        url.searchParams.delete("tg");
      }}
      if (selectedSystem) {{
        url.searchParams.set("system", selectedSystem);
      }} else {{
        url.searchParams.delete("system");
      }}
      window.history.replaceState({{}}, "", url);
    }}

    function getSelectedTalkgroupMeta() {{
      return talkgroupsCatalog.find((group) => Number(group.talkgroup) === selectedTalkgroup) || null;
    }}

    function callsEndpoint() {{
      const params = new URLSearchParams({{ limit: String(CALLS_LIMIT) }});
      if (selectedTalkgroup != null) {{
        params.set("talkgroup", String(selectedTalkgroup));
      }}
      if (selectedSystem) {{
        params.set("system", selectedSystem);
      }}
      if (rangeFrom) params.set("from", rangeFrom);
      if (rangeTo) params.set("to", rangeTo);
      if (transcriptQuery) params.set("q", transcriptQuery);
      // When alerts-only is on (especially with no TG focused), ask the API for
      // keyword-matching transcripts so encrypted/metadata rows don't crowd them out.
      if (alertsOnly) params.set("alerts_only", "true");
      return `/calls?${{params}}`;
    }}

    function localInputToIso(value) {{
      if (!value) return null;
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return null;
      return date.toISOString();
    }}

    function formatRangeLabel(iso) {{
      if (!iso) return "";
      const date = new Date(iso);
      if (Number.isNaN(date.getTime())) return iso;
      return date.toLocaleString();
    }}

    function updateRangeUi() {{
      const note = document.getElementById("range-active-note");
      if (!note) return;
      if (!rangeSearchActive) {{
        note.hidden = true;
        note.textContent = "";
        return;
      }}
      const parts = [];
      if (rangeFrom) parts.push(`from ${{formatRangeLabel(rangeFrom)}}`);
      if (rangeTo) parts.push(`to ${{formatRangeLabel(rangeTo)}}`);
      if (transcriptQuery) parts.push(`transcript “${{transcriptQuery}}”`);
      if (selectedTalkgroup != null) parts.push(`TG ${{selectedTalkgroup}}`);
      if (selectedSystem) parts.push(selectedSystem);
      note.hidden = false;
      note.textContent = `Search active · ${{parts.join(" · ")}} · live updates paused`;
    }}

    function applyRangeSearch() {{
      const fromInput = document.getElementById("range-from");
      const toInput = document.getElementById("range-to");
      const qInput = document.getElementById("transcript-q");
      const fromIso = localInputToIso(fromInput?.value || "");
      const toIso = localInputToIso(toInput?.value || "");
      const q = (qInput?.value || "").trim();
      if (!fromIso && !toIso && !q) {{
        alert("Enter a transcript search and/or choose a From/To date/time.");
        return;
      }}
      if (fromIso && toIso && fromIso > toIso) {{
        alert("From must be earlier than To.");
        return;
      }}
      rangeFrom = fromIso;
      rangeTo = toIso;
      transcriptQuery = q || null;
      rangeSearchActive = true;
      updateRangeUi();
      refreshDashboard({{ force: true }});
    }}

    function clearRangeSearch({{ refresh = true }} = {{}}) {{
      rangeFrom = null;
      rangeTo = null;
      transcriptQuery = null;
      rangeSearchActive = false;
      const fromInput = document.getElementById("range-from");
      const toInput = document.getElementById("range-to");
      const qInput = document.getElementById("transcript-q");
      if (fromInput) fromInput.value = "";
      if (toInput) toInput.value = "";
      if (qInput) qInput.value = "";
      updateRangeUi();
      if (refresh) refreshDashboard({{ force: true }});
    }}

    function compareCalls(a, b) {{
      const numericKeys = new Set(["id", "talkgroup", "src", "call_length"]);
      let av = a[sortKey] ?? "";
      let bv = b[sortKey] ?? "";
      if (numericKeys.has(sortKey)) {{
        av = Number(av) || 0;
        bv = Number(bv) || 0;
        return sortDir === "asc" ? av - bv : bv - av;
      }}
      av = String(av).toLowerCase();
      bv = String(bv).toLowerCase();
      const cmp = av.localeCompare(bv);
      return sortDir === "asc" ? cmp : -cmp;
    }}

    function sortCalls(calls) {{
      return [...calls].sort(compareCalls);
    }}

    function updateSortIndicators() {{
      for (const th of document.querySelectorAll("th[data-sort]")) {{
        const indicator = th.querySelector(".sort-indicator");
        if (th.dataset.sort === sortKey) {{
          th.classList.add("sorted");
          indicator.textContent = sortDir === "asc" ? " ▲" : " ▼";
        }} else {{
          th.classList.remove("sorted");
          indicator.textContent = "";
        }}
      }}
    }}

    const NON_PLAYABLE_STATUSES = new Set(["encrypted", "unknown_talkgroup"]);

    function formatRid(value) {{
      if (value == null || value === -1 || value === 0 || value === "") return "unknown";
      return String(value);
    }}

    function buildRecordsRequestText(call) {{
      if (!recordsRequest.enabled) return "";
      const created = call.created_at || "unknown";
      const system = call.system_name || "unknown";
      const talkgroup = call.talkgroup;
      const tag = (call.talkgroup_tag || "").trim();
      const category = (call.category || "").trim();
      const src = formatRid(call.src);
      const callType = call.call_type || "group";
      const target = call.target;
      const freq = call.freq;
      const confidence = (call.addressing_confidence || "").trim();

      let addressing;
      if (callType === "unit_to_unit") {{
        addressing = [
          "Unit-to-unit / private call (inferred)",
          `- Source RID: ${{src}}`,
          `- Target RID: ${{formatRid(target)}}`,
        ].join("\\n");
      }} else if (callType === "unknown") {{
        addressing = [
          "Addressing unknown",
          `- Source RID: ${{src}}`,
          `- Reported ID (TG field): ${{talkgroup ?? "unknown"}}`,
        ].join("\\n");
      }} else {{
        let tgLabel = talkgroup == null ? "unknown" : String(talkgroup);
        if (tag) tgLabel = `${{tgLabel}} (${{tag}})`;
        addressing = [
          "Group talkgroup call",
          `- Talkgroup: ${{tgLabel}}`,
          `- Source RID: ${{src}}`,
        ].join("\\n");
      }}

      const lines = [
        recordsRequest.title,
        "",
        "Please produce audio (and chain-of-custody export) for the following encrypted P25 traffic:",
        "",
        `- Our internal record ID (not an agency logger ID): ${{call.id ?? "unknown"}}`,
        `- Observed at (UTC): ${{created}}`,
        `- System / site: ${{system}}`,
        freq == null || freq === "" ? "- Frequency: unknown" : `- Frequency: ${{freq}} MHz`,
        category ? `- Category: ${{category}}` : "- Category: unknown",
        "- Encrypted: yes (no clear audio available from scanner capture)",
        "",
        addressing,
      ];
      if (confidence) lines.push(`- Addressing confidence: ${{confidence}}`);
      lines.push(
        "",
        "Related to case/CAD #: ________________",
        `Requestor / ${{recordsRequest.contact_label}} contact: ________________`,
        "Preferred format: WAV or vendor logger export with metadata sheet",
        "",
        "Notes: This request identifies the call from publicly observable trunking metadata",
        "(time, system/site, talkgroup or RIDs, frequency). Our internal record ID is for",
        "our tracking only and will not match agency CAD/logger identifiers.",
        "Decryption keys are not included as part of this request and must remain under agency control.",
      );
      return lines.join("\\n");
    }}

    async function copyTextToClipboard(text) {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(text);
        return;
      }}
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      document.body.removeChild(area);
    }}

    async function copyRecordsRequest(button) {{
      const callId = Number(button.dataset.callId);
      const call = callsData.find((item) => Number(item.id) === callId);
      const text = call
        ? buildRecordsRequestText(call)
        : (button.dataset.requestText || "");
      if (!text) return;
      const buttonLabel = `📋 ${{recordsRequest.button_label}}`;
      try {{
        await copyTextToClipboard(text);
        const original = button.textContent;
        button.classList.add("copied");
        button.textContent = "✓ Copied";
        setTimeout(() => {{
          button.classList.remove("copied");
          button.textContent = original;
        }}, 1600);
      }} catch (err) {{
        console.error("Clipboard copy failed", err);
        button.textContent = "Copy failed";
        setTimeout(() => {{ button.textContent = buttonLabel; }}, 1600);
      }}
    }}

    function renderAudioCell(call) {{
      if (call.status === "encrypted") {{
        if (!recordsRequest.enabled) {{
          return `<span class="encrypted-indicator" title="Encrypted — not recorded">🔒</span>`;
        }}
        return `<span class="encrypted-actions">
          <span class="encrypted-indicator" title="Encrypted — not recorded">🔒</span>
          <button type="button" class="copy-request" data-call-id="${{call.id}}" title="Copy ${{esc(recordsRequest.button_label)}} request to clipboard">📋 ${{esc(recordsRequest.button_label)}}</button>
        </span>`;
      }}
      if (call.status === "unknown_talkgroup") {{
        return `<span class="unknown-tg-indicator" title="Talk group not in talk_groups.csv — add to enable recording">📋</span>`;
      }}
      return `<audio controls preload="none" class="audio-player" data-call-id="${{call.id}}" src="/calls/${{call.id}}/audio"></audio>`;
    }}

    function updateAutoPlayStatus() {{
      if (!selectedTalkgroup) {{
        autoPlayStatus.textContent = "Select a talk group to auto-play";
        return;
      }}
      if (!autoPlayEnabled) {{
        autoPlayStatus.textContent = "Auto-play off";
        return;
      }}
      if (!audioUnlocked) {{
        autoPlayStatus.textContent = "Click anywhere to allow audio playback";
        return;
      }}
      const queued = playQueue.length + (isPlayingQueue ? 1 : 0);
      if (isPlayingQueue) {{
        autoPlayStatus.textContent = queued > 1
          ? `Playing · ${{queued - 1}} queued to latest`
          : "Playing";
      }} else if (playQueue.length) {{
        autoPlayStatus.textContent = `${{playQueue.length}} queued to latest`;
      }} else {{
        autoPlayStatus.textContent = "Listening for new calls on this talk group";
      }}
    }}

    function highlightPlayingRow(callId) {{
      for (const row of document.querySelectorAll("tr[data-call-id]")) {{
        row.classList.toggle(
          "playing",
          callId != null && Number(row.dataset.callId) === Number(callId),
        );
      }}
    }}

    async function tryPlayAudio(audio) {{
      try {{
        await audio.play();
        audioUnlocked = true;
        return true;
      }} catch (err) {{
        console.warn("Audio playback blocked:", err);
        return false;
      }}
    }}

    function unlockAudioPlayback() {{
      if (audioUnlocked) return;
      audioUnlocked = true;
      updateAutoPlayStatus();
      if (autoPlayEnabled && selectedTalkgroup != null && !isPlayingQueue) {{
        processPlayQueue();
      }}
    }}

    function getCallsChronologically() {{
      return [...callsData].sort((a, b) => a.id - b.id);
    }}

    function appendToPlayQueue(callIds) {{
      for (const id of callIds) {{
        if (id === currentAutoPlayId || playQueue.includes(id)) continue;
        playQueue.push(id);
      }}
      playQueue.sort((a, b) => a - b);
    }}

    function getPlayableCallsChronologically() {{
      return getCallsChronologically().filter((call) => !NON_PLAYABLE_STATUSES.has(call.status));
    }}

    function queueCatchUpFrom(callId) {{
      const newerIds = getPlayableCallsChronologically()
        .map((call) => call.id)
        .filter((id) => id > callId);
      playQueue = newerIds;
    }}

    function resetAutoPlayState() {{
      playQueue = [];
      isPlayingQueue = false;
      currentAutoPlayId = null;
      knownCallIds.clear();
      initialLoadDone = false;
      pauseAllAudio();
      updateAutoPlayStatus();
    }}

    function pauseAllAudio(exceptId = null) {{
      for (const audio of document.querySelectorAll("audio[data-call-id]")) {{
        if (exceptId != null && audio.dataset.callId === String(exceptId)) continue;
        audio.pause();
      }}
    }}

    function attachAutoPlayEndedHandler(audio) {{
      if (audio.dataset.autoPlayBound === "1") return;
      audio.dataset.autoPlayBound = "1";
      audio.addEventListener("ended", () => {{
        if (Number(audio.dataset.callId) !== currentAutoPlayId) return;
        isPlayingQueue = false;
        currentAutoPlayId = null;
        highlightPlayingRow(null);
        updateAutoPlayStatus();
        processPlayQueue();
      }});
    }}

    async function playCallById(callId) {{
      pauseAllAudio(callId);
      isPlayingQueue = true;
      currentAutoPlayId = Number(callId);
      highlightPlayingRow(callId);

      const audio = document.querySelector(`audio[data-call-id="${{callId}}"]`);
      if (!audio) {{
        isPlayingQueue = false;
        currentAutoPlayId = null;
        highlightPlayingRow(null);
        return false;
      }}

      attachAutoPlayEndedHandler(audio);
      audio.currentTime = 0;
      const started = await tryPlayAudio(audio);
      if (!started) {{
        isPlayingQueue = false;
        currentAutoPlayId = null;
        highlightPlayingRow(null);
        updateAutoPlayStatus();
        return false;
      }}

      scrollRowIntoViewIfNeeded(audio.closest("tr"));
      updateAutoPlayStatus();
      return true;
    }}

    function scrollRowIntoViewIfNeeded(row) {{
      if (!row) return;
      const rect = row.getBoundingClientRect();
      const margin = 48;
      if (rect.top >= margin && rect.bottom <= window.innerHeight - margin) return;
      row.scrollIntoView({{ block: "nearest", behavior: "smooth" }});
    }}

    async function processPlayQueue() {{
      if (!autoPlayEnabled || selectedTalkgroup == null) {{
        updateAutoPlayStatus();
        return;
      }}
      if (!audioUnlocked) {{
        updateAutoPlayStatus();
        return;
      }}
      if (isPlayingQueue) return;

      while (playQueue.length) {{
        const nextId = playQueue[0];
        if (await playCallById(nextId)) {{
          playQueue.shift();
          return;
        }}
        break;
      }}
      updateAutoPlayStatus();
    }}

    function enqueueNewCalls(calls) {{
      if (!autoPlayEnabled || selectedTalkgroup == null) return;
      const newCalls = calls
        .filter((call) => !knownCallIds.has(call.id))
        .sort((a, b) => a.id - b.id);
      for (const call of newCalls) {{
        knownCallIds.add(call.id);
      }}
      if (!initialLoadDone) {{
        initialLoadDone = true;
        updateAutoPlayStatus();
        return;
      }}
      appendToPlayQueue(
        newCalls
          .filter((call) => !NON_PLAYABLE_STATUSES.has(call.status))
          .map((call) => call.id),
      );
      processPlayQueue();
    }}

    function captureAudioState() {{
      const state = {{}};
      for (const audio of document.querySelectorAll("audio[data-call-id]")) {{
        if (Number(audio.dataset.callId) === currentAutoPlayId) {{
          state[audio.dataset.callId] = {{
            time: audio.currentTime,
            playing: !audio.paused,
            auto: true,
          }};
          continue;
        }}
        if (!audio.paused || audio.currentTime > 0) {{
          state[audio.dataset.callId] = {{
            time: audio.currentTime,
            playing: !audio.paused,
            auto: false,
          }};
        }}
      }}
      return state;
    }}

    function restoreAudioState(state) {{
      for (const [id, {{ time, playing, auto }}] of Object.entries(state)) {{
        const audio = document.querySelector(`audio[data-call-id="${{id}}"]`);
        if (!audio) continue;
        audio.currentTime = time;
        if (!playing) continue;
        if (auto || Number(id) === currentAutoPlayId) {{
          attachAutoPlayEndedHandler(audio);
          isPlayingQueue = true;
          currentAutoPlayId = Number(id);
          highlightPlayingRow(currentAutoPlayId);
          tryPlayAudio(audio);
        }} else if (!currentAutoPlayId) {{
          tryPlayAudio(audio);
        }}
      }}
    }}

    function getVisibleCalls() {{
      return callsData.filter((call) => {{
        if (hideEncrypted && call.status === "encrypted") return false;
        if (hideUnknownTg && call.status === "unknown_talkgroup") return false;
        if (alertsOnly) {{
          // Prefer indexed has_alert from API; fall back to client keyword scan.
          if (call.has_alert != null) return Number(call.has_alert) === 1;
          return Boolean(transcriptAlertEmojis(call.transcript || ""));
        }}
        return true;
      }});
    }}

    function renderRows(calls) {{
      if (!calls.length) {{
        let message = "No calls yet";
        if (selectedSystem && selectedTalkgroup != null) {{
          message = `No calls for this talk group on ${{selectedSystem}}`;
        }} else if (selectedSystem) {{
          message = `No calls for ${{selectedSystem}} yet`;
        }} else if (selectedTalkgroup != null) {{
          message = "No calls for this talk group yet";
        }}
        if (callsData.length && (hideEncrypted || hideUnknownTg || alertsOnly)) {{
          message = "No calls match the current filters";
        }}
        return `<tr><td colspan="13"><em>${{message}}</em></td></tr>`;
      }}
      return calls.map((call) => {{
        const transcript = call.transcript || "";
        const previewHtml = formatTranscriptCell(transcript, call.status);
        const alertsHtml = formatAlertsCell(transcript);
        const tagHtml = call.talkgroup_tag ? esc(call.talkgroup_tag) : "<em>—</em>";
        const category = call.category || "";
        const categoryEmoji = call.category_emoji || "📁";
        const categoryHtml = `<span class="category" title="${{esc(category || "Unknown category")}}"><span class="category-emoji">${{categoryEmoji}}</span><span class="category-label">${{category ? esc(category) : "<em>—</em>"}}</span></span>`;
        const srcValue = (call.src == null || call.src === -1 || call.src === 0) ? "—" : String(call.src);
        const callType = call.call_type || "group";
        let typeHtml = `<span class="call-type group" title="Group talkgroup call">TG</span>`;
        if (callType === "unit_to_unit") {{
          const target = (call.target == null || call.target === -1 || call.target === 0) ? "?" : String(call.target);
          typeHtml = `<span class="call-type u2u" title="Likely unit-to-unit / private call">📡→${{esc(target)}}</span>`;
        }} else if (callType === "unknown") {{
          typeHtml = `<span class="call-type unknown" title="Addressing unknown">?</span>`;
        }}
        const playingClass = Number(call.id) === currentAutoPlayId ? " playing" : "";
        return `<tr data-call-id="${{call.id}}" class="${{playingClass}}">
          <td>${{call.id}}</td>
          <td>${{esc(call.created_at || "")}}</td>
          <td>${{esc(call.system_name || "")}}</td>
          <td>${{esc(String(call.talkgroup ?? ""))}}</td>
          <td>${{typeHtml}}</td>
          <td title="Radio / unit ID">${{esc(srcValue)}}</td>
          <td>${{esc(String(call.call_length ?? ""))}}</td>
          <td><span class="status ${{esc(call.status)}}">${{esc(call.status)}}</span></td>
          <td>${{renderAudioCell(call)}}</td>
          <td class="tag">${{tagHtml}}</td>
          <td class="category-cell">${{categoryHtml}}</td>
          <td class="alerts">${{alertsHtml}}</td>
          <td class="transcript">${{previewHtml}}</td>
        </tr>`;
      }}).join("");
    }}

    function renderTableBody() {{
      const audioState = captureAudioState();
      const playingId = currentAutoPlayId;
      let anchorTop = null;
      if (playingId != null) {{
        const playingRow = document.querySelector(`tr[data-call-id="${{playingId}}"]`);
        if (playingRow) anchorTop = playingRow.getBoundingClientRect().top;
      }}
      const scrollY = window.scrollY;

      document.getElementById("calls-body").innerHTML = renderRows(sortCalls(getVisibleCalls()));
      restoreAudioState(audioState);

      if (playingId != null && anchorTop != null) {{
        const playingRow = document.querySelector(`tr[data-call-id="${{playingId}}"]`);
        if (playingRow) {{
          const delta = playingRow.getBoundingClientRect().top - anchorTop;
          if (Math.abs(delta) > 1) window.scrollBy(0, delta);
        }}
      }} else {{
        window.scrollTo(0, scrollY);
      }}

      updateFilterUi();
      updateAutoPlayStatus();
    }}

    function updateStats(counts) {{
      // Always system-wide from /health — never derived from the filtered call list.
      // Missing keys mean zero (SQL omits empty statuses unless the API fills them).
      for (const status of QUEUE_STATUSES) {{
        globalQueue[status] = counts?.[status] ?? 0;
        const el = document.querySelector(`[data-status="${{status}}"]`);
        if (el) el.textContent = globalQueue[status];
      }}
    }}

    function filterTalkgroupOptions(query) {{
      const needle = query.trim().toLowerCase();
      let options = talkgroupsCatalog;
      if (needle) {{
        options = talkgroupsCatalog.filter((group) => {{
          const haystack = [
            group.talkgroup,
            group.talkgroup_tag,
            group.description,
            group.category,
          ].join(" ").toLowerCase();
          return haystack.includes(needle);
        }});
      }} else {{
        const withCalls = talkgroupsCatalog.filter((group) => Number(group.call_count || 0) > 0);
        options = withCalls.length ? withCalls : talkgroupsCatalog;
      }}
      return options;
    }}

    function renderTalkgroupMenu(query = "") {{
      const options = filterTalkgroupOptions(query);
      activeMenuIndex = options.length ? 0 : -1;
      if (!options.length) {{
        tgMenu.innerHTML = `<button type="button" class="combobox-option" disabled>No matching talk groups</button>`;
        tgMenu.hidden = false;
        return;
      }}
      tgMenu.innerHTML = options.map((group, index) => {{
        const activeClass = index === activeMenuIndex ? " active" : "";
        const count = group.call_count ? `${{group.call_count}} calls` : "no calls yet";
        const description = group.description ? `<span>${{esc(group.description)}} · ${{esc(count)}}</span>` : `<span>${{esc(count)}}</span>`;
        return `<button type="button" class="combobox-option${{activeClass}}" data-tg="${{group.talkgroup}}">
          <strong>${{esc(talkgroupLabel(group))}}</strong>
          ${{description}}
        </button>`;
      }}).join("");
      tgMenu.hidden = false;
    }}

    function pruneActivityHighlights() {{
      const now = Date.now();
      for (const [talkgroup, expiresAt] of activityTalkgroups) {{
        if (expiresAt <= now) activityTalkgroups.delete(talkgroup);
      }}
    }}

    function clearActivityHighlight(talkgroup) {{
      if (talkgroup == null) {{
        activityTalkgroups.clear();
        return;
      }}
      activityTalkgroups.delete(Number(talkgroup));
    }}

    function applyQuickFilterActivity() {{
      pruneActivityHighlights();
      for (const button of quickFilters.querySelectorAll(".quick-filter")) {{
        const talkgroup = Number(button.dataset.tg);
        button.classList.toggle("activity", activityTalkgroups.has(talkgroup));
      }}
    }}

    function noteTalkgroupActivity(catalog) {{
      pruneActivityHighlights();
      const now = Date.now();
      for (const group of catalog) {{
        const talkgroup = Number(group.talkgroup);
        if (!Number.isFinite(talkgroup)) continue;
        const lastAt = group.last_call_at || "";
        const previous = knownTgActivityAt.get(talkgroup);
        knownTgActivityAt.set(talkgroup, lastAt);
        if (!previous || !lastAt || lastAt <= previous) continue;
        if (selectedTalkgroup == null) continue;
        if (talkgroup === selectedTalkgroup) continue;
        activityTalkgroups.set(talkgroup, now + ACTIVITY_HIGHLIGHT_MS);
      }}
    }}

    function updateFilterUi({{ scrollActiveQuickFilter = false }} = {{}}) {{
      const selected = getSelectedTalkgroupMeta();
      if (selectedTalkgroup != null && selected) {{
        tgChip.hidden = false;
        tgChipLabel.textContent = talkgroupLabel(selected);
        tgSearch.placeholder = "Change talk group…";
      }} else if (selectedTalkgroup != null) {{
        tgChip.hidden = false;
        tgChipLabel.textContent = `TG ${{selectedTalkgroup}}`;
        tgSearch.placeholder = "Change talk group…";
      }} else {{
        tgChip.hidden = true;
        tgSearch.placeholder = "Search by TG, tag, or description…";
      }}

      const visible = getVisibleCalls();
      const parts = [];
      if (selectedSystem) parts.push(`system ${{selectedSystem}}`);
      if (selectedTalkgroup != null) parts.push("this talk group");
      if (rangeSearchActive) {{
        const bits = [];
        if (rangeFrom || rangeTo) bits.push("date/time");
        if (transcriptQuery) bits.push("transcript");
        parts.push(bits.length ? bits.join(" + ") + " search" : "search");
      }}
      if (alertsOnly) parts.push("alerts only");
      const suffix = parts.length ? ` in ${{parts.join(" · ")}}` : "";
      let summary = `${{visible.length}} call${{visible.length === 1 ? "" : "s"}} shown${{suffix}}`;
      if (visible.length !== callsData.length) {{
        summary += ` (${{callsData.length - visible.length}} hidden)`;
      }}
      filterSummary.textContent = summary;
      updateRangeUi();

      let activeQuickFilter = null;
      for (const button of quickFilters.querySelectorAll(".quick-filter")) {{
        const isActive = Number(button.dataset.tg) === selectedTalkgroup;
        button.classList.toggle("active", isActive);
        if (isActive) activeQuickFilter = button;
      }}
      if (scrollActiveQuickFilter && activeQuickFilter) {{
        activeQuickFilter.scrollIntoView({{ block: "nearest", inline: "nearest", behavior: "smooth" }});
      }}
      applyQuickFilterActivity();
      updateAutoPlayStatus();
    }}

    function renderQuickFilters() {{
      const active = talkgroupsCatalog
        .filter((group) => Number(group.call_count || 0) > 0);
      if (!active.length) {{
        quickFilters.innerHTML = "";
        return;
      }}
      quickFilters.innerHTML = active.map((group) => `
        <button type="button" class="quick-filter" data-tg="${{group.talkgroup}}" title="TG ${{group.talkgroup}}">
          ${{esc(group.talkgroup_tag || `TG ${{group.talkgroup}}`)}}
          <span style="opacity:0.7">(${{group.call_count}})</span>
        </button>
      `).join("");
      applyQuickFilterActivity();
    }}

    function renderSystemFilters() {{
      if (!systemsCatalog.length) {{
        systemFilters.innerHTML = `
          <span class="system-filters-label">Systems</span>
          <span class="system-filters-note">No systems seen yet</span>`;
        return;
      }}
      const buttons = [
        `<button type="button" class="system-filter${{selectedSystem == null ? " active" : ""}}" data-system="">
          All systems
        </button>`,
        ...systemsCatalog.map((system) => {{
          const isActive = selectedSystem === system.name;
          const activeClass = system.active ? " active" : "";
          const title = system.active
            ? `${{system.name}} active in the last 5 minutes`
            : `${{system.name}} idle / no recent activity`;
          const count = system.call_count || 0;
          return `<button type="button" class="system-filter${{isActive ? " active" : ""}}" data-system="${{esc(system.name)}}" title="${{esc(title)}}">
            <span class="dot${{activeClass}}"></span>
            ${{esc(system.name)}}
            <span class="count">(${{count}})</span>
          </button>`;
        }}),
      ];
      systemFilters.innerHTML = `
        <span class="system-filters-label">Systems</span>
        ${{buttons.join("")}}
        <span class="system-filters-note">green = activity in last 5 min</span>`;
    }}

    function selectSystem(system, {{ refresh = true }} = {{}}) {{
      selectedSystem = system ? String(system) : null;
      resetAutoPlayState();
      syncFilterUrl();
      renderSystemFilters();
      updateFilterUi();
      if (refresh) refreshCallsAndActivity();
      unlockAudioPlayback();
    }}

    function selectTalkgroup(talkgroup, {{ refresh = true }} = {{}}) {{
      selectedTalkgroup = talkgroup == null ? null : Number(talkgroup);
      if (selectedTalkgroup == null) selectedDistrictId = null;
      tgSearch.value = "";
      tgMenu.hidden = true;
      activeMenuIndex = -1;
      clearActivityHighlight(selectedTalkgroup);
      if (selectedTalkgroup == null) clearActivityHighlight(null);
      resetAutoPlayState();
      syncFilterUrl();
      updateFilterUi({{ scrollActiveQuickFilter: true }});
      if (districtStatsCache) renderDistrictMap(districtStatsCache);
      if (refresh) refreshCallsAndActivity();
      unlockAudioPlayback();
    }}

    async function loadSystems() {{
      const response = await fetch("/systems");
      if (!response.ok) return;
      const payload = await response.json();
      systemsCatalog = payload.systems || [];
      if (selectedSystem && !systemsCatalog.some((system) => system.name === selectedSystem)) {{
        // Keep selection even if config temporarily unavailable.
        systemsCatalog = [
          {{ name: selectedSystem, call_count: 0, active: false, configured: false }},
          ...systemsCatalog,
        ];
      }}
      renderSystemFilters();
    }}

    async function loadTalkgroups() {{
      const params = new URLSearchParams();
      if (selectedSystem) params.set("system", selectedSystem);
      const response = await fetch(`/talkgroups?${{params}}`);
      if (!response.ok) return;
      const payload = await response.json();
      talkgroupsCatalog = payload.talkgroups || [];
      noteTalkgroupActivity(talkgroupsCatalog);
      renderQuickFilters();
      updateFilterUi();
    }}

    async function refreshQueueStats() {{
      try {{
        const healthRes = await fetch("/health");
        if (!healthRes.ok) return;
        const health = await healthRes.json();
        updateStats(health.queue);
        updateBackendLabel(health);
      }} catch (err) {{
        console.error("Queue stats refresh failed", err);
      }}
    }}

    async function refreshCallsAndActivity() {{
      if (callsAbort) callsAbort.abort();
      callsAbort = new AbortController();
      const signal = callsAbort.signal;
      try {{
        const callsRes = await fetch(callsEndpoint(), {{ signal }});
        if (!callsRes.ok) return;
        const {{ calls }} = await callsRes.json();
        if (signal.aborted) return;
        const audioState = captureAudioState();
        callsData = calls;
        renderTableBody();
        restoreAudioState(audioState);
        if (!rangeSearchActive) enqueueNewCalls(calls);
        await loadActivityChart();
      }} catch (err) {{
        if (err && err.name === "AbortError") return;
        console.error("Calls refresh failed", err);
      }}
    }}

    async function refreshDashboard({{ force = false }} = {{}}) {{
      if (rangeSearchActive && !force) return;
      if (dashboardAbort) dashboardAbort.abort();
      dashboardAbort = new AbortController();
      const signal = dashboardAbort.signal;
      try {{
        const [callsRes, healthRes] = await Promise.all([
          fetch(callsEndpoint(), {{ signal }}),
          fetch("/health", {{ signal }}),
        ]);
        if (!callsRes.ok || !healthRes.ok) return;
        const {{ calls }} = await callsRes.json();
        const health = await healthRes.json();
        if (signal.aborted) return;
        const audioState = captureAudioState();
        callsData = calls;
        renderTableBody();
        restoreAudioState(audioState);
        if (!rangeSearchActive) enqueueNewCalls(calls);
        updateStats(health.queue);
        updateBackendLabel(health);
        await loadSystems();
        await loadTalkgroups();
        if (signal.aborted) return;
        await Promise.all([
          loadActivityChart(),
          loadEncryptedAnomalies(),
          loadSystemOutcomeChart(),
          loadDistrictMap(),
        ]);
        document.getElementById("last-updated").textContent = new Date().toLocaleTimeString();
      }} catch (err) {{
        if (err && err.name === "AbortError") return;
        console.error("Dashboard refresh failed", err);
      }}
    }}

    document.getElementById("range-search-btn").addEventListener("click", applyRangeSearch);
    document.getElementById("range-clear-btn").addEventListener("click", () => clearRangeSearch());
    for (const id of ["range-from", "range-to", "transcript-q"]) {{
      document.getElementById(id).addEventListener("keydown", (event) => {{
        if (event.key === "Enter") {{
          event.preventDefault();
          applyRangeSearch();
        }}
      }});
    }}

    document.getElementById("anomaly-list").addEventListener("click", (event) => {{
      const button = event.target.closest(".anomaly-item[data-tg]");
      if (!button) return;
      selectTalkgroup(button.dataset.tg);
    }});

    document.getElementById("district-map").addEventListener("click", (event) => {{
      const path = event.target.closest("path[data-district-id]");
      if (!path) return;
      const districtId = path.dataset.districtId;
      const primaryTg = path.dataset.primaryTg;
      if (!primaryTg) return;
      if (selectedDistrictId === districtId && selectedTalkgroup === Number(primaryTg)) {{
        selectedDistrictId = null;
        selectTalkgroup(null);
        return;
      }}
      selectedDistrictId = districtId;
      selectTalkgroup(primaryTg);
    }});

    tgSearch.addEventListener("focus", () => renderTalkgroupMenu(tgSearch.value));
    tgSearch.addEventListener("input", () => renderTalkgroupMenu(tgSearch.value));
    tgSearch.addEventListener("keydown", (event) => {{
      const options = [...tgMenu.querySelectorAll(".combobox-option:not([disabled])")];
      if (event.key === "ArrowDown") {{
        event.preventDefault();
        if (tgMenu.hidden) renderTalkgroupMenu(tgSearch.value);
        activeMenuIndex = Math.min(activeMenuIndex + 1, options.length - 1);
      }} else if (event.key === "ArrowUp") {{
        event.preventDefault();
        activeMenuIndex = Math.max(activeMenuIndex - 1, 0);
      }} else if (event.key === "Enter") {{
        event.preventDefault();
        if (activeMenuIndex >= 0 && options[activeMenuIndex]) {{
          selectTalkgroup(options[activeMenuIndex].dataset.tg);
          return;
        }}
        const direct = Number(tgSearch.value.trim());
        if (Number.isFinite(direct) && direct > 0) {{
          selectTalkgroup(direct);
        }}
        return;
      }} else if (event.key === "Escape") {{
        tgMenu.hidden = true;
        return;
      }} else {{
        return;
      }}
      options.forEach((option, index) => option.classList.toggle("active", index === activeMenuIndex));
      options[activeMenuIndex]?.scrollIntoView({{ block: "nearest" }});
    }});

    tgMenu.addEventListener("click", (event) => {{
      const option = event.target.closest(".combobox-option[data-tg]");
      if (!option) return;
      selectTalkgroup(option.dataset.tg);
    }});

    tgClear.addEventListener("click", () => selectTalkgroup(null));

    systemFilters.addEventListener("click", (event) => {{
      const button = event.target.closest(".system-filter[data-system]");
      if (!button) return;
      const system = button.dataset.system || null;
      if ((selectedSystem || null) === (system || null)) {{
        selectSystem(null);
      }} else {{
        selectSystem(system);
      }}
    }});

    quickFilters.addEventListener("click", (event) => {{
      const button = event.target.closest(".quick-filter[data-tg]");
      if (!button) return;
      const talkgroup = Number(button.dataset.tg);
      if (selectedTalkgroup === talkgroup) {{
        selectTalkgroup(null);
      }} else {{
        selectTalkgroup(talkgroup);
      }}
    }});

    document.addEventListener("click", (event) => {{
      if (!document.getElementById("tg-combobox").contains(event.target)) {{
        tgMenu.hidden = true;
      }}
    }});

    document.getElementById("calls-body").addEventListener("click", (event) => {{
      const button = event.target.closest(".copy-request[data-call-id]");
      if (!button) return;
      event.preventDefault();
      copyRecordsRequest(button);
    }});

    document.getElementById("calls-body").addEventListener("play", (event) => {{
      const audio = event.target.closest("audio[data-call-id]");
      if (!audio) return;
      const callId = Number(audio.dataset.callId);
      audioUnlocked = true;

      if (callId === currentAutoPlayId) return;

      pauseAllAudio(callId);

      if (!autoPlayEnabled || selectedTalkgroup == null) {{
        isPlayingQueue = false;
        currentAutoPlayId = null;
        playQueue = [];
        highlightPlayingRow(null);
        updateAutoPlayStatus();
        return;
      }}

      queueCatchUpFrom(callId);
      isPlayingQueue = true;
      currentAutoPlayId = callId;
      highlightPlayingRow(callId);
      attachAutoPlayEndedHandler(audio);
      updateAutoPlayStatus();
    }}, true);

    document.body.addEventListener("click", () => unlockAudioPlayback(), true);

    hideEncryptedToggle.addEventListener("change", () => {{
      hideEncrypted = hideEncryptedToggle.checked;
      renderTableBody();
    }});

    hideUnknownTgToggle.addEventListener("change", () => {{
      hideUnknownTg = hideUnknownTgToggle.checked;
      renderTableBody();
    }});

    alertsOnlyToggle.addEventListener("change", () => {{
      alertsOnly = alertsOnlyToggle.checked;
      refreshCallsAndActivity();
    }});

    autoPlayToggle.addEventListener("change", () => {{
      autoPlayEnabled = autoPlayToggle.checked;
      unlockAudioPlayback();
      if (!autoPlayEnabled) {{
        playQueue = [];
        isPlayingQueue = false;
        currentAutoPlayId = null;
        pauseAllAudio();
        highlightPlayingRow(null);
      }} else if (selectedTalkgroup != null) {{
        processPlayQueue();
      }}
      updateAutoPlayStatus();
    }});

    for (const th of document.querySelectorAll("th[data-sort]")) {{
      th.addEventListener("click", () => {{
        const key = th.dataset.sort;
        if (sortKey === key) {{
          sortDir = sortDir === "asc" ? "desc" : "asc";
        }} else {{
          sortKey = key;
          sortDir = key === "created_at" || key === "id" ? "desc" : "asc";
        }}
        updateSortIndicators();
        renderTableBody();
      }});
    }}

    selectedTalkgroup = readTalkgroupFromUrl();
    selectedSystem = readSystemFromUrl();
    updateSortIndicators();
    loadSystems().then(() => loadTalkgroups()).then(refreshDashboard);
    setInterval(refreshDashboard, POLL_MS);
    setInterval(refreshQueueStats, QUEUE_POLL_MS);
  </script>
</body>
</html>"""
    return HTMLResponse(content=page_html)
