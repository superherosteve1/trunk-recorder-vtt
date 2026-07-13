"""Transcript keyword alerts shared by API list filters and the dashboard."""

from __future__ import annotations

import re

# Gun / shooting — checked first; suppresses weaker fire hits in the same transcript.
SHOOTING_PATTERN = re.compile(
    r"\b("
    r"gunshots?|gun shots?|gun fire|gunfire|"
    r"shots? fired|shots? fire(?: at|d)?|"
    r"shootings?|shooters?|active shooters?|gsw|"
    r"stage(?:d)? for shots(?: fired)?"
    r")\b",
    re.I,
)

# Definite fire incidents — always alert even if shooting is mentioned elsewhere.
FIRE_HIGH_PATTERN = re.compile(
    r"\b("
    r"working fire|structure fire|building fire|house fire|garage fire|"
    r"vehicle fire|car fire|brush fire|wildfire|grass fire|"
    r"flames|fully engulfed|confirmed fire"
    r")\b",
    re.I,
)

# Softer fire language — skip when the transcript is primarily a shooting call.
FIRE_MED_PATTERN = re.compile(
    r"\b(reported fire|active fire|on fire|smoke showing)\b",
    re.I,
)

OTHER_ALERT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(mental health|psychiatric|psych|5150|suicidal|suicide|behavioral|crisis)\b",
            re.I,
        ),
        "🧠",
    ),
    (re.compile(r"\b(stabbing|stabbed|stab wound|knife wound)\b", re.I), "🔪"),
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


def _shooting_context(transcript: str) -> bool:
    return SHOOTING_PATTERN.search(transcript) is not None


def _fire_alert(transcript: str, *, shooting: bool) -> bool:
    if FIRE_HIGH_PATTERN.search(transcript):
        return True
    if shooting:
        return False
    return FIRE_MED_PATTERN.search(transcript) is not None


def transcript_alert_emojis(transcript: str | None) -> str:
    if not transcript:
        return ""
    shooting = _shooting_context(transcript)
    seen: set[str] = set()
    emojis: list[str] = []

    def add(emoji: str) -> None:
        if emoji in seen:
            return
        seen.add(emoji)
        emojis.append(emoji)

    if shooting:
        add("💥")
    if _fire_alert(transcript, shooting=shooting):
        add("🔥")
    for pattern, emoji in OTHER_ALERT_PATTERNS:
        if pattern.search(transcript):
            add(emoji)
    return "".join(emojis)


def transcript_has_alert(transcript: str | None) -> bool:
    if not transcript:
        return False
    if _shooting_context(transcript):
        return True
    if _fire_alert(transcript, shooting=False):
        return True
    return any(pattern.search(transcript) for pattern, _ in OTHER_ALERT_PATTERNS)
