from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="speech_", extra="ignore")

    api_key: str = ""
    allowed_origins: str = "http://localhost:8000"
    model_dir: Path = Path("models")
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_threads: int = Field(4, ge=1, le=64)
    kokoro_threads: int = Field(2, ge=1, le=64)
    max_sessions: int = Field(8, ge=1, le=100)
    session_seconds: int = Field(1800, ge=60, le=7200)
    queue_size: int = Field(8, ge=1, le=100)

    @property
    def origins(self):
        return {value.strip() for value in self.allowed_origins.split(",") if value.strip()}
