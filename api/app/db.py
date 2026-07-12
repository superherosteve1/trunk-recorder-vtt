"""Dual-mode DB connections: SQLite (default) or Postgres when DATABASE_URL is set."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger(__name__)

_pg_pool = None


def using_postgres() -> bool:
    return bool((settings.database_url or "").strip())


def pg_schema() -> str:
    return (settings.database_schema or "trunk-recorder-oltp").strip() or "public"


def quote_ident(name: str) -> str:
    """Quote a Postgres identifier (needed for hyphenated schema names)."""
    return '"' + name.replace('"', '""') + '"'


def apply_search_path(conn: Any) -> None:
    schema = pg_schema()
    conn.execute(f"SET search_path TO {quote_ident(schema)}, public")


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    from psycopg_pool import ConnectionPool
    from psycopg.rows import dict_row

    url = settings.database_url.strip()
    schema = pg_schema()

    # Do not SET search_path in pool configure(): with autocommit=False that
    # leaves the connection INTRANS and psycopg_pool discards it. search_path
    # is applied on each checkout in get_db().
    _pg_pool = ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=True,
    )
    logger.info("Opened Postgres connection pool (schema=%s)", schema)
    return _pg_pool


def close_db_pools() -> None:
    global _pg_pool
    if _pg_pool is not None:
        _pg_pool.close()
        _pg_pool = None


def qmark_to_percent(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to psycopg ``%s`` (no literal ? in our SQL)."""
    return sql.replace("?", "%s")


class _CursorProxy:
    """Unify sqlite3 / psycopg cursor: fetchone/fetchall + lastrowid via RETURNING."""

    def __init__(self, cursor: Any, *, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class _ConnProxy:
    """Connection wrapper that accepts ``?`` placeholders on both backends."""

    def __init__(self, conn: Any, *, postgres: bool):
        self._conn = conn
        self.postgres = postgres

    def execute(self, sql: str, params: Any = ()) -> _CursorProxy:
        if self.postgres:
            sql = qmark_to_percent(sql)
            cur = self._conn.execute(sql, params or ())
            # psycopg3 returns the cursor from execute
            return _CursorProxy(cur)
        cur = self._conn.execute(sql, params or ())
        return _CursorProxy(cur, lastrowid=getattr(cur, "lastrowid", None))

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


@contextmanager
def get_db() -> Iterator[_ConnProxy]:
    if using_postgres():
        pool = _get_pg_pool()
        with pool.connection() as conn:
            apply_search_path(conn)
            proxy = _ConnProxy(conn, postgres=True)
            try:
                yield proxy
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return

    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    proxy = _ConnProxy(conn, postgres=False)
    try:
        yield proxy
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def serialize_value(value: Any) -> Any:
    """Normalize DB values for JSON/API (TIMESTAMPTZ → Zulu string, bool → 0/1)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        raw = row
    else:
        raw = dict(row)
    return {key: serialize_value(val) for key, val in raw.items()}


def since_expr(amount: int, unit: str) -> str:
    """SQL expression for UTC now minus interval (unit: minutes|hours|days)."""
    amount = int(amount)
    unit = unit.lower().rstrip("s") + "s"  # minute -> minutes
    if using_postgres():
        return f"NOW() - INTERVAL '{amount} {unit}'"
    # SQLite accepts singular in datetime modifiers: '-15 minutes'
    return f"datetime('now', '-{amount} {unit}')"


def insert_returning_id(conn: _ConnProxy, sql: str, params: tuple | list) -> int:
    """Run INSERT and return new id (RETURNING on Postgres, lastrowid on SQLite)."""
    if conn.postgres:
        sql_ret = sql.rstrip().rstrip(";") + " RETURNING id"
        row = conn.execute(sql_ret, params).fetchone()
        if not row:
            raise RuntimeError("INSERT RETURNING id returned no row")
        return int(row["id"] if isinstance(row, dict) else row[0])
    cur = conn.execute(sql, params)
    return int(cur.lastrowid)
