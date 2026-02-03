"""Cloud transcription using OpenAI Whisper API."""

import io
from typing import Any

import numpy as np
from scipy.io import wavfile

from config.settings import get_settings

from .base import TranscriptionEngine, TranscriptionResult


class WhisperAPIEngine(TranscriptionEngine):
    """Transcription engine using OpenAI Whisper API."""

    def __init__(self, api_key: str | None = None) -> None:
        """
        Initialize OpenAI Whisper API engine.

        Args:
            api_key: OpenAI API key (default from settings)
        """
        settings = get_settings()
        self._api_key = api_key or settings.openai_api_key
        self._client: Any = None
        self._initialized = False

    @property
    def name(self) -> str:
        return "openai-whisper-api"

    @property
    def is_local(self) -> bool:
        return False

    async def initialize(self) -> None:
        """Initialize the OpenAI client."""
        if self._initialized:
            return

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=self._api_key)
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the engine."""
        if self._client:
            await self._client.close()
        self._client = None
        self._initialized = False

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio using OpenAI Whisper API."""
        if not self._initialized:
            await self.initialize()

        # Convert numpy array to WAV bytes
        audio_bytes = self._array_to_wav_bytes(audio, sample_rate)

        # Create a file-like object
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.wav"

        # Call API with verbose timestamps
        response = await self._client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=language,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

        # Extract results
        text = response.text
        words = None
        start_time = 0.0
        end_time = len(audio) / sample_rate

        if hasattr(response, "words") and response.words:
            words = [
                {
                    "word": w.word,
                    "start": w.start,
                    "end": w.end,
                }
                for w in response.words
            ]
            if words:
                start_time = words[0]["start"]
                end_time = words[-1]["end"]

        return TranscriptionResult(
            text=text,
            start_time=start_time,
            end_time=end_time,
            language=response.language if hasattr(response, "language") else language,
            words=words,
        )

    def _array_to_wav_bytes(self, audio: np.ndarray, sample_rate: int) -> bytes:
        """Convert numpy array to WAV bytes."""
        # Ensure float32 and normalize to int16
        if audio.dtype == np.float32:
            audio_int16 = (audio * 32767).astype(np.int16)
        else:
            audio_int16 = audio.astype(np.int16)

        # Write to bytes buffer
        buffer = io.BytesIO()
        wavfile.write(buffer, sample_rate, audio_int16)
        return buffer.getvalue()
