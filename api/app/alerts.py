"""Transcript keyword alerts shared by API list filters and the dashboard."""

from __future__ import annotations

import re

TRANSCRIPT_ALERT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b("
            r"working fire|structure fire|building fire|house fire|garage fire|"
            r"vehicle fire|car fire|brush fire|wildfire|grass fire|"
            r"smoke showing|flames|fully engulfed|reported fire|confirmed fire|"
            r"active fire|fire at|on fire|smoke"
            r")\b",
            re.I,
        ),
        "🔥",
    ),
    (
        re.compile(
            r"\b(mental health|psychiatric|psych|5150|suicidal|suicide|behavioral|crisis)\b",
            re.I,
        ),
        "🧠",
    ),
    (re.compile(r"\b(stabbing|stabbed|stab wound|knife wound)\b", re.I), "🔪"),
    (
        re.compile(r"\b(gunshot|gun shot|shots fired|shooting|shooter|gsw)\b", re.I),
        "💥",
    ),
    (
        re.compile(
            r"\b("
            r"overdose|overdosing|overdosed|"
            r"od(?:ing|ed)?|"
            r"narcan|naloxone|"
            r"fentanyl(?:\s+overdose)?|"
            r"heroin(?:\s+overdose)?"
            r")\b",
            re.I,
        ),
        "💉",
    ),
    (
        re.compile(
            r"\b("
            r"doa|dead on arrival|"
            r"deceased|fatality|"
            r"(?<!non-)fatal(?:ity)?|"
            r"code black|"
            r"obvious death|confirmed death|time of death|"
            r"passed away|pronounced dead|"
            r"body found|found deceased"
            r")\b",
            re.I,
        ),
        "☠️",
    ),
    (
        re.compile(
            r"\b("
            r"trauma(?:tic)?(?:\s+injur(?:y|ies))?|"
            r"injur(?:y|ies|ed)|"
            r"patient down|unconscious|cardiac arrest|chest pain|"
            r"mvc|mva|motor vehicle accident|motor vehicle crash"
            r")\b",
            re.I,
        ),
        "🩹",
    ),
)


def transcript_has_alert(transcript: str | None) -> bool:
    if not transcript:
        return False
    return any(pattern.search(transcript) for pattern, _ in TRANSCRIPT_ALERT_PATTERNS)


def transcript_alert_emojis(transcript: str | None) -> str:
    if not transcript:
        return ""
    seen: set[str] = set()
    emojis: list[str] = []
    for pattern, emoji in TRANSCRIPT_ALERT_PATTERNS:
        if emoji in seen:
            continue
        if pattern.search(transcript):
            seen.add(emoji)
            emojis.append(emoji)
    return "".join(emojis)
