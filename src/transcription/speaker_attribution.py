"""Shared helpers for mapping diarization spans onto transcript segments."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Sequence

from .base import AlignedTranscriptSegment

DiarizationSpan = tuple[float, float, str]
AssignSpeakerFn = Callable[[float, float, list[DiarizationSpan], float, float], str | None]


def assign_speakers_to_aligned_result(
    aligned_result: dict[str, Any],
    spans: Sequence[DiarizationSpan],
    assign_speaker_fn: AssignSpeakerFn,
) -> dict[str, Any]:
    """Attach speaker labels to aligned WhisperX segments and words."""
    guided_segments: list[dict[str, Any]] = []
    segment_speakers: list[str | None] = []
    normalized_spans = list(spans)

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
                        normalized_spans,
                        0.03,
                        0.0,
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
                normalized_spans,
                0.05,
                0.15,
            )
        if speaker_label is not None:
            guided_segment["speaker"] = speaker_label
            guided_segment["speaker_cluster"] = speaker_label

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
            previous_gap = float(guided_segment.get("start", 0.0)) - float(
                guided_segments[previous_index].get("end", 0.0)
            )
            break
        for next_index in range(index + 1, len(guided_segments)):
            candidate = segment_speakers[next_index]
            if candidate is None:
                continue
            next_label = candidate
            next_gap = float(guided_segments[next_index].get("start", 0.0)) - float(
                guided_segment.get("end", 0.0)
            )
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
            guided_segment["speaker_cluster"] = previous_label
            segment_speakers[index] = previous_label

    return {**aligned_result, "segments": guided_segments}


def build_aligned_transcript_segments(aligned_result: dict[str, Any]) -> list[AlignedTranscriptSegment]:
    """Normalize aligned-result segment dictionaries into persisted transcript segments."""
    segments: list[AlignedTranscriptSegment] = []
    for raw_segment in aligned_result.get("segments", []):
        start = raw_segment.get("start")
        end = raw_segment.get("end")
        text = str(raw_segment.get("text", "")).strip()
        if start is None or end is None or not text:
            continue

        speaker_label = raw_segment.get("speaker")
        speaker_cluster = raw_segment.get("speaker_cluster", speaker_label)
        words = raw_segment.get("words")
        segments.append(
            AlignedTranscriptSegment(
                start=float(start),
                end=float(end),
                text=text,
                speaker=str(speaker_label) if speaker_label is not None else None,
                speaker_cluster=str(speaker_cluster) if speaker_cluster is not None else None,
                words=words if isinstance(words, list) else None,
            )
        )
    return segments


def assign_speakers_to_segments(
    segments: Sequence[Any],
    spans: Sequence[DiarizationSpan],
    assign_speaker_fn: AssignSpeakerFn,
) -> list[AlignedTranscriptSegment]:
    """Map diarization spans onto existing timestamped transcript segments."""
    reassigned: list[AlignedTranscriptSegment] = []
    normalized_spans = list(spans)

    for segment in segments:
        start = float(getattr(segment, "start", getattr(segment, "start_time", 0.0)))
        end = float(getattr(segment, "end", getattr(segment, "end_time", 0.0)))
        speaker_cluster = assign_speaker_fn(start, end, normalized_spans, 0.05, 0.15)
        reassigned.append(
            AlignedTranscriptSegment(
                start=start,
                end=end,
                text=str(getattr(segment, "text", "")),
                speaker=str(speaker_cluster) if speaker_cluster is not None else None,
                speaker_cluster=str(speaker_cluster) if speaker_cluster is not None else None,
                words=getattr(segment, "words", None),
            )
        )
    return reassigned


def speaker_assignment_metrics(segments: Sequence[Any]) -> dict[str, float | int]:
    """Return speaker-count and unassigned metrics for attributed transcript segments."""
    normalized_labels = []
    unassigned_segment_count = 0

    for segment in segments:
        speaker_cluster = getattr(segment, "speaker_cluster", None)
        speaker = getattr(segment, "speaker", None)
        label = str(speaker_cluster or speaker or "").strip()
        if label:
            normalized_labels.append(label)
        else:
            unassigned_segment_count += 1

    total_segments = len(segments)
    return {
        "actual_speaker_count": len(set(normalized_labels)),
        "unassigned_segment_count": unassigned_segment_count,
        "unassigned_segment_ratio": (
            float(unassigned_segment_count) / float(total_segments)
            if total_segments
            else 0.0
        ),
    }


def repair_quality_gate_passed(
    metrics: dict[str, float | int | None],
    *,
    expected_speaker_count: int,
) -> bool:
    """Require the expected speaker count with minimal unassigned segments."""
    actual_count = int(metrics.get("actual_speaker_count") or 0)
    unassigned_ratio = float(metrics.get("unassigned_segment_ratio") or 0.0)
    if actual_count != int(expected_speaker_count):
        return False
    if unassigned_ratio > 0.05:
        return False
    return True
