"""Classification and normalization for concise summarization."""

from __future__ import annotations

import re

from .types import ExtractedItem


_DECISION_NEGATIVE_CUES = (
    "should we",
    "what if",
    "?",
    "could ",
    "maybe",
    "questioned whether",
    "suggested",
    "one option is",
    "we might",
    "i think",
    "do we want",
)

_DECISION_POSITIVE_CUES = (
    "we decided",
    "agreed",
    "we'll ",
    "we will ",
    "we are going with",
    "the plan is",
    "move forward with",
    "moving forward with",
    "let's do",
    "lets do",
    "approved",
)

_CONSTRAINT_CUES = (
    "must",
    "cannot",
    "can't",
    "should not",
    "only if",
    "depends on",
    "guardrail",
    "constraint",
    "non-negotiable",
    "need to avoid",
)

_DIRECTION_CUES = (
    "leaning toward",
    "seems like",
    "directionally",
    "the direction is",
    "we should probably",
)


def _normalize_status(item_type: str, status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized:
        return normalized
    return {
        "decision": "confirmed",
        "proposal": "proposed",
        "action": "open",
        "constraint": "active",
        "issue": "open",
        "question": "open",
        "observation": "noted",
        "direction": "tentative",
    }.get(item_type, "open")


def _item_haystack(item: ExtractedItem) -> str:
    return " ".join(
        part
        for part in [
            item.text.lower(),
            item.context.lower() if item.context else "",
            item.evidence.quote.lower() if item.evidence and item.evidence.quote else "",
        ]
        if part
    )


def _has_positive_decision_signal(item: ExtractedItem) -> bool:
    haystack = _item_haystack(item)
    return any(cue in haystack for cue in _DECISION_POSITIVE_CUES)


def _has_negative_decision_signal(item: ExtractedItem) -> bool:
    haystack = _item_haystack(item)
    return any(cue in haystack for cue in _DECISION_NEGATIVE_CUES)


def _looks_like_constraint(item: ExtractedItem) -> bool:
    haystack = _item_haystack(item)
    return any(cue in haystack for cue in _CONSTRAINT_CUES)


def _looks_like_direction(item: ExtractedItem) -> bool:
    haystack = _item_haystack(item)
    return any(cue in haystack for cue in _DIRECTION_CUES)


def _extract_explicit_action_owner(text: str, known_participants: list[str]) -> str | None:
    lowered = text.lower().strip()
    for participant in known_participants:
        participant_lower = participant.lower()
        if re.match(rf"^{re.escape(participant_lower)}\s+(to|will|should|needs?\s+to)\b", lowered):
            return participant
    return None


def classify_items(
    items: list[ExtractedItem],
    known_participants: list[str],
) -> list[ExtractedItem]:
    """Normalize extracted items conservatively before validation."""
    participant_lookup = {participant.lower(): participant for participant in known_participants}
    classified: list[ExtractedItem] = []

    for item in items:
        item.status = _normalize_status(item.type, item.status)
        item.text = re.sub(r"\s+", " ", item.text).strip()
        if item.context:
            item.context = re.sub(r"\s+", " ", item.context).strip()

        if item.speaker:
            speaker_key = item.speaker.lower().strip()
            item.speaker = participant_lookup.get(speaker_key, item.speaker.strip())

        item.actors = [
            re.sub(r"\s+", " ", actor).strip()
            for actor in item.actors
            if actor and str(actor).strip()
        ]

        if item.owner:
            owner_lower = item.owner.lower().strip()
            if owner_lower in participant_lookup:
                item.owner = participant_lookup[owner_lower]
            elif item.speaker and owner_lower == item.speaker.lower():
                item.owner = item.speaker
            else:
                item.owner = None

        if item.type == "action":
            explicit_owner = _extract_explicit_action_owner(
                " ".join(
                    [
                        item.text,
                        item.evidence.quote if item.evidence and item.evidence.quote else "",
                    ]
                ),
                known_participants,
            )
            if explicit_owner:
                item.owner = explicit_owner

        if item.type == "decision":
            if _has_negative_decision_signal(item):
                item.type = "question"
                item.status = "open"
            elif not _has_positive_decision_signal(item):
                item.type = "discussion_point"
                item.status = "open"

        if item.type in {"proposal", "observation", "discussion_point"} and _looks_like_constraint(item):
            item.type = "constraint"
            item.status = "active"

        if item.type in {"proposal", "direction", "observation"} and item.type != "constraint":
            item.type = "discussion_point"
            item.status = "open"

        classified.append(item)

    return classified
