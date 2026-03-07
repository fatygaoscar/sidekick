"""Utilities for stable user-facing speaker labels."""

from __future__ import annotations

import re
from typing import Iterable, Sequence

_GENERIC_SPEAKER_RE = re.compile(r"^SPEAKER_(\d+)$")
_GENERIC_SPEAKER_TOKEN_RE = re.compile(r"\bSPEAKER_\d+\b")


def is_generic_speaker(label: str | None) -> bool:
    """Return True when the label is a raw diarization cluster name."""
    return bool(label and _GENERIC_SPEAKER_RE.match(str(label).strip()))


def build_user_facing_speaker_map(labels: Iterable[str | None]) -> dict[str, str]:
    """Map raw generic speaker labels to stable user-facing attendee labels."""
    generic_labels: list[str] = []
    seen: set[str] = set()

    for label in labels:
        normalized = str(label or "").strip()
        if not is_generic_speaker(normalized) or normalized in seen:
            continue
        generic_labels.append(normalized)
        seen.add(normalized)

    if not generic_labels:
        return {}
    if len(generic_labels) == 1:
        return {generic_labels[0]: "Attendee"}

    return {
        label: f"Attendee {_alpha_suffix(index)}"
        for index, label in enumerate(generic_labels)
    }


def resolve_user_facing_speaker_name(
    display_name: str | None,
    raw_label: str | None,
    fallback_map: dict[str, str],
) -> str | None:
    """Prefer a resolved display name, otherwise return a stable attendee fallback."""
    normalized_display = str(display_name or "").strip()
    if normalized_display and not is_generic_speaker(normalized_display):
        return normalized_display

    normalized_raw = str(raw_label or "").strip()
    if normalized_raw and not is_generic_speaker(normalized_raw):
        return normalized_raw

    return fallback_map.get(normalized_raw) if normalized_raw else None


def humanize_transcript_speaker_labels(transcript: str) -> tuple[str, dict[str, str]]:
    """Replace raw SPEAKER_XX tokens in a transcript with attendee fallbacks."""
    ordered_labels = [match.group(0) for match in _GENERIC_SPEAKER_TOKEN_RE.finditer(transcript)]
    speaker_map = build_user_facing_speaker_map(ordered_labels)
    if not speaker_map:
        return transcript, {}

    def replace(match: re.Match[str]) -> str:
        return speaker_map.get(match.group(0), match.group(0))

    return _GENERIC_SPEAKER_TOKEN_RE.sub(replace, transcript), speaker_map


def infer_strict_segment_speakers(
    segments: Sequence[object],
    *,
    max_duration_seconds: float = 2.0,
    max_gap_seconds: float = 1.0,
) -> list[dict[str, str | None] | None]:
    """Infer only very high-confidence missing speaker labels from surrounding context.

    This is intentionally output-only and conservative: it only fills completely
    unlabeled segments when the nearest labeled segment on each side points to
    the same identity and the unlabeled segment is short with small gaps.
    """

    segment_list = list(segments)
    inferred: list[dict[str, str | None] | None] = [None] * len(segment_list)

    for index, segment in enumerate(segment_list):
        if not _is_unlabeled_segment(segment):
            continue

        start_time = float(getattr(segment, "start_time", 0.0) or 0.0)
        end_time = float(getattr(segment, "end_time", 0.0) or 0.0)
        duration_seconds = end_time - start_time
        if duration_seconds <= 0 or duration_seconds > max_duration_seconds:
            continue

        previous = _previous_labeled_segment(segment_list, index)
        following = _next_labeled_segment(segment_list, index)
        if previous is None or following is None:
            continue

        if not _same_identity(previous, following):
            continue

        left_gap = max(0.0, start_time - float(getattr(previous, "end_time", start_time) or start_time))
        right_gap = max(0.0, float(getattr(following, "start_time", end_time) or end_time) - end_time)
        if left_gap > max_gap_seconds or right_gap > max_gap_seconds:
            continue

        inferred[index] = {
            "speaker": _preferred_display_name(previous) or _preferred_display_name(following),
            "speaker_cluster": _preferred_raw_label(previous) or _preferred_raw_label(following),
            "reason": "same_identity_on_both_sides_short_gap",
        }

    return inferred


def _is_unlabeled_segment(segment: object) -> bool:
    return not str(getattr(segment, "speaker", "") or "").strip() and not str(
        getattr(segment, "speaker_cluster", "") or ""
    ).strip()


def _previous_labeled_segment(segments: Sequence[object], start_index: int) -> object | None:
    for index in range(start_index - 1, -1, -1):
        if not _is_unlabeled_segment(segments[index]):
            return segments[index]
    return None


def _next_labeled_segment(segments: Sequence[object], start_index: int) -> object | None:
    for index in range(start_index + 1, len(segments)):
        if not _is_unlabeled_segment(segments[index]):
            return segments[index]
    return None


def _same_identity(left: object, right: object) -> bool:
    left_raw = _preferred_raw_label(left)
    right_raw = _preferred_raw_label(right)
    if left_raw and right_raw:
        return left_raw == right_raw

    left_name = _preferred_display_name(left)
    right_name = _preferred_display_name(right)
    return bool(left_name and right_name and left_name == right_name)


def _preferred_raw_label(segment: object) -> str | None:
    raw_label = str(getattr(segment, "speaker_cluster", None) or "").strip()
    if raw_label:
        return raw_label

    fallback = str(getattr(segment, "speaker", None) or "").strip()
    if fallback and not is_generic_speaker(fallback):
        return None
    return fallback or None


def _preferred_display_name(segment: object) -> str | None:
    display_name = str(getattr(segment, "speaker", None) or "").strip()
    if display_name and not is_generic_speaker(display_name):
        return display_name
    return None


def _alpha_suffix(index: int) -> str:
    """Convert 0-based indexes to A, B, ..., Z, AA, AB, ..."""
    value = index
    result = ""
    while True:
        value, remainder = divmod(value, 26)
        result = chr(ord("A") + remainder) + result
        if value == 0:
            return result
        value -= 1
