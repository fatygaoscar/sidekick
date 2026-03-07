"""Utilities for stable user-facing speaker labels."""

from __future__ import annotations

import re
from typing import Iterable

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
