"""Application settings using Pydantic."""

from enum import Enum
from functools import lru_cache
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TranscriptionBackend(str, Enum):
    LOCAL = "local"
    OPENAI = "openai"


class SummarizationBackend(str, Enum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/sidekick.db"

    # Transcription
    transcription_backend: TranscriptionBackend = TranscriptionBackend.LOCAL

    # faster-whisper settings
    whisper_model_size: str = "base"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"

    # OpenAI API
    openai_api_key: str = ""

    # Anthropic API
    anthropic_api_key: str = ""

    # Summarization
    summarization_backend: SummarizationBackend = SummarizationBackend.OLLAMA

    # Ollama settings
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # OpenAI summarization
    openai_summarization_model: str = "gpt-4o-mini"

    # Anthropic summarization
    anthropic_summarization_model: str = "claude-3-haiku-20240307"

    # Audio settings
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    vad_aggressiveness: int = 2

    # Important marker duration (seconds)
    important_marker_duration: int = 60

    # Obsidian integration
    obsidian_vault_path: str = "/mnt/c/Users/ozzfa/Documents/Obsidian Vault"

    @field_validator("vad_aggressiveness")
    @classmethod
    def validate_vad_aggressiveness(cls, v: int) -> int:
        if v not in (0, 1, 2, 3):
            raise ValueError("vad_aggressiveness must be 0, 1, 2, or 3")
        return v

    @property
    def data_dir(self) -> Path:
        """Get the data directory path."""
        return Path("data")

    @property
    def db_path(self) -> Path:
        """Get the database file path."""
        # Extract path from SQLite URL
        url = self.database_url
        if url.startswith("sqlite"):
            # Handle sqlite+aiosqlite:///./data/sidekick.db format
            path_part = url.split("///")[-1]
            return Path(path_part)
        return self.data_dir / "sidekick.db"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
