#!/usr/bin/env python3
"""One-shot migrate of calls rows from SQLite calls.db into Postgres.

Preserves primary-key IDs and maps TEXT timestamps / has_alert 0|1 into
TIMESTAMPTZ / boolean. Safe to re-run: ON CONFLICT (id) DO NOTHING.

Usage (from repo root, with network access to both DBs):

  # Put DATABASE_URL / DATABASE_SCHEMA in repo .env (recommended on Mac), then:
  ./scripts/migrate-sqlite-to-postgres.py /path/to/calls.db

  # Or export for this shell only:
  export DATABASE_URL='postgresql://vtt:PASSWORD@192.168.1.162:2665/vtt'
  export DATABASE_SCHEMA=trunk-recorder-oltp
  ./scripts/migrate-sqlite-to-postgres.py /path/to/calls.db

Prefer migrating a *copy* of calls.db first, then a short API pause for the
production file. After success, set DATABASE_URL on the API and roll out.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = (
    "id",
    "created_at",
    "updated_at",
    "status",
    "system_name",
    "talkgroup",
    "talkgroup_tag",
    "src",
    "src_tag",
    "freq",
    "call_length",
    "wav_path",
    "json_path",
    "metadata_json",
    "transcript",
    "backend_used",
    "error_message",
    "retry_count",
    "has_alert",
)


def parse_ts(value: object) -> datetime:
    """Parse SQLite TEXT / numeric timestamps into aware UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    text = str(value).strip()
    if not text:
        return datetime.now(timezone.utc)
    text = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Truncate to seconds if fractional junk remains
        parsed = datetime.fromisoformat(text[:19])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def row_tuple(row: sqlite3.Row) -> tuple:
    return (
        int(row["id"]),
        parse_ts(row["created_at"]),
        parse_ts(row["updated_at"]),
        row["status"],
        row["system_name"],
        row["talkgroup"],
        row["talkgroup_tag"],
        row["src"],
        row["src_tag"],
        row["freq"],
        row["call_length"],
        row["wav_path"] or "",
        row["json_path"],
        row["metadata_json"],
        row["transcript"],
        row["backend_used"],
        row["error_message"],
        int(row["retry_count"] or 0),
        as_bool(row["has_alert"] if "has_alert" in row.keys() else 0),
    )


def load_env() -> None:
    """Load DATABASE_* from repo .env into os.environ if not already set."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in {"DATABASE_URL", "DATABASE_SCHEMA", "DATA_DIR", "SQLITE_PATH"}:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sqlite_path",
        nargs="?",
        default=None,
        help="Path to calls.db (or use --sqlite / DATA_DIR)",
    )
    parser.add_argument(
        "--sqlite",
        dest="sqlite_opt",
        default=None,
        help="Path to SQLite calls.db",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres URL (default: DATABASE_URL env)",
    )
    parser.add_argument(
        "--schema",
        default=os.environ.get("DATABASE_SCHEMA", "trunk-recorder-oltp"),
        help='Postgres schema containing calls (default: trunk-recorder-oltp)',
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows and parse timestamps only; do not write",
    )
    args = parser.parse_args()

    sqlite_path = Path(
        args.sqlite_opt
        or args.sqlite_path
        or os.environ.get("SQLITE_PATH")
        or (Path(os.environ.get("DATA_DIR", "/data")) / "calls.db")
    )
    database_url = (args.database_url or "").strip()
    if not database_url:
        print("DATABASE_URL is required (env or --database-url)", file=sys.stderr)
        return 2
    if "://" not in database_url or not database_url.lower().startswith(
        ("postgresql://", "postgres://")
    ):
        print(
            "DATABASE_URL must look like:\n"
            "  postgresql://USER:PASSWORD@HOST:PORT/DBNAME\n"
            "Common mistakes: typo 'postgressql', missing '://', or shell-eating '!' in the password.\n"
            "URL-encode special password chars (e.g. ! → %21).",
            file=sys.stderr,
        )
        return 2
    if not sqlite_path.is_file():
        print(f"SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    try:
        import psycopg
    except ImportError:
        print(
            "psycopg is required. From repo: pip install 'psycopg[binary,pool]==3.2.6'",
            file=sys.stderr,
        )
        return 2

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        src_count = int(src.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
    except sqlite3.Error as exc:
        print(f"Failed reading SQLite calls table: {exc}", file=sys.stderr)
        return 1

    print(f"SQLite: {sqlite_path} ({src_count} rows)")
    print(f"Postgres: {database_url.split('@')[-1] if '@' in database_url else '(url)'}")
    schema = (args.schema or "trunk-recorder-oltp").strip() or "public"
    schema_ident = '"' + schema.replace('"', '""') + '"'
    print(f"Schema: {schema}")

    if args.dry_run:
        sample = src.execute(
            "SELECT id, created_at, updated_at, has_alert FROM calls LIMIT 3"
        ).fetchall()
        for row in sample:
            print(
                f"  sample id={row['id']} created={parse_ts(row['created_at']).isoformat()} "
                f"has_alert={as_bool(row['has_alert'] if 'has_alert' in row.keys() else 0)}"
            )
        print("Dry run only — no writes.")
        src.close()
        return 0

    col_list = ", ".join(COLUMNS)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    insert_sql = f"""
        INSERT INTO calls ({col_list})
        OVERRIDING SYSTEM VALUE
        VALUES ({placeholders})
        ON CONFLICT (id) DO NOTHING
    """

    inserted = 0
    with psycopg.connect(database_url) as dst:
        dst.execute(f"SET search_path TO {schema_ident}, public")
        before = int(dst.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
        print(f"Postgres before: {before} rows")

        cur = src.execute(f"SELECT {col_list} FROM calls ORDER BY id ASC")
        while True:
            rows = cur.fetchmany(args.batch_size)
            if not rows:
                break
            batch = [row_tuple(row) for row in rows]
            with dst.cursor() as pg_cur:
                for values in batch:
                    pg_cur.execute(insert_sql, values)
                    if pg_cur.rowcount:
                        inserted += 1
            dst.commit()
            # search_path can be reset by rollback; re-apply after each commit batch
            dst.execute(f"SET search_path TO {schema_ident}, public")
            print(f"  processed through id={batch[-1][0]} ({len(batch)} rows)")

        after = int(dst.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
        seq_val = dst.execute(
            """
            SELECT setval(
              pg_get_serial_sequence('calls', 'id'),
              COALESCE((SELECT MAX(id) FROM calls), 1)
            )
            """
        ).fetchone()[0]
        dst.commit()

        print(f"Postgres after:  {after} rows")
        print(f"Inserted this run: {inserted}")
        print(f"Already present / skipped: {src_count - inserted}")
        print(f"calls_id_seq set to: {seq_val}")
        print("Done. Point the API at DATABASE_URL and roll out.")

    src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
