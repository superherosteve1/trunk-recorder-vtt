#!/usr/bin/env python3
"""Parse Trunk Recorder stdout and POST skipped-recording activity to sdr-trunk-vtt.

Trunk Recorder does not run uploadScript when it skips a call. It only logs lines like:

  Not Recording: ENCRYPTED - src: 850811
  Not Recording: TG not in Talkgroup File

Real TR output often includes ANSI color codes and tab separators, e.g.:

  [Denver]\\t\\x1b[0;34m62C\\x1b[0m\\tTG: \\x1b[35m     35058\\x1b[0m\\t...

Pipe trunk-recorder output through this script (see scripts/run-trunk-recorder.sh) or:

  trunk-recorder config.json 2>&1 | ./scripts/tr-encrypted-relay.py

Environment (from .env or shell):
  VTT_API_URL   default http://127.0.0.1:8080
  VTT_API_KEY   or API_KEY
  TR_LOCAL_TIMEZONE  default America/Denver
  TR_AUTO_ADD_UNKNOWN_TG  default 1 — append Unknown placeholders to talk_groups.csv
  TR_TALKGROUPS_CSV  optional path to talk_groups.csv
  TR_CONFIG_JSON     optional path to Trunk Recorder config.json (for talkgroupsFile)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from talkgroups_csv import (  # noqa: E402
    ensure_unknown_placeholder,
    env_flag,
    resolve_talkgroups_csv,
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

SKIPPED_LINE = re.compile(
    r"\[(?P<ts>[^\]]+)\].*"
    r"\[(?P<system>[^\]]+)\]\s+"
    r"\d+C\s+TG:\s+(?P<talkgroup>\d+)\s+"
    r"Freq:\s+(?P<freq>[\d.]+)\s+MHz\s+"
    r"Not Recording:\s+(?P<reason>.+?)\s*$"
)
ENCRYPTED_SRC = re.compile(r"^ENCRYPTED(?:\s*-\s*src:\s*(?P<src>-?\d+))?$")
UNKNOWN_TG = re.compile(r"^TG not in Talkgroup File$")

# Avoid re-checking / re-appending the same TG repeatedly in one TR run.
_seen_unknown_tgs: set[int] = set()
_csv_path: Path | None = None
_auto_add = True


def load_env() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in {
            "API_KEY",
            "VTT_API_KEY",
            "VTT_API_URL",
            "TR_LOCAL_TIMEZONE",
            "TR_AUTO_ADD_UNKNOWN_TG",
            "TR_TALKGROUPS_CSV",
            "TR_CONFIG_JSON",
        }:
            continue
        value = value.strip().strip('"').strip("'")
        # .env wins over a stale exported shell value (setdefault hid key mismatches).
        os.environ[key] = value


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def normalize_line(line: str) -> str:
    # TR uses tabs between fields; collapse whitespace after stripping colors.
    return re.sub(r"[ \t]+", " ", strip_ansi(line)).strip()


def parse_event_time(raw_ts: str) -> str:
    tz_name = os.environ.get("TR_LOCAL_TIMEZONE", "America/Denver")
    local_tz = ZoneInfo(tz_name)
    dt = datetime.strptime(raw_ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=local_tz)
    return dt.astimezone(timezone.utc).isoformat()


def post_activity_event(path: str, payload: dict) -> None:
    api_url = os.environ.get("VTT_API_URL", "http://127.0.0.1:8080").rstrip("/")
    api_key = os.environ.get("VTT_API_KEY") or os.environ.get("API_KEY", "change-me")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"tr-encrypted-relay: POST failed ({exc.code}): {detail}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"tr-encrypted-relay: POST failed: {exc.reason}", file=sys.stderr)


def maybe_auto_add_unknown(talkgroup: int) -> None:
    global _csv_path
    if not _auto_add:
        return
    if talkgroup in _seen_unknown_tgs:
        return
    _seen_unknown_tgs.add(talkgroup)
    if _csv_path is None:
        return
    try:
        added = ensure_unknown_placeholder(_csv_path, talkgroup)
    except OSError as exc:
        print(
            f"tr-encrypted-relay: failed to update talk_groups.csv: {exc}",
            file=sys.stderr,
        )
        return
    if added:
        print(
            f"tr-encrypted-relay: added Unknown {talkgroup} to {_csv_path} "
            "(restart Trunk Recorder to record clear traffic)",
            file=sys.stderr,
        )


def handle_line(line: str) -> None:
    cleaned = normalize_line(line)
    match = SKIPPED_LINE.search(cleaned)
    if not match:
        return

    event_time = parse_event_time(match.group("ts"))
    talkgroup = int(match.group("talkgroup"))
    base = {
        "system_name": match.group("system"),
        "talkgroup": talkgroup,
        "freq": float(match.group("freq")),
        "event_time": event_time,
    }
    reason = match.group("reason")

    encrypted = ENCRYPTED_SRC.match(reason)
    if encrypted:
        src_raw = encrypted.group("src")
        src = int(src_raw) if src_raw is not None else -1
        post_activity_event(
            "/events/encrypted",
            {**base, "src": src},
        )
        return

    if UNKNOWN_TG.match(reason):
        post_activity_event("/events/unknown-talkgroup", base)
        maybe_auto_add_unknown(talkgroup)


def main() -> int:
    global _csv_path, _auto_add
    load_env()
    _auto_add = env_flag("TR_AUTO_ADD_UNKNOWN_TG", default=True)
    if _auto_add:
        config_json = os.environ.get("TR_CONFIG_JSON", "").strip() or None
        _csv_path = resolve_talkgroups_csv(config_path=config_json)
        print(
            f"tr-encrypted-relay: auto-add unknown TGs enabled → {_csv_path}",
            file=sys.stderr,
        )
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
        handle_line(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
