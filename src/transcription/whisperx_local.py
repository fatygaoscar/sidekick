"""Local authoritative transcription using WhisperX."""

import asyncio
import gc
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from config.settings import get_settings

from .audio_decode import decode_audio_to_mono_float32, duration_seconds_from_audio
from .base import (
    AlignedTranscriptSegment,
    FileTranscriptionResult,
    TranscriptionEngine,
    TranscriptionResult,
)

ProgressCallback = Callable[[float, str], None]
logger = logging.getLogger(__name__)


class WhisperXLocalEngine(TranscriptionEngine):
    """File-oriented local transcription engine backed by WhisperX."""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model_size = model_size or settings.whisper_model_size
        self._device = device or settings.whisper_device
        self._compute_type = compute_type or settings.whisper_compute_type
        self._batch_size = batch_size or settings.whisperx_batch_size
        self._hf_token = settings.hf_token
        self._diarization_enabled = settings.diarization_enabled

        self._model: Any = None
        self._diarization_pipeline: Any = None
        self._align_models: dict[str, tuple[Any, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._initialized = False

    @property
    def name(self) -> str:
        return f"whisperx-{self._model_size}"

    @property
    def is_local(self) -> bool:
        return True

    async def initialize(self) -> None:
        if self._initialized:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._initialized = True

    def _resolved_device(self) -> str:
        device = self._device
        if device == "auto":
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        return device

    def _resolved_compute_type(self, device: str) -> str:
        compute_type = self._compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "float32"
        return compute_type

    def _load_model(self) -> None:
        import whisperx

        device = self._resolved_device()
        compute_type = self._resolved_compute_type(device)
        if device != "cuda":
            raise RuntimeError("WhisperX local backend requires CUDA")
        if compute_type != "float16":
            raise RuntimeError("WhisperX local backend requires float16 compute type")

        self._model = whisperx.load_model(
            self._model_size,
            device,
            compute_type=compute_type,
        )

    def _get_align_model(self, language_code: str):
        import whisperx

        if language_code not in self._align_models:
            self._align_models[language_code] = whisperx.load_align_model(
                language_code=language_code,
                device=self._resolved_device(),
            )
        return self._align_models[language_code]

    def _get_diarization_pipeline(self):
        from whisperx.diarize import DiarizationPipeline

        if self._diarization_pipeline is None:
            self._diarization_pipeline = DiarizationPipeline(
                token=self._hf_token,
                device=self._resolved_device(),
            )
        return self._diarization_pipeline

    def _release_resources(self) -> None:
        self._model = None
        self._diarization_pipeline = None
        self._align_models.clear()
        self._initialized = False

        gc.collect()

        try:
            import torch
        except Exception:
            return

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            ipc_collect = getattr(torch.cuda, "ipc_collect", None)
            if callable(ipc_collect):
                ipc_collect()

    async def unload(self) -> None:
        self._release_resources()

    async def shutdown(self) -> None:
        self._release_resources()
        self._executor.shutdown(wait=False)

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str | None = None,
        progress_callback: Optional[ProgressCallback] = None,
        audio_duration: Optional[float] = None,
    ) -> TranscriptionResult:
        raise RuntimeError("Live preview is disabled for the WhisperX local backend")

    async def transcribe_file(
        self,
        file_path: str | Path,
        language: str | None = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> FileTranscriptionResult:
        if not self._initialized:
            await self.initialize()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._transcribe_file_sync,
            str(Path(file_path)),
            language,
            progress_callback,
        )

    def _transcribe_file_sync(
        self,
        audio_file: str,
        language: str | None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> FileTranscriptionResult:
        import whisperx

        if not self._model:
            raise RuntimeError("WhisperX model is not initialized")

        audio = decode_audio_to_mono_float32(audio_file, sample_rate=16000)
        duration_seconds = duration_seconds_from_audio(audio, sample_rate=16000)

        if progress_callback:
            progress_callback(0.15, "Transcribing audio")

        asr_result = self._model.transcribe(
            audio,
            batch_size=self._batch_size,
            language=language,
        )
        detected_language = asr_result.get("language") or language
        if not detected_language:
            raise RuntimeError("WhisperX did not return a detected language")

        if progress_callback:
            progress_callback(0.45, "Aligning words")

        align_model, metadata = self._get_align_model(str(detected_language))
        aligned_result = whisperx.align(
            asr_result.get("segments", []),
            align_model,
            metadata,
            audio,
            self._resolved_device(),
            return_char_alignments=False,
        )

        speaker_result = aligned_result
        if self._diarization_enabled and self._hf_token:
            try:
                if progress_callback:
                    progress_callback(0.70, "Assigning speakers")
                diarization_pipeline = self._get_diarization_pipeline()
                diarize_segments = diarization_pipeline(audio)
                speaker_result = whisperx.assign_word_speakers(diarize_segments, aligned_result)
            except Exception as exc:
                logger.warning("WhisperX diarization failed, continuing without speakers: %s", exc)
        elif progress_callback:
            progress_callback(0.70, "Skipping speaker assignment")

        segments: list[AlignedTranscriptSegment] = []
        texts: list[str] = []
        for raw_segment in speaker_result.get("segments", []):
            start = raw_segment.get("start")
            end = raw_segment.get("end")
            text = str(raw_segment.get("text", "")).strip()
            if start is None or end is None or not text:
                continue

            speaker_label = raw_segment.get("speaker")
            words = raw_segment.get("words")
            segments.append(
                AlignedTranscriptSegment(
                    start=float(start),
                    end=float(end),
                    text=text,
                    speaker=str(speaker_label) if speaker_label is not None else None,
                    speaker_cluster=str(speaker_label) if speaker_label is not None else None,
                    words=words if isinstance(words, list) else None,
                )
            )
            texts.append(text)

        if progress_callback:
            progress_callback(1.0, "Transcription complete")

        return FileTranscriptionResult(
            text=" ".join(texts).strip(),
            duration_seconds=duration_seconds,
            segments=segments,
            confidence=None,
            language=str(detected_language),
        )
