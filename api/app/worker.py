import asyncio
import logging
from pathlib import Path

from app.audio_storage import compress_call_audio
from app.config import settings
from app.database import (
    claim_completed_wav_for_compression,
    claim_pending_call,
    mark_call_completed,
    mark_call_failed,
    update_call_audio_path,
)
from app.transcription import TranscriptionError, transcribe_audio

logger = logging.getLogger(__name__)


class TranscriptionWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="transcription-worker")
        logger.info("Transcription worker started")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task
            self._task = None
        logger.info("Transcription worker stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            call = claim_pending_call()
            if not call:
                # When idle, gradually compress older completed WAVs.
                if settings.audio_compress:
                    for _ in range(5):
                        if self._stop_event.is_set():
                            break
                        await asyncio.to_thread(self._compress_one_existing_wav)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=settings.worker_poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            call_id = call["id"]
            wav_path = Path(call["wav_path"])
            logger.info("Processing call %s (%s)", call_id, wav_path.name)

            if not wav_path.exists():
                # DB path may lag behind early WAV→MP3 compression.
                resolved = None
                for ext in (".mp3", ".ogg", ".opus", ".wav"):
                    alt = wav_path.with_suffix(ext)
                    if alt.is_file():
                        resolved = alt
                        break
                if resolved is None:
                    mark_call_failed(
                        call_id,
                        error_message=f"Audio file not found: {wav_path}",
                        increment_retry=False,
                    )
                    logger.error(
                        "Giving up on call %s; audio missing at %s",
                        call_id,
                        wav_path,
                    )
                    continue
                wav_path = resolved
                update_call_audio_path(call_id, wav_path=wav_path)

            try:
                # Compress before Whisper so the dashboard can play a small MP3
                # while transcription is still running (WAV on NFS is slow to start).
                play_path = wav_path
                if settings.audio_compress and wav_path.suffix.lower() == ".wav":
                    compressed = await asyncio.to_thread(compress_call_audio, wav_path)
                    if compressed is not None:
                        play_path = compressed
                        update_call_audio_path(call_id, wav_path=play_path)
                        logger.info(
                            "Compressed call %s for playback before transcription (%s)",
                            call_id,
                            play_path.name,
                        )

                transcript, backend_used = await transcribe_audio(play_path)
                mark_call_completed(
                    call_id,
                    transcript=transcript,
                    backend_used=backend_used,
                    wav_path=play_path,
                )
                logger.info(
                    "Completed call %s via %s (%d chars, audio=%s)",
                    call_id,
                    backend_used,
                    len(transcript),
                    play_path.name,
                )
            except TranscriptionError as exc:
                mark_call_failed(
                    call_id,
                    error_message=str(exc),
                    increment_retry=True,
                )
                logger.error("Failed call %s: %s", call_id, exc)
            except Exception as exc:
                mark_call_failed(
                    call_id,
                    error_message=f"Unexpected error: {exc}",
                    increment_retry=True,
                )
                logger.exception("Unexpected failure for call %s", call_id)

    def _compress_one_existing_wav(self) -> None:
        call = claim_completed_wav_for_compression()
        if not call:
            return
        call_id = call["id"]
        wav_path = Path(call["wav_path"])
        if not wav_path.is_file():
            for ext in (".mp3", ".ogg", ".opus"):
                alt = wav_path.with_suffix(ext)
                if alt.is_file():
                    update_call_audio_path(call_id, wav_path=alt)
                    return
            # Avoid retrying the same missing WAV forever.
            update_call_audio_path(call_id, wav_path=f"{wav_path}.missing")
            logger.warning(
                "Skipping compression for call %s; missing file %s",
                call_id,
                wav_path,
            )
            return
        compressed = compress_call_audio(wav_path)
        if compressed is None:
            return
        update_call_audio_path(call_id, wav_path=compressed)


worker = TranscriptionWorker()
