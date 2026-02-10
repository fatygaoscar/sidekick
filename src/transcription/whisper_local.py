"""Local transcription using faster-whisper."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import numpy as np

from config.settings import get_settings

from .base import TranscriptionEngine, TranscriptionResult

# Progress callback type: (progress: float, message: str) -> None
# progress is 0.0 to 1.0, message is a status string
ProgressCallback = Callable[[float, str], None]


class WhisperLocalEngine(TranscriptionEngine):
    """Transcription engine using faster-whisper for local processing."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        """
        Initialize faster-whisper engine.

        Args:
            model_size: Model size (tiny, base, small, medium, large-v2, large-v3)
            device: Device to use (auto, cpu, cuda)
            compute_type: Compute type (auto, int8, float16, float32)
        """
        settings = get_settings()
        self._model_size = model_size or settings.whisper_model_size
        self._device = device or settings.whisper_device
        self._compute_type = compute_type or settings.whisper_compute_type

        self._model: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._initialized = False

    @property
    def name(self) -> str:
        return f"faster-whisper-{self._model_size}"

    @property
    def is_local(self) -> bool:
        return True

    async def initialize(self) -> None:
        """Initialize the Whisper model."""
        if self._initialized:
            return

        # Load model in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._initialized = True

    def _load_model(self) -> None:
        """Load the Whisper model (blocking)."""
        from faster_whisper import WhisperModel

        # Determine device
        device = self._device
        if device == "auto":
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"

        # Determine compute type
        compute_type = self._compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"

        self._model = WhisperModel(
            self._model_size,
            device=device,
            compute_type=compute_type,
        )

    async def shutdown(self) -> None:
        """Shutdown the engine."""
        self._model = None
        self._executor.shutdown(wait=False)
        self._initialized = False

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
        progress_callback: Optional[ProgressCallback] = None,
        audio_duration: Optional[float] = None,
    ) -> TranscriptionResult:
        """Transcribe audio using faster-whisper."""
        if not self._initialized:
            await self.initialize()

        # Calculate duration if not provided
        if audio_duration is None:
            audio_duration = len(audio) / sample_rate

        # Run transcription in thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            self._executor,
            self._transcribe_sync,
            audio,
            sample_rate,
            language,
            progress_callback,
            audio_duration,
        )
        return result

    def _transcribe_sync(
        self,
        audio: np.ndarray,
        sample_rate: int,
        language: str | None,
        progress_callback: Optional[ProgressCallback] = None,
        audio_duration: Optional[float] = None,
    ) -> TranscriptionResult:
        """Synchronous transcription (runs in thread pool)."""
        # Ensure audio is float32 and correct shape
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Resample if needed (faster-whisper expects 16kHz)
        if sample_rate != 16000:
            from scipy import signal
            num_samples = int(len(audio) * 16000 / sample_rate)
            audio = signal.resample(audio, num_samples)

        # Calculate total duration for progress reporting
        if audio_duration is None:
            audio_duration = len(audio) / 16000  # Assume 16kHz after resampling

        # Report initial progress
        if progress_callback:
            progress_callback(0.05, "Starting transcription")

        # Transcribe
        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )

        # Collect segments and report progress as we go
        texts = []
        words_list = []
        start_time = 0.0
        end_time = 0.0
        last_progress_report = 0.0

        for segment in segments:
            texts.append(segment.text)
            if segment.words:
                words_list.extend([
                    {
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    }
                    for w in segment.words
                ])
            if len(texts) == 1:
                start_time = segment.start
            end_time = segment.end

            # Report progress based on segment end time vs total duration
            if progress_callback and audio_duration > 0:
                progress = min(0.95, end_time / audio_duration)
                # Only report if progress increased by at least 2%
                if progress - last_progress_report >= 0.02:
                    progress_callback(progress, f"Transcribing ({int(progress * 100)}%)")
                    last_progress_report = progress

        full_text = " ".join(texts).strip()

        # Report completion
        if progress_callback:
            progress_callback(1.0, "Transcription complete")

        return TranscriptionResult(
            text=full_text,
            start_time=start_time,
            end_time=end_time,
            confidence=info.language_probability if info.language else None,
            language=info.language,
            words=words_list if words_list else None,
        )
