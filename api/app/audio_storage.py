"""Compress stored call audio after transcription."""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def audio_media_type(path: Path | str) -> str:
    suffix = Path(path).suffix.lower()
    return _MEDIA_TYPES.get(suffix, "application/octet-stream")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def compress_call_audio(wav_path: Path) -> Path | None:
    """Convert a WAV to the configured compressed format and remove the WAV.

    Returns the new path on success, or None if compression is disabled/skipped.
    Safe under concurrent callers (early ingest compress + worker): writes a
    unique temp file, never deletes another writer's finished output on failure.
    """
    if not settings.audio_compress:
        return None
    if wav_path.suffix.lower() != ".wav":
        return None
    if not ffmpeg_available():
        logger.warning("AUDIO_COMPRESS enabled but ffmpeg is not installed")
        return None

    fmt = (settings.audio_format or "mp3").strip().lower()
    if fmt not in {"mp3", "ogg", "opus"}:
        logger.warning("Unsupported AUDIO_FORMAT %r; leaving %s as WAV", fmt, wav_path.name)
        return None

    bitrate = (settings.audio_bitrate or "32k").strip()
    out_path = wav_path.with_suffix(f".{fmt}")
    if out_path.resolve() == wav_path.resolve():
        return None

    # Another compress (early ingest vs worker) already finished.
    if out_path.is_file() and out_path.stat().st_size > 0:
        try:
            if wav_path.is_file():
                wav_path.unlink()
        except OSError as exc:
            logger.warning(
                "Compressed sibling exists for %s but could not delete WAV: %s",
                wav_path.name,
                exc,
            )
        return out_path

    if not wav_path.is_file():
        # WAV gone; prefer an existing compressed sibling over failing.
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        return None

    tmp_path = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex[:8]}.tmp{out_path.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-vn",
        "-ac",
        "1",
    ]
    if fmt == "mp3":
        cmd.extend(["-codec:a", "libmp3lame", "-b:a", bitrate])
    elif fmt == "ogg":
        cmd.extend(["-codec:a", "libopus", "-b:a", bitrate])
    else:  # opus in .opus container
        cmd.extend(["-codec:a", "libopus", "-b:a", bitrate])

    cmd.append(str(tmp_path))

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.error("Failed compressing %s: %s", wav_path.name, exc)
        tmp_path.unlink(missing_ok=True)
        # Peer may have finished while we failed.
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        return None

    if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
        tmp_path.unlink(missing_ok=True)
        logger.error("Compression produced empty file for %s", wav_path.name)
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        return None

    try:
        tmp_path.replace(out_path)
    except OSError as exc:
        logger.error("Failed publishing compressed %s: %s", out_path.name, exc)
        tmp_path.unlink(missing_ok=True)
        if out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
        return None

    try:
        if wav_path.is_file():
            wav_path.unlink()
    except OSError as exc:
        logger.warning("Compressed %s but could not delete WAV: %s", wav_path.name, exc)

    logger.info(
        "Compressed %s -> %s (%d bytes)",
        wav_path.name,
        out_path.name,
        out_path.stat().st_size,
    )
    return out_path
