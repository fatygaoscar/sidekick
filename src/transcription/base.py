"""Abstract base interface for transcription engines."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator

import numpy as np


@dataclass
class TranscriptionResult:
    """Result from transcription engine."""

    text: str
    start_time: float
    end_time: float
    confidence: float | None = None
    language: str | None = None
    words: list[dict] | None = None  # Word-level timestamps if available


class TranscriptionEngine(ABC):
    """Abstract base class for transcription engines."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the engine name."""
        pass

    @property
    @abstractmethod
    def is_local(self) -> bool:
        """Check if the engine runs locally."""
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the transcription engine."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the transcription engine."""
        pass

    @abstractmethod
    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio data.

        Args:
            audio: Audio data as numpy array (float32, mono)
            sample_rate: Audio sample rate
            language: Language code (e.g., 'en') or None for auto-detect

        Returns:
            TranscriptionResult with text and metadata
        """
        pass

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[np.ndarray],
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> AsyncIterator[TranscriptionResult]:
        """
        Transcribe streaming audio.

        Default implementation processes chunks individually.
        Subclasses can override for true streaming support.

        Args:
            audio_stream: Async iterator of audio chunks
            sample_rate: Audio sample rate
            language: Language code or None for auto-detect

        Yields:
            TranscriptionResult for each processed chunk
        """
        async for audio_chunk in audio_stream:
            result = await self.transcribe(audio_chunk, sample_rate, language)
            if result.text.strip():
                yield result
