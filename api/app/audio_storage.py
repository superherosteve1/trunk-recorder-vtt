"""Compress stored call audio after transcription."""

from __future__ import annotations

import logging
import shutil
import subprocess
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
    """
    if not settings.audio_compress:
        return None
    if not wav_path.is_file():
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

    cmd.append(str(out_path))

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.error("Failed compressing %s: %s", wav_path.name, exc)
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        return None

    if not out_path.is_file() or out_path.stat().st_size <= 0:
        out_path.unlink(missing_ok=True)
        logger.error("Compression produced empty file for %s", wav_path.name)
        return None

    try:
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
