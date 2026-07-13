#!/usr/bin/env python3
"""Print the resolved Whisper prompt (base + jargon file, trimmed to 224 tokens).

Reads WHISPER_PROMPT, DATA_DIR, WHISPER_JARGON_PATH, WHISPER_PROMPT_MAX_TOKENS
from the environment (same as the API). No pydantic dependency — safe on the
Trunk Recorder edge host for upload-transcribed.sh.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.whisper_prompt import (  # noqa: E402
    WHISPER_PROMPT_MAX_TOKENS,
    build_whisper_prompt,
    resolve_whisper_jargon_path,
)

_DEFAULT_BASE = (
    "Police fire EMS dispatch scanner radio. "
    "10-4, copy, en route, responding, medic, unit, code."
)


def main() -> int:
    base = os.environ.get("WHISPER_PROMPT", _DEFAULT_BASE).strip() or _DEFAULT_BASE
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "config")))
    explicit_raw = os.environ.get("WHISPER_JARGON_PATH", "").strip()
    explicit = Path(explicit_raw) if explicit_raw else None
    try:
        max_tokens = int(os.environ.get("WHISPER_PROMPT_MAX_TOKENS", WHISPER_PROMPT_MAX_TOKENS))
    except ValueError:
        max_tokens = WHISPER_PROMPT_MAX_TOKENS

    path = resolve_whisper_jargon_path(data_dir=data_dir, explicit=explicit)
    prompt = build_whisper_prompt(
        base=base,
        jargon_path=path,
        max_tokens=max(1, max_tokens),
    )
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
