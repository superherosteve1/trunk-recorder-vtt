"""Build Whisper initial prompts from env base text + optional jargon file.

OpenAI Whisper uses only the final 224 tokens of the prompt. We append jargon
after the base and trim from the start when over budget so local spellings at
the end are preserved. See:
https://developers.openai.com/cookbook/examples/whisper_prompting_guide
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

WHISPER_PROMPT_MAX_TOKENS = 224
# Conservative chars/token for English dispatch text (Whisper multilingual tokenizer).
_CHARS_PER_TOKEN = 3.2

_jargon_cache: dict[Path, tuple[float | None, list[str]]] = {}


def resolve_whisper_jargon_path(*, data_dir: Path, explicit: Path | None = None) -> Path:
    """Prefer /data/whisper-jargon.txt; fall back to repo config/."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    primary = data_dir / "whisper-jargon.txt"
    if primary.is_file():
        return primary
    repo_fallback = Path(__file__).resolve().parents[2] / "config" / "whisper-jargon.txt"
    if repo_fallback.is_file():
        return repo_fallback
    example = Path(__file__).resolve().parents[2] / "config" / "whisper-jargon.example.txt"
    if example.is_file():
        return example
    return primary


def _estimate_tokens(text: str) -> int:
    text = text.strip()
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN + 0.999))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def load_jargon_terms(path: Path, *, force: bool = False) -> list[str]:
    """Load non-comment lines from a jargon file (one term/phrase per line)."""
    if not path.is_file():
        _jargon_cache.pop(path, None)
        return []

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None

    cached = _jargon_cache.get(path)
    if not force and cached is not None and cached[0] == mtime:
        return list(cached[1])

    terms: list[str] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Could not read whisper jargon file %s: %s", path, exc)
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        term = line.strip(" ,")
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)

    _jargon_cache[path] = (mtime, terms)
    return terms


def build_whisper_prompt(
    *,
    base: str,
    jargon_path: Path | None = None,
    jargon_terms: list[str] | None = None,
    max_tokens: int = WHISPER_PROMPT_MAX_TOKENS,
) -> str:
    """Merge base prompt + jargon glossary; trim from the start to fit token budget."""
    base = _normalize_whitespace(base)
    raw_terms = list(jargon_terms or [])
    if jargon_path is not None:
        raw_terms = load_jargon_terms(jargon_path)

    glossary: list[str] = []
    style_examples: list[str] = []
    for term in raw_terms:
        if term.endswith(".") and len(term.split()) >= 4:
            style_examples.append(term.rstrip("."))
        elif len(term.split()) >= 6 and not term.endswith("."):
            style_examples.append(term)
        else:
            glossary.append(term)

    if not base and not glossary and not style_examples:
        return ""

    parts: list[str] = []
    if base:
        parts.append(base.rstrip(". "))
    if glossary:
        parts.append("Glossary: " + ", ".join(glossary))
    parts.extend(style_examples)

    prompt = _normalize_whitespace(". ".join(parts) + ".")
    if _estimate_tokens(prompt) <= max_tokens:
        return prompt

    # Whisper keeps the *final* tokens — drop from the front until we fit.
    while prompt and _estimate_tokens(prompt) > max_tokens:
        if ". " in prompt:
            prompt = prompt.split(". ", 1)[1].strip()
            if not prompt.endswith("."):
                prompt += "."
            continue
        # Single long chunk: keep suffix by characters.
        max_chars = int(max_tokens * _CHARS_PER_TOKEN)
        prompt = prompt[-max_chars:].lstrip(" ,.")
        break

    trimmed = _normalize_whitespace(prompt)
    if trimmed and not trimmed.endswith("."):
        trimmed += "."
    return trimmed
