"""Shared speaker-review state derivation for transcript versions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.core.speaker_labels import is_generic_speaker


def speaker_identity(segment: Any) -> str | None:
    """Return the raw cluster-style identity for a transcript segment."""
    if isinstance(segment, dict):
        return segment.get("speaker_cluster") or segment.get("speaker") or None
    return getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None) or None


def transcript_requires_speaker_review(segments: list[Any]) -> bool:
    """Require review only when more than one unresolved generic speaker remains."""
    unresolved: set[str] = set()
    for segment in segments:
        if isinstance(segment, dict):
            speaker_name = str(segment.get("speaker", "") or "").strip()
        else:
            speaker_name = str(getattr(segment, "speaker", "") or "").strip()
        speaker_cluster = str(speaker_identity(segment) or "").strip()
        if speaker_name and not is_generic_speaker(speaker_name):
            continue
        speaker_key = speaker_name or speaker_cluster
        if is_generic_speaker(speaker_key):
            unresolved.add(speaker_key)
    return len(unresolved) > 1


def speaker_review_update_fields(segments: list[Any]) -> dict[str, object]:
    """Return the canonical persistence fields for transcript speaker-review state."""
    requires_review = transcript_requires_speaker_review(segments)
    return {
        "speaker_review_required": requires_review,
        "speaker_review_completed_at": None if requires_review else datetime.now(timezone.utc),
    }
