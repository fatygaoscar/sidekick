"""Transcription engine manager for switching between backends."""

from pathlib import Path
from typing import Callable, Optional

import numpy as np

from config.settings import Settings, TranscriptionBackend, get_settings
from src.core.events import EventType, get_event_bus

from .base import TranscriptionEngine, TranscriptionResult
from .whisper_api import WhisperAPIEngine
from .whisper_local import WhisperLocalEngine

# Progress callback type: (progress: float, message: str) -> None
ProgressCallback = Callable[[float, str], None]


class TranscriptionManager:
    """Manages transcription engines and provides unified interface."""

    def __init__(self, settings: Settings | None = None) -> None:
        """
        Initialize transcription manager.

        Args:
            settings: Application settings (default from get_settings)
        """
        self._settings = settings or get_settings()
        self._event_bus = get_event_bus()
        self._engines: dict[TranscriptionBackend, TranscriptionEngine] = {}
        self._active_engine: TranscriptionEngine | None = None
        self._initialized = False

    @property
    def active_engine(self) -> TranscriptionEngine | None:
        """Get the currently active engine."""
        return self._active_engine

    @property
    def active_backend(self) -> TranscriptionBackend | None:
        """Get the currently active backend type."""
        if self._active_engine is None:
            return None
        for backend, engine in self._engines.items():
            if engine is self._active_engine:
                return backend
        return None

    async def initialize(self, backend: TranscriptionBackend | None = None) -> None:
        """
        Initialize transcription manager with specified or default backend.

        Args:
            backend: Backend to initialize (default from settings)
        """
        backend = backend or self._settings.transcription_backend

        # Create engine if not exists
        if backend not in self._engines:
            self._engines[backend] = self._create_engine(backend)

        # Initialize and set as active
        engine = self._engines[backend]
        await engine.initialize()
        self._active_engine = engine
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown all engines."""
        for engine in self._engines.values():
            await engine.shutdown()
        self._engines.clear()
        self._active_engine = None
        self._initialized = False

    async def switch_backend(self, backend: TranscriptionBackend) -> TranscriptionEngine:
        """
        Switch to a different transcription backend.

        Args:
            backend: Backend to switch to

        Returns:
            The new active engine
        """
        if backend not in self._engines:
            self._engines[backend] = self._create_engine(backend)

        engine = self._engines[backend]
        if not engine._initialized:
            await engine.initialize()

        self._active_engine = engine
        return engine

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
        start_offset: float = 0.0,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio using the active engine.

        Args:
            audio: Audio data as numpy array
            sample_rate: Audio sample rate
            language: Language code or None for auto-detect
            start_offset: Time offset for the audio chunk (seconds)
            progress_callback: Optional callback for progress updates

        Returns:
            TranscriptionResult with adjusted timestamps
        """
        if not self._initialized or self._active_engine is None:
            await self.initialize()

        audio_duration = len(audio) / sample_rate

        # Emit start event
        await self._event_bus.emit(
            EventType.TRANSCRIPTION_STARTED,
            {
                "engine": self._active_engine.name,
                "audio_duration": audio_duration,
            },
            source="transcription_manager",
        )

        try:
            result = await self._active_engine.transcribe(
                audio,
                sample_rate,
                language,
                progress_callback=progress_callback,
                audio_duration=audio_duration,
            )

            # Adjust timestamps by offset
            adjusted_result = TranscriptionResult(
                text=result.text,
                start_time=result.start_time + start_offset,
                end_time=result.end_time + start_offset,
                confidence=result.confidence,
                language=result.language,
                words=[
                    {**w, "start": w["start"] + start_offset, "end": w["end"] + start_offset}
                    for w in (result.words or [])
                ],
            )

            # Emit completion event
            await self._event_bus.emit(
                EventType.TRANSCRIPTION_COMPLETED,
                {
                    "engine": self._active_engine.name,
                    "text": result.text,
                    "duration": result.end_time - result.start_time,
                },
                source="transcription_manager",
            )

            return adjusted_result

        except Exception as e:
            # Emit error event
            await self._event_bus.emit(
                EventType.TRANSCRIPTION_ERROR,
                {
                    "engine": self._active_engine.name if self._active_engine else "unknown",
                    "error": str(e),
                },
                source="transcription_manager",
            )
            raise

    async def transcribe_file(
        self,
        file_path: str | Path,
        language: str | None = None,
        start_offset: float = 0.0,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> tuple[TranscriptionResult, float]:
        """
        Transcribe an audio file from disk.

        Args:
            file_path: Path to audio file
            language: Language code or None for auto-detect
            start_offset: Time offset (seconds)
            progress_callback: Optional callback for progress updates

        Returns:
            Tuple of (TranscriptionResult, duration_seconds)
        """
        from faster_whisper.audio import decode_audio

        resolved = Path(file_path)
        audio = decode_audio(str(resolved), sampling_rate=16000)
        duration_seconds = len(audio) / 16000
        result = await self.transcribe(
            audio=audio,
            sample_rate=16000,
            language=language,
            start_offset=start_offset,
            progress_callback=progress_callback,
        )
        return result, duration_seconds

    def _create_engine(self, backend: TranscriptionBackend) -> TranscriptionEngine:
        """Create a transcription engine for the specified backend."""
        if backend == TranscriptionBackend.LOCAL:
            return WhisperLocalEngine()
        elif backend == TranscriptionBackend.OPENAI:
            return WhisperAPIEngine()
        else:
            raise ValueError(f"Unknown transcription backend: {backend}")
