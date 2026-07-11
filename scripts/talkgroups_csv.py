#!/usr/bin/env python3
"""Helpers for reading/appending Trunk Recorder talk_groups.csv placeholders.

Placeholder row format (matches existing Unknown convention):
  {id},Unknown {id},D,Unknown {id},Interop,Unknown,
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "config" / "talk_groups.csv"
DEFAULT_CONFIG = REPO_ROOT / "config.json"
PLACEHOLDER_HEADER = [
    "Decimal",
    "Alpha Tag",
    "Mode",
    "Description",
    "Tag",
    "Category",
    "Priority",
]


def resolve_talkgroups_csv(
    *,
    csv_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    """Resolve talk_groups.csv from explicit path, env, config.json, or default."""
    if csv_path:
        return Path(csv_path).expanduser().resolve()

    env_path = os.environ.get("TR_TALKGROUPS_CSV", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    cfg_file = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG
    if not cfg_file.is_absolute():
        cfg_file = (Path.cwd() / cfg_file).resolve()
    if cfg_file.is_file():
        try:
            payload = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for system in payload.get("systems") or []:
            tg_file = (system or {}).get("talkgroupsFile")
            if not tg_file:
                continue
            candidate = Path(tg_file)
            if not candidate.is_absolute():
                candidate = (cfg_file.parent / candidate).resolve()
            return candidate

    return DEFAULT_CSV.resolve()


def load_talkgroup_ids(csv_path: Path) -> set[int]:
    """Return the set of Decimal talkgroup IDs present in the CSV."""
    ids: set[int] = set()
    if not csv_path.is_file():
        return ids
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "Decimal" not in reader.fieldnames:
            raise ValueError(f"{csv_path}: missing Decimal column")
        for row in reader:
            raw = (row.get("Decimal") or "").strip()
            if not raw:
                continue
            try:
                ids.add(int(raw))
            except ValueError:
                continue
    return ids


def placeholder_row(talkgroup_id: int) -> dict[str, str]:
    label = f"Unknown {talkgroup_id}"
    return {
        "Decimal": str(talkgroup_id),
        "Alpha Tag": label,
        "Mode": "D",
        "Description": label,
        "Tag": "Interop",
        "Category": "Unknown",
        "Priority": "",
    }


def ensure_unknown_placeholder(csv_path: Path, talkgroup_id: int) -> bool:
    """Append an Unknown placeholder row if talkgroup_id is missing.

    Returns True when a row was added, False if it already existed.
    """
    if talkgroup_id < 1:
        raise ValueError(f"invalid talkgroup id: {talkgroup_id}")

    # Fast path without lock; re-check under lock via append-only uniqueness.
    if talkgroup_id in load_talkgroup_ids(csv_path):
        return False

    # Re-check under exclusive lock to avoid duplicate appends from concurrent writers.
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a+", newline="", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing_text = handle.read()
            ids: set[int] = set()
            fieldnames = list(PLACEHOLDER_HEADER)
            if existing_text:
                handle.seek(0)
                reader = csv.DictReader(handle)
                if reader.fieldnames:
                    fieldnames = list(reader.fieldnames)
                for row in reader:
                    raw = (row.get("Decimal") or "").strip()
                    if not raw:
                        continue
                    try:
                        ids.add(int(raw))
                    except ValueError:
                        continue
            if talkgroup_id in ids:
                return False

            row = placeholder_row(talkgroup_id)
            if existing_text and not existing_text.endswith("\n"):
                handle.seek(0, os.SEEK_END)
                handle.write("\n")
            else:
                handle.seek(0, os.SEEK_END)

            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                lineterminator="\n",
                extrasaction="ignore",
            )
            if not existing_text:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
            return True
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    path = resolve_talkgroups_csv()
    ids = load_talkgroup_ids(path)
    print(f"{path}: {len(ids)} talkgroups", file=sys.stderr)
