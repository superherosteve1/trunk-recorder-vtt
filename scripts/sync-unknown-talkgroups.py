#!/usr/bin/env python3
"""Backfill talk_groups.csv with Unknown placeholders from discovered TGs.

Sources (first that works):
  1. VTT API  GET /calls?status=unknown_talkgroup
  2. Optional local SQLite (--sqlite), e.g. Docker volume copy of calls.db

Usage (from repo root):
  ./scripts/sync-unknown-talkgroups.py --dry-run
  ./scripts/sync-unknown-talkgroups.py --min-hits 3
  ./scripts/sync-unknown-talkgroups.py --sqlite /path/to/calls.db

Environment:
  VTT_API_URL / VTT_API_KEY (or API_KEY)
  TR_TALKGROUPS_CSV / TR_CONFIG_JSON
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from talkgroups_csv import (  # noqa: E402
    ensure_unknown_placeholder,
    load_talkgroup_ids,
    resolve_talkgroups_csv,
)


def load_env() -> None:
    env_file = SCRIPT_DIR.parent / ".env"
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
            "TR_TALKGROUPS_CSV",
            "TR_CONFIG_JSON",
        }:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def fetch_unknown_counts_from_api(*, page_size: int = 1000) -> Counter[int]:
    api_url = os.environ.get("VTT_API_URL", "http://127.0.0.1:8080").rstrip("/")
    api_key = os.environ.get("VTT_API_KEY") or os.environ.get("API_KEY", "change-me")
    counts: Counter[int] = Counter()
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "status": "unknown_talkgroup",
                "limit": page_size,
                "offset": offset,
            }
        )
        request = urllib.request.Request(
            f"{api_url}/calls?{query}",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        calls = payload.get("calls") or []
        if not calls:
            break
        for call in calls:
            tg = call.get("talkgroup")
            if tg is None:
                continue
            try:
                counts[int(tg)] += 1
            except (TypeError, ValueError):
                continue
        if len(calls) < page_size:
            break
        offset += page_size
    return counts


def fetch_unknown_counts_from_sqlite(db_path: Path) -> Counter[int]:
    counts: Counter[int] = Counter()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT talkgroup, COUNT(*) AS hits
            FROM calls
            WHERE status = 'unknown_talkgroup' AND talkgroup IS NOT NULL
            GROUP BY talkgroup
            """
        )
        for talkgroup, hits in rows:
            counts[int(talkgroup)] = int(hits)
    finally:
        conn.close()
    return counts


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Append Unknown placeholders for discovered talkgroups missing from CSV"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to talk_groups.csv (default: TR_TALKGROUPS_CSV / config.json / config/talk_groups.csv)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Trunk Recorder config.json used to locate talkgroupsFile",
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=None,
        help="Optional local calls.db (used instead of API when provided)",
    )
    parser.add_argument(
        "--min-hits",
        type=int,
        default=1,
        help="Only add talkgroups seen at least N times (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added without writing",
    )
    args = parser.parse_args()

    csv_path = resolve_talkgroups_csv(csv_path=args.csv, config_path=args.config)
    print(f"sync-unknown-talkgroups: csv={csv_path}", file=sys.stderr)

    try:
        if args.sqlite is not None:
            counts = fetch_unknown_counts_from_sqlite(args.sqlite.expanduser().resolve())
            print(
                f"sync-unknown-talkgroups: source=sqlite ({args.sqlite}) "
                f"distinct={len(counts)}",
                file=sys.stderr,
            )
        else:
            counts = fetch_unknown_counts_from_api()
            print(
                f"sync-unknown-talkgroups: source=api distinct={len(counts)}",
                file=sys.stderr,
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"sync-unknown-talkgroups: API failed ({exc.code}): {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"sync-unknown-talkgroups: API failed: {exc.reason}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"sync-unknown-talkgroups: failed to read source: {exc}", file=sys.stderr)
        return 1

    existing = load_talkgroup_ids(csv_path)
    candidates = sorted(
        tg for tg, hits in counts.items() if hits >= args.min_hits and tg not in existing
    )

    if not candidates:
        print("sync-unknown-talkgroups: nothing to add")
        return 0

    added: list[int] = []
    for tg in candidates:
        hits = counts[tg]
        if args.dry_run:
            print(f"would add Unknown {tg} (hits={hits})")
            added.append(tg)
            continue
        try:
            if ensure_unknown_placeholder(csv_path, tg):
                print(f"added Unknown {tg} (hits={hits})")
                added.append(tg)
            else:
                print(f"skipped {tg} (already present)")
        except OSError as exc:
            print(f"failed to add {tg}: {exc}", file=sys.stderr)
            return 1

    action = "would add" if args.dry_run else "added"
    print(f"sync-unknown-talkgroups: {action} {len(added)} talkgroup(s)")
    if added and not args.dry_run:
        print(
            "Restart Trunk Recorder so it reloads talk_groups.csv and can record clear traffic.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
