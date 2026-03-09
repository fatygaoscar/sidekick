"""Deduplication helpers for extracted adaptive summary items."""

from __future__ import annotations

import re

from .types import EvidenceSpan, ExtractedItem


def _normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\b(the|a|an|to|for|of|and|or|in|on|at)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _compute_similarity(text1: str, text2: str) -> float:
    norm1 = _normalize_text(text1)
    norm2 = _normalize_text(text2)
    if not norm1 or not norm2:
        return 0.0
    words1 = set(norm1.split())
    words2 = set(norm2.split())
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / len(words1 | words2)


def _items_are_duplicates(item1: ExtractedItem, item2: ExtractedItem, threshold: float = 0.72) -> bool:
    if item1.type != item2.type:
        return False
    if item1.thread_id and item2.thread_id and item1.thread_id != item2.thread_id:
        return False
    if item1.topic_id and item2.topic_id and item1.topic_id != item2.topic_id:
        if not item1.thread_id or item1.thread_id != item2.thread_id:
            return False
    if item1.speaker and item2.speaker and item1.speaker != item2.speaker:
        return False
    return _compute_similarity(item1.text, item2.text) >= threshold


def _merge_evidence(e1: EvidenceSpan, e2: EvidenceSpan) -> EvidenceSpan:
    turn_ids = list(dict.fromkeys([*e1.turn_ids, *e2.turn_ids]))
    quote = e1.quote if len(e1.quote) >= len(e2.quote) else e2.quote
    start_time = min(e1.start_time or e2.start_time, e2.start_time or e1.start_time)
    end_time = max(e1.end_time or e2.end_time, e2.end_time or e1.end_time)
    return EvidenceSpan(turn_ids=turn_ids, start_time=start_time, end_time=end_time, quote=quote)


def _merge_item_pair(item1: ExtractedItem, item2: ExtractedItem) -> ExtractedItem:
    merged_text = item2.text if len(item2.text) > len(item1.text) else item1.text
    merged = ExtractedItem(
        id=item1.id or item2.id,
        type=item1.type,
        text=merged_text,
        speaker=item1.speaker or item2.speaker,
        actors=list(dict.fromkeys([*item1.actors, *item2.actors])),
        owner=item1.owner or item2.owner,
        due_date=item1.due_date or item2.due_date,
        blocking=item1.blocking or item2.blocking,
        source_timestamp=item1.source_timestamp or item2.source_timestamp,
        confidence=max(item1.confidence, item2.confidence),
        importance=max(item1.importance, item2.importance),
        rationale=item1.rationale or item2.rationale,
        impact=item1.impact or item2.impact,
        mitigation=item1.mitigation or item2.mitigation,
        context=item1.context or item2.context,
        who_decides=item1.who_decides or item2.who_decides,
        timeline=item1.timeline or item2.timeline,
        status=item1.status if item1.status == item2.status else (item1.status or item2.status),
        topic_id=item1.topic_id or item2.topic_id,
        thread_id=item1.thread_id or item2.thread_id,
        evidence=_merge_evidence(item1.evidence, item2.evidence),
        validation_flags=list(dict.fromkeys([*item1.validation_flags, *item2.validation_flags])),
        validated=item1.validated or item2.validated,
        dropped=item1.dropped and item2.dropped,
    )
    return merged


def merge_items(all_items: list[ExtractedItem], similarity_threshold: float = 0.72) -> list[ExtractedItem]:
    """Merge duplicate extracted items while preserving per-thread structure."""
    if not all_items:
        return []

    merged_items: list[ExtractedItem] = []
    by_type: dict[str, list[ExtractedItem]] = {}
    for item in all_items:
        by_type.setdefault(item.type, []).append(item)

    for type_items in by_type.values():
        merged_indices: set[int] = set()
        for i, current in enumerate(type_items):
            if i in merged_indices:
                continue
            for j, candidate in enumerate(type_items[i + 1 :], start=i + 1):
                if j in merged_indices:
                    continue
                if _items_are_duplicates(current, candidate, similarity_threshold):
                    current = _merge_item_pair(current, candidate)
                    merged_indices.add(j)
            merged_items.append(current)

    return sorted(
        merged_items,
        key=lambda item: (
            item.thread_id or "",
            item.topic_id or "",
            item.evidence.start_time,
            item.type,
        ),
    )


def validate_owners(items: list[ExtractedItem], known_participants: list[str]) -> list[ExtractedItem]:
    """Normalize owner values to explicit known participants or None."""
    if not known_participants:
        return items
    participant_lookup = {participant.lower(): participant for participant in known_participants}
    for item in items:
        if not item.owner:
            continue
        owner_lower = item.owner.lower().strip()
        item.owner = participant_lookup.get(owner_lower)
    return items
