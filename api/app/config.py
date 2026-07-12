from enum import Enum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SITE_NOTICE = (
    "No decryption of encrypted communications takes place. "
    "Encrypted calls are logged as metadata only — recording or decrypting "
    "encrypted radio you are not authorized to receive is generally a federal felony. "
    "Access to encrypted audio must be requested through the proper channels "
    "(for example, a public-records request to the responsible agency). "
    "Non-encrypted calls are recorded and transcribed; accuracy is not 100%."
)

RECORDS_BUTTON_NOTICE = (
    "A button is provided on each encrypted row to copy everything an agency "
    "administrator would need to quickly locate the call."
)


class TranscriptionBackend(str, Enum):
    OPENAI = "openai"
    FASTER_WHISPER = "faster_whisper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: str = "change-me"
    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080

    # openai | faster_whisper
    transcription_backend: TranscriptionBackend = TranscriptionBackend.OPENAI
    transcription_fallback: bool = True

    # OpenAI-compatible endpoint (OpenAI API, whisper.cpp server, etc.)
    whisper_api_url: str = "http://host.docker.internal:9000/v1/audio/transcriptions"
    whisper_api_key: str = ""
    whisper_model: str = "whisper-1"
    whisper_language: str = "en"
    whisper_prompt: str = (
        "Police fire EMS dispatch scanner radio. "
        "10-4, copy, en route, responding, medic, unit, code. "
        "Calls often end with local 24-hour time like 0945, 1200, 1244, 1259, 0100, 1730 "
        "— never dollar amounts like $12.44."
    )

    # faster-whisper HTTP server (POST multipart audio, JSON {"text": "..."} response)
    faster_whisper_api_url: str = "http://host.docker.internal:8000/transcribe"
    faster_whisper_language: str = "en"

    min_call_length: float = 2.0
    worker_poll_interval: float = 1.0
    transcription_timeout: float = 300.0
    max_retries: int = 3
    # When false (cloud archive API), POST /calls requires a transcript;
    # the in-process Whisper worker is not started.
    transcription_worker_enabled: bool = True

    # Defense in depth: reject POST /calls that look like encrypted voice.
    # Honest Trunk Recorder clients never upload encrypted WAVs.
    reject_encrypted_uploads: bool = True
    reject_encrypted_audio_entropy: bool = True
    # Shannon entropy of PCM bytes (max 8.0). Clear speech is typically lower;
    # encrypted/noise-like PCM trends toward random (~7.5+).
    encrypted_audio_entropy_threshold: float = 7.5

    trunk_recorder_config: Path = Path("/data/trunk-recorder.json")
    gis_dir: Path = Path("/data/gis")
    docs_dir: Path = Path("/data/docs")
    districts_config_path: Path = Path("/data/districts.json")

    # After transcription, recompress WAV for long-term storage (requires ffmpeg).
    audio_compress: bool = True
    audio_format: str = "mp3"  # mp3 | ogg | opus
    audio_bitrate: str = "32k"

    # Municipality / site branding (defaults preserve Denver/Aurora deploy)
    site_title: str = "Denver / Aurora Trunk Monitor"
    site_subtitle: str = "Trunk Recorder transcription dashboard"
    site_notice: str = DEFAULT_SITE_NOTICE
    site_show_records_help: bool = True

    # Encrypted-call public-records clipboard helper (CORA/FOIA/etc.)
    records_request_enabled: bool = True
    records_request_button_label: str = "CORA"
    records_request_title: str = (
        "CORA (Colorado Open Records Act) audio retrieval request"
    )
    # Used in "Requestor / {contact} contact"; falls back to button label when empty
    records_request_contact_label: str = "CORA"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "calls.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    def records_request_config(self) -> dict:
        button = (self.records_request_button_label or "Records").strip() or "Records"
        contact = (self.records_request_contact_label or button).strip() or button
        title = (self.records_request_title or "").strip() or (
            f"{button} audio retrieval request"
        )
        return {
            "enabled": bool(self.records_request_enabled),
            "button_label": button,
            "title": title,
            "contact_label": contact,
        }

    def site_branding(self) -> dict:
        records = self.records_request_config()
        notice = (self.site_notice or DEFAULT_SITE_NOTICE).strip() or DEFAULT_SITE_NOTICE
        if records["enabled"] and RECORDS_BUTTON_NOTICE not in notice:
            # Append button sentence unless the operator already included it in SITE_NOTICE.
            notice = f"{notice.rstrip()} {RECORDS_BUTTON_NOTICE}"
        return {
            "title": (self.site_title or "").strip() or "Trunk Monitor",
            "subtitle": (self.site_subtitle or "").strip()
            or "Trunk Recorder transcription dashboard",
            "notice": notice,
            "show_records_help": bool(self.site_show_records_help)
            and bool(records["enabled"]),
            "records_request": records,
        }


settings = Settings()
