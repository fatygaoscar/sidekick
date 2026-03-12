"""Local authoritative transcription using WhisperX."""

import asyncio
import gc
import logging
from collections import Counter
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
from .diarization_runtime import ensure_diarization_ready

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

    def _release_resources(self) -> None:
        self._model = None
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
        expected_speaker_count: int | None = None,
        late_join_offset_seconds: float | None = None,
        repair_reason: str | None = None,
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
            expected_speaker_count,
            late_join_offset_seconds,
            repair_reason,
        )

    def _transcribe_file_sync(
        self,
        audio_file: str,
        language: str | None,
        progress_callback: Optional[ProgressCallback] = None,
        expected_speaker_count: int | None = None,
        late_join_offset_seconds: float | None = None,
        repair_reason: str | None = None,
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
        diarization_backend = None
        diarization_model = None
        repair_strategy = None
        actual_speaker_count = None
        unassigned_segment_count = None
        unassigned_segment_ratio = None
        repair_quality_gate_passed = None
        if self._diarization_enabled:
            if progress_callback:
                progress_callback(0.70, "Assigning speakers")
            ensure_diarization_ready(enabled=True, hf_token=self._hf_token)
            (
                speaker_result,
                diarization_backend,
                diarization_model,
                repair_strategy,
                actual_speaker_count,
                unassigned_segment_count,
                unassigned_segment_ratio,
                repair_quality_gate_passed,
            ) = self._assign_pyannote_speakers(
                audio_file=audio_file,
                aligned_result=aligned_result,
                expected_speaker_count=expected_speaker_count,
            )
            if expected_speaker_count is not None and not repair_quality_gate_passed:
                raise RuntimeError(
                    f"Speaker detection could not confidently separate {int(expected_speaker_count)} speakers."
                )
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
            diarization_backend=diarization_backend,
            diarization_model=diarization_model,
            repair_strategy=repair_strategy,
            diarization_actual_speaker_count=actual_speaker_count,
            diarization_unassigned_segment_count=unassigned_segment_count,
            diarization_unassigned_segment_ratio=unassigned_segment_ratio,
            repair_quality_gate_passed=repair_quality_gate_passed,
        )

    def _assign_pyannote_speakers(
        self,
        *,
        audio_file: str,
        aligned_result: dict,
        expected_speaker_count: int | None,
    ) -> tuple[dict, str, str, str, int, int, float, bool | None]:
        from .diarize import assign_speaker, diarize, get_loaded_pipeline_model_name, get_required_pipeline_model_name

        diarize_kwargs: dict[str, int] = {}
        if expected_speaker_count is not None and expected_speaker_count > 0:
            diarize_kwargs["min_speakers"] = int(expected_speaker_count)
            diarize_kwargs["max_speakers"] = int(expected_speaker_count)

        spans = diarize(audio_file, self._hf_token, **diarize_kwargs)
        if not spans:
            raise RuntimeError("Required diarization model returned no speaker spans.")

        speaker_result = self._assign_speakers_from_spans(aligned_result, spans, assign_speaker)
        metrics = self._speaker_assignment_metrics(speaker_result)
        repair_strategy = (
            "full_file_exact_count"
            if expected_speaker_count is not None
            else "full_file_unconstrained"
        )
        repair_quality_gate_passed = None
        if expected_speaker_count is not None:
            repair_quality_gate_passed = self._repair_quality_gate_passed(
                metrics,
                expected_speaker_count=int(expected_speaker_count),
            )

        return (
            speaker_result,
            "pyannote",
            get_loaded_pipeline_model_name() or get_required_pipeline_model_name(),
            repair_strategy,
            int(metrics["actual_speaker_count"]),
            int(metrics["unassigned_segment_count"]),
            float(metrics["unassigned_segment_ratio"]),
            repair_quality_gate_passed,
        )

    def _assign_speakers_from_spans(
        self,
        aligned_result: dict,
        spans: list[tuple[float, float, str]],
        assign_speaker_fn: Callable[..., str | None],
    ) -> dict:
        guided_segments = []
        segment_speakers: list[str | None] = []
        for raw_segment in aligned_result.get("segments", []):
            guided_segment = dict(raw_segment)
            start = guided_segment.get("start")
            end = guided_segment.get("end")
            if start is None or end is None:
                guided_segments.append(guided_segment)
                segment_speakers.append(None)
                continue

            raw_words = guided_segment.get("words")
            word_speakers: list[tuple[str, float]] = []
            if isinstance(raw_words, list):
                updated_words = []
                for raw_word in raw_words:
                    if not isinstance(raw_word, dict):
                        updated_words.append(raw_word)
                        continue
                    updated_word = dict(raw_word)
                    word_start = updated_word.get("start")
                    word_end = updated_word.get("end")
                    if word_start is not None and word_end is not None:
                        word_speaker = assign_speaker_fn(
                            float(word_start),
                            float(word_end),
                            spans,
                            min_overlap=0.03,
                            min_overlap_ratio=0.0,
                        )
                        if word_speaker is not None:
                            updated_word["speaker"] = word_speaker
                            word_speakers.append(
                                (word_speaker, max(0.0, float(word_end) - float(word_start)))
                            )
                    updated_words.append(updated_word)
                guided_segment["words"] = updated_words

            speaker_label = None
            if word_speakers:
                speaker_duration = Counter()
                speaker_hits = Counter()
                for word_speaker, duration in word_speakers:
                    speaker_duration[word_speaker] += duration
                    speaker_hits[word_speaker] += 1
                speaker_label = max(
                    speaker_duration,
                    key=lambda label: (speaker_hits[label], speaker_duration[label], label),
                )
            if speaker_label is None:
                speaker_label = assign_speaker_fn(
                    float(start),
                    float(end),
                    spans,
                    min_overlap=0.05,
                    min_overlap_ratio=0.15,
                )
            if speaker_label is not None:
                guided_segment["speaker"] = speaker_label

            guided_segments.append(guided_segment)
            segment_speakers.append(speaker_label)

        for index, guided_segment in enumerate(guided_segments):
            if segment_speakers[index] is not None:
                continue
            previous_label = None
            next_label = None
            previous_gap = None
            next_gap = None

            for previous_index in range(index - 1, -1, -1):
                candidate = segment_speakers[previous_index]
                if candidate is None:
                    continue
                previous_label = candidate
                previous_gap = float(guided_segment.get("start", 0.0)) - float(guided_segments[previous_index].get("end", 0.0))
                break
            for next_index in range(index + 1, len(guided_segments)):
                candidate = segment_speakers[next_index]
                if candidate is None:
                    continue
                next_label = candidate
                next_gap = float(guided_segments[next_index].get("start", 0.0)) - float(guided_segment.get("end", 0.0))
                break

            if (
                previous_label
                and next_label
                and previous_label == next_label
                and previous_gap is not None
                and next_gap is not None
                and previous_gap <= 1.5
                and next_gap <= 1.5
            ):
                guided_segment["speaker"] = previous_label
                segment_speakers[index] = previous_label

        return {**aligned_result, "segments": guided_segments}

    def _speaker_assignment_metrics(
        self,
        speaker_result: dict,
    ) -> dict[str, float | int | None]:
        segments = speaker_result.get("segments", [])
        assigned_labels = [
            str(segment.get("speaker")).strip()
            for segment in segments
            if segment.get("speaker") is not None and str(segment.get("speaker")).strip()
        ]
        total_segments = len(segments)
        unassigned_segment_count = sum(1 for segment in segments if not segment.get("speaker"))
        actual_speaker_count = len(set(assigned_labels))
        return {
            "actual_speaker_count": actual_speaker_count,
            "unassigned_segment_count": unassigned_segment_count,
            "unassigned_segment_ratio": (
                (float(unassigned_segment_count) / float(total_segments))
                if total_segments
                else 0.0
            ),
        }

    def _repair_quality_gate_passed(
        self,
        metrics: dict[str, float | int | None],
        *,
        expected_speaker_count: int,
    ) -> bool:
        actual_count = int(metrics.get("actual_speaker_count") or 0)
        unassigned_ratio = float(metrics.get("unassigned_segment_ratio") or 0.0)
        if actual_count != int(expected_speaker_count):
            return False
        if unassigned_ratio > 0.05:
            return False
        return True
