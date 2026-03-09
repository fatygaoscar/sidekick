"""Conservative entity normalization helpers."""

from __future__ import annotations

import re

from .types import NormalizedEntity, TranscriptTurn


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def normalize_entities(turns: list[TranscriptTurn]) -> tuple[list[TranscriptTurn], list[NormalizedEntity]]:
    """Normalize speaker aliases conservatively and return discovered entities."""
    entities: list[NormalizedEntity] = []
    canonical_by_normalized: dict[str, str] = {}
    surface_forms: dict[str, set[str]] = {}

    for turn in turns:
        if not turn.speaker_raw:
            continue
        token = _normalize_token(turn.speaker_raw)
        if not token:
            continue
        canonical_by_normalized.setdefault(token, turn.speaker_raw.strip())
        surface_forms.setdefault(token, set()).add(turn.speaker_raw.strip())

    for turn in turns:
        if not turn.speaker_raw:
            continue
        token = _normalize_token(turn.speaker_raw)
        canonical = canonical_by_normalized.get(token)
        # Conservative rule: only exact normalized token matches are merged.
        if canonical:
            turn.speaker = canonical

    for index, (token, canonical) in enumerate(sorted(canonical_by_normalized.items()), start=1):
        entities.append(
            NormalizedEntity(
                entity_id=f"ent_{index:04d}",
                entity_type="person",
                surface_forms=sorted(surface_forms.get(token, {canonical})),
                canonical_name=canonical,
                confidence=0.99,
                normalization_source="exact_normalized_token",
            )
        )

    return turns, entities


def extract_actors(text: str, entities: list[NormalizedEntity]) -> list[str]:
    """Extract explicit actor mentions conservatively from text."""
    lowered = text.lower()
    actors: list[str] = []
    for entity in entities:
        for surface in entity.surface_forms:
            if surface.lower() in lowered and entity.canonical_name not in actors:
                actors.append(entity.canonical_name)
                break
    return actors
