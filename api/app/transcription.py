import logging
from pathlib import Path

import httpx

from app.config import Settings, TranscriptionBackend, settings

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    pass


async def transcribe_openai(
    audio_path: Path,
    *,
    cfg: Settings = settings,
    client: httpx.AsyncClient | None = None,
) -> str:
    headers: dict[str, str] = {}
    if cfg.whisper_api_key:
        headers["Authorization"] = f"Bearer {cfg.whisper_api_key}"

    data = {
        "model": cfg.whisper_model,
        "language": cfg.whisper_language,
        "response_format": "json",
    }
    if cfg.whisper_prompt:
        data["prompt"] = cfg.whisper_prompt

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=cfg.transcription_timeout)

    try:
        with audio_path.open("rb") as audio_file:
            response = await client.post(
                cfg.whisper_api_url,
                headers=headers,
                data=data,
                files={"file": (audio_path.name, audio_file, "audio/wav")},
            )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("text", "").strip()
        if not text:
            raise TranscriptionError("OpenAI-compatible endpoint returned empty transcript")
        return text
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"OpenAI-compatible transcription failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()


async def transcribe_faster_whisper(
    audio_path: Path,
    *,
    cfg: Settings = settings,
    client: httpx.AsyncClient | None = None,
) -> str:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=cfg.transcription_timeout)

    try:
        with audio_path.open("rb") as audio_file:
            response = await client.post(
                cfg.faster_whisper_api_url,
                files={"audio": (audio_path.name, audio_file, "audio/wav")},
                data={"language": cfg.faster_whisper_language},
            )
        response.raise_for_status()
        payload = response.json()

        # Support common faster-whisper server response shapes
        if isinstance(payload, str):
            text = payload.strip()
        elif "text" in payload:
            text = str(payload["text"]).strip()
        elif "transcription" in payload:
            text = str(payload["transcription"]).strip()
        elif "segments" in payload and payload["segments"]:
            text = " ".join(
                segment.get("text", "").strip()
                for segment in payload["segments"]
                if segment.get("text")
            ).strip()
        else:
            raise TranscriptionError(
                f"Unrecognized faster-whisper response: {list(payload.keys())}"
            )

        if not text:
            raise TranscriptionError("faster-whisper endpoint returned empty transcript")
        return text
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()


async def transcribe_audio(
    audio_path: Path,
    *,
    cfg: Settings = settings,
) -> tuple[str, str]:
    backends: list[tuple[str, TranscriptionBackend]] = []

    if cfg.transcription_backend == TranscriptionBackend.OPENAI:
        backends.append(("openai", TranscriptionBackend.OPENAI))
        if cfg.transcription_fallback:
            backends.append(("faster_whisper", TranscriptionBackend.FASTER_WHISPER))
    else:
        backends.append(("faster_whisper", TranscriptionBackend.FASTER_WHISPER))
        if cfg.transcription_fallback:
            backends.append(("openai", TranscriptionBackend.OPENAI))

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=cfg.transcription_timeout) as client:
        for name, backend in backends:
            try:
                if backend == TranscriptionBackend.OPENAI:
                    text = await transcribe_openai(audio_path, cfg=cfg, client=client)
                else:
                    text = await transcribe_faster_whisper(audio_path, cfg=cfg, client=client)
                return text, name
            except TranscriptionError as exc:
                logger.warning("Backend %s failed for %s: %s", name, audio_path, exc)
                errors.append(f"{name}: {exc}")

    raise TranscriptionError("; ".join(errors))
