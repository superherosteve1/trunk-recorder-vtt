"""Reject encrypted voice uploads on POST /calls (defense in depth).

Honest Trunk Recorder clients never upload encrypted WAVs — they skip recording.
This gate blocks misconfigured or malicious feeders that still POST audio for
calls the control channel marked encrypted, or whose PCM looks like high-entropy
noise rather than speech.
"""

from __future__ import annotations

import audioop
import logging
import math
import struct
import wave
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Common Trunk Recorder / OpenMHz-style metadata keys that mean "this grant was encrypted".
_ENCRYPTED_TRUTH_KEYS = (
    "encrypted",
    "enc",
    "is_encrypted",
    "encryption",
)


def _as_boolish(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "encrypted"}:
        return True
    if text in {"0", "false", "no", "n", "off", "clear", "unencrypted"}:
        return False
    return None


def metadata_indicates_encrypted(metadata: dict[str, Any]) -> str | None:
    """Return a rejection reason if call JSON says the voice grant was encrypted."""
    for key in _ENCRYPTED_TRUTH_KEYS:
        if key not in metadata:
            continue
        flag = _as_boolish(metadata.get(key))
        if flag is True:
            return f"metadata.{key}=encrypted"
        if flag is False:
            # Explicit clear wins over weaker hints for this key.
            continue

    # Nested blobs some pipelines attach
    for nest_key in ("call", "meta", "p25", "encryption"):
        nested = metadata.get(nest_key)
        if isinstance(nested, dict):
            nested_reason = metadata_indicates_encrypted(nested)
            if nested_reason:
                return f"{nest_key}.{nested_reason}"

    # Non-clear P25 algorithm id when present (0x80 = unencrypted voice).
    for alg_key in ("algid", "alg_id", "algorithm_id", "encryption_algid"):
        if alg_key not in metadata:
            continue
        try:
            alg = int(str(metadata.get(alg_key)).strip(), 0)
        except (TypeError, ValueError):
            continue
        # 0 and 0x80 are treated as clear / no encryption in common P25 tooling.
        if alg not in (0, 0x80):
            return f"metadata.{alg_key}=0x{alg:02x} (non-clear)"

    return None


def _pcm_byte_entropy(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    counts = [0] * 256
    for b in pcm:
        counts[b] += 1
    length = len(pcm)
    entropy = 0.0
    for count in counts:
        if count == 0:
            continue
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def _read_wav_pcm_mono16(path: Path, *, max_seconds: float = 4.0) -> bytes | None:
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            frame_rate = handle.getframerate() or 1
            frames_wanted = int(frame_rate * max_seconds)
            raw = handle.readframes(min(handle.getnframes(), frames_wanted))
    except (wave.Error, OSError) as exc:
        logger.debug("WAV inspect failed for %s: %s", path, exc)
        return None

    if not raw:
        return None

    try:
        if channels > 1:
            raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
        if sample_width != 2:
            raw = audioop.lin2lin(raw, sample_width, 2)
    except audioop.error as exc:
        logger.debug("PCM convert failed for %s: %s", path, exc)
        return None
    return raw


def audio_looks_like_encrypted_noise(path: Path) -> str | None:
    """Heuristic: high byte-entropy PCM is unlikely to be clear speech.

    Only applied to WAV uploads. Compressed formats are skipped (metadata gate
    remains authoritative for those).
    """
    if path.suffix.lower() != ".wav":
        return None
    if not settings.reject_encrypted_audio_entropy:
        return None

    pcm = _read_wav_pcm_mono16(path)
    if pcm is None or len(pcm) < 4000:
        return None

    # Drop near-silence windows — silence is low entropy and not encryption.
    try:
        # RMS of int16 samples
        sample_count = len(pcm) // 2
        if sample_count <= 0:
            return None
        samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
        mean_square = sum(s * s for s in samples) / sample_count
        rms = math.sqrt(mean_square)
    except (struct.error, ValueError):
        return None

    if rms < 200:
        return None

    entropy = _pcm_byte_entropy(pcm)
    threshold = float(settings.encrypted_audio_entropy_threshold)
    if entropy >= threshold:
        return (
            f"audio entropy {entropy:.2f} >= {threshold:.2f} "
            "(likely encrypted/noise payload)"
        )
    return None


def reject_encrypted_upload(
    *,
    metadata: dict[str, Any],
    audio_path: Path | None = None,
) -> str | None:
    """Return rejection reason, or None if the upload may proceed."""
    if not settings.reject_encrypted_uploads:
        return None

    meta_reason = metadata_indicates_encrypted(metadata)
    if meta_reason:
        return meta_reason

    if audio_path is not None:
        audio_reason = audio_looks_like_encrypted_noise(audio_path)
        if audio_reason:
            return audio_reason

    return None
