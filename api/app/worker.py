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


def _resolve_audio_path(path: Path) -> Path | None:
    """Return an existing audio path, preferring compressed siblings."""
    if path.is_file():
        return path
    for ext in (".mp3", ".ogg", ".opus", ".wav"):
        alt = path.with_suffix(ext)
        if alt.is_file():
            return alt
    return None


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
            try:
                call = await asyncio.to_thread(claim_pending_call)
            except Exception:
                logger.exception("claim_pending_call failed; backing off")
                await asyncio.sleep(settings.worker_poll_interval)
                continue
            if not call:
                # When idle, gradually compress older completed WAVs.
                if settings.audio_compress:
                    for _ in range(5):
                        if self._stop_event.is_set():
                            break
                        try:
                            await asyncio.to_thread(self._compress_one_existing_wav)
                        except Exception:
                            logger.exception(
                                "Background WAV compression failed; will retry later"
                            )
                            break
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

            resolved = _resolve_audio_path(wav_path)
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
            if resolved != wav_path:
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
                    else:
                        # Early ingest compress may have won the race — re-resolve.
                        again = _resolve_audio_path(wav_path)
                        if again is None:
                            raise FileNotFoundError(wav_path)
                        play_path = again
                        if play_path != wav_path:
                            update_call_audio_path(call_id, wav_path=play_path)

                # Final existence check (peer compress may have replaced WAV).
                play_path = _resolve_audio_path(play_path) or play_path
                if not play_path.is_file():
                    raise FileNotFoundError(play_path)

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
            except FileNotFoundError as exc:
                # Soft retry: early compression raced and paths shifted.
                again = _resolve_audio_path(Path(call["wav_path"]))
                if again is not None:
                    update_call_audio_path(call_id, wav_path=again)
                    mark_call_failed(
                        call_id,
                        error_message=f"Audio path raced during compress; will retry ({again.name})",
                        increment_retry=True,
                    )
                    logger.warning(
                        "Call %s audio raced (%s); requeued with %s",
                        call_id,
                        exc,
                        again.name,
                    )
                else:
                    mark_call_failed(
                        call_id,
                        error_message=f"Audio file not found: {exc}",
                        increment_retry=False,
                    )
                    logger.error("Giving up on call %s; audio missing after race", call_id)
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
