"""Structuring helpers for adaptive summary items."""

from __future__ import annotations

from .types import EvidenceSpan, ExtractedItem, StructuredItems


ID_PREFIXES = {
    "action": "A",
    "decision": "D",
    "proposal": "P",
    "constraint": "C",
    "issue": "I",
    "question": "Q",
    "observation": "O",
    "direction": "R",
}


def assign_ids(items: list[ExtractedItem]) -> list[ExtractedItem]:
    counters: dict[str, int] = {}
    for item in items:
        if item.id:
            continue
        prefix = ID_PREFIXES.get(item.type, "X")
        counters[prefix] = counters.get(prefix, 0) + 1
        item.id = f"{prefix}-{counters[prefix]:03d}"
    return items


def _evidence_to_dict(evidence: EvidenceSpan) -> dict[str, object]:
    return {
        "turn_ids": list(evidence.turn_ids),
        "start_time": evidence.start_time,
        "end_time": evidence.end_time,
        "quote": evidence.quote,
    }


def items_to_json(items: StructuredItems) -> dict[str, list[dict[str, object]]]:
    def item_to_dict(item: ExtractedItem) -> dict[str, object]:
        return {
            "id": item.id,
            "type": item.type,
            "text": item.text,
            "speaker": item.speaker,
            "actors": list(item.actors),
            "owner": item.owner,
            "due_date": item.due_date,
            "confidence": item.confidence,
            "importance": item.importance,
            "status": item.status,
            "topic_id": item.topic_id,
            "thread_id": item.thread_id,
            "validation_flags": list(item.validation_flags),
            "evidence": _evidence_to_dict(item.evidence),
        }

    return {
        "actions": [item_to_dict(item) for item in items.actions],
        "decisions": [item_to_dict(item) for item in items.decisions],
        "proposals": [item_to_dict(item) for item in items.proposals],
        "constraints": [item_to_dict(item) for item in items.constraints],
        "issues": [item_to_dict(item) for item in items.issues],
        "questions": [item_to_dict(item) for item in items.questions],
        "observations": [item_to_dict(item) for item in items.observations],
        "directions": [item_to_dict(item) for item in items.directions],
    }
