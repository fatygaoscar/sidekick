"""Deduplication and merging of extracted items across chunks.

Combines items from multiple chunks, detecting and merging duplicates
based on semantic similarity.
"""

import re
from typing import Optional

from .types import ExtractedItem


def _normalize_text(text: str) -> str:
    """Normalize text for comparison.

    Args:
        text: Raw text

    Returns:
        Normalized lowercase text with extra whitespace removed
    """
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    # Remove common filler words for comparison
    text = re.sub(r"\b(the|a|an|to|for|of|and|or|in|on|at)\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _compute_similarity(text1: str, text2: str) -> float:
    """Compute simple similarity between two texts.

    Uses Jaccard similarity on word sets.

    Args:
        text1: First text
        text2: Second text

    Returns:
        Similarity score 0-1
    """
    norm1 = _normalize_text(text1)
    norm2 = _normalize_text(text2)

    if not norm1 or not norm2:
        return 0.0

    words1 = set(norm1.split())
    words2 = set(norm2.split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union)


def _items_are_duplicates(item1: ExtractedItem, item2: ExtractedItem, threshold: float = 0.6) -> bool:
    """Check if two items are likely duplicates.

    Args:
        item1: First item
        item2: Second item
        threshold: Similarity threshold for duplicate detection

    Returns:
        True if items are likely duplicates
    """
    # Must be same type
    if item1.type != item2.type:
        return False

    # Check text similarity
    similarity = _compute_similarity(item1.text, item2.text)
    if similarity >= threshold:
        return True

    # Also check if one text contains the other (subset)
    norm1 = _normalize_text(item1.text)
    norm2 = _normalize_text(item2.text)

    if len(norm1) > 10 and len(norm2) > 10:
        if norm1 in norm2 or norm2 in norm1:
            return True

    return False


def _merge_item_pair(item1: ExtractedItem, item2: ExtractedItem) -> ExtractedItem:
    """Merge two duplicate items into one.

    Takes the better/more complete version of each field.

    Args:
        item1: First item
        item2: Second item

    Returns:
        Merged item
    """
    # Prefer longer text (more detail)
    if len(item2.text) > len(item1.text):
        merged_text = item2.text
    else:
        merged_text = item1.text

    # Prefer earlier timestamp
    if item1.source_timestamp and item2.source_timestamp:
        timestamp = min(item1.source_timestamp, item2.source_timestamp)
    else:
        timestamp = item1.source_timestamp or item2.source_timestamp

    # Higher confidence wins
    confidence = max(item1.confidence, item2.confidence)

    # Prefer non-null values for optional fields
    def prefer_value(v1: Optional[str], v2: Optional[str]) -> Optional[str]:
        if v1 and v2:
            return v1 if len(v1) >= len(v2) else v2
        return v1 or v2

    return ExtractedItem(
        id="",  # Will be assigned during structuring
        type=item1.type,
        text=merged_text,
        owner=prefer_value(item1.owner, item2.owner),
        due_date=prefer_value(item1.due_date, item2.due_date),
        blocking=prefer_value(item1.blocking, item2.blocking),
        source_timestamp=timestamp,
        confidence=confidence,
        rationale=prefer_value(item1.rationale, item2.rationale),
        impact=prefer_value(item1.impact, item2.impact),
        mitigation=prefer_value(item1.mitigation, item2.mitigation),
        context=prefer_value(item1.context, item2.context),
        who_decides=prefer_value(item1.who_decides, item2.who_decides),
        timeline=prefer_value(item1.timeline, item2.timeline),
        status=item1.status or item2.status,
    )


def merge_items(
    all_items: list[ExtractedItem],
    similarity_threshold: float = 0.6,
) -> list[ExtractedItem]:
    """Merge and deduplicate items from multiple chunks.

    Args:
        all_items: Flat list of all extracted items from all chunks
        similarity_threshold: Threshold for duplicate detection (0-1)

    Returns:
        Deduplicated list of items
    """
    if not all_items:
        return []

    # Group by type first for efficiency
    by_type: dict[str, list[ExtractedItem]] = {}
    for item in all_items:
        if item.type not in by_type:
            by_type[item.type] = []
        by_type[item.type].append(item)

    merged_items: list[ExtractedItem] = []

    for item_type, type_items in by_type.items():
        # Track which items have been merged
        merged_indices: set[int] = set()

        for i, item1 in enumerate(type_items):
            if i in merged_indices:
                continue

            # Find all duplicates of this item
            current = item1
            for j, item2 in enumerate(type_items[i + 1:], start=i + 1):
                if j in merged_indices:
                    continue

                if _items_are_duplicates(current, item2, similarity_threshold):
                    current = _merge_item_pair(current, item2)
                    merged_indices.add(j)

            merged_items.append(current)

    # Sort by type and source timestamp
    def sort_key(item: ExtractedItem) -> tuple[str, str]:
        return (item.type, item.source_timestamp or "")

    return sorted(merged_items, key=sort_key)


def validate_owners(items: list[ExtractedItem], known_participants: list[str]) -> list[ExtractedItem]:
    """Validate and normalize owner names against known participants.

    Args:
        items: List of extracted items
        known_participants: List of known participant names

    Returns:
        Items with validated owner fields
    """
    if not known_participants:
        return items

    # Create lowercase lookup
    participant_lookup = {p.lower(): p for p in known_participants}

    for item in items:
        if item.owner:
            owner_lower = item.owner.lower().strip()

            # Try exact match
            if owner_lower in participant_lookup:
                item.owner = participant_lookup[owner_lower]
                continue

            # Try partial match (first name only)
            for participant_name in known_participants:
                if owner_lower in participant_name.lower() or participant_name.lower() in owner_lower:
                    item.owner = participant_name
                    break

    return items
