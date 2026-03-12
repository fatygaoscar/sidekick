"""Cloud transcription using OpenAI Whisper API."""

import io
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from scipy.io import wavfile

from config.settings import get_settings

from .base import (
    AlignedTranscriptSegment,
    FileTranscriptionResult,
    TranscriptionEngine,
    TranscriptionResult,
)

ProgressCallback = Callable[[float, str], None]


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
        progress_callback: Optional[ProgressCallback] = None,
        audio_duration: Optional[float] = None,
    ) -> TranscriptionResult:
        """Transcribe audio using OpenAI Whisper API."""
        if not self._initialized:
            await self.initialize()

        # API doesn't support incremental progress, just report start/end
        if progress_callback:
            progress_callback(0.1, "Uploading to OpenAI")

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

        if progress_callback:
            progress_callback(1.0, "Transcription complete")

        return TranscriptionResult(
            text=text,
            start_time=start_time,
            end_time=end_time,
            language=response.language if hasattr(response, "language") else language,
            words=words,
        )

    async def transcribe_file(
        self,
        file_path: str | Path,
        language: str | None = None,
        progress_callback: Optional[ProgressCallback] = None,
        expected_speaker_count: int | None = None,
        late_join_offset_seconds: float | None = None,
        repair_reason: str | None = None,
    ) -> FileTranscriptionResult:
        """Transcribe an audio file and normalize it into authoritative segment form."""
        from faster_whisper.audio import decode_audio

        resolved = Path(file_path)
        audio = decode_audio(str(resolved), sampling_rate=16000)
        duration_seconds = len(audio) / 16000 if len(audio) else 0.0
        result = await self.transcribe(
            audio=audio,
            sample_rate=16000,
            language=language,
            progress_callback=progress_callback,
            audio_duration=duration_seconds,
        )

        segments: list[AlignedTranscriptSegment] = []
        if result.words:
            words = [word for word in result.words if str(word.get("word", "")).strip()]
            if words:
                segments.append(
                    AlignedTranscriptSegment(
                        start=float(words[0].get("start", 0.0)),
                        end=float(words[-1].get("end", duration_seconds)),
                        text=result.text.strip(),
                        speaker=None,
                        speaker_cluster=None,
                        words=words,
                    )
                )

        if not segments and result.text.strip():
            segments.append(
                AlignedTranscriptSegment(
                    start=result.start_time,
                    end=result.end_time or duration_seconds,
                    text=result.text.strip(),
                    speaker=None,
                    speaker_cluster=None,
                    words=result.words,
                )
            )

        return FileTranscriptionResult(
            text=result.text.strip(),
            duration_seconds=duration_seconds,
            segments=segments,
            confidence=result.confidence,
            language=result.language,
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
