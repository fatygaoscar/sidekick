"""Structuring pass for the pipeline.

Assigns IDs to items, validates schema, and organizes into final structure.
"""

from typing import Optional

from .types import ExtractedItem, StructuredItems


# ID prefixes by type
ID_PREFIXES = {
    "action": "A",
    "decision": "D",
    "risk": "R",
    "question": "Q",
    "followup": "F",
}


def _assign_ids(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """Assign sequential IDs to items by type.

    Args:
        items: List of items without IDs

    Returns:
        Items with IDs assigned
    """
    counters: dict[str, int] = {}

    for item in items:
        prefix = ID_PREFIXES.get(item.type, "X")
        if prefix not in counters:
            counters[prefix] = 0
        counters[prefix] += 1
        item.id = f"{prefix}-{counters[prefix]:03d}"

    return items


def _validate_item(item: ExtractedItem) -> list[str]:
    """Validate a single item and return any issues.

    Args:
        item: Item to validate

    Returns:
        List of validation issue messages (empty if valid)
    """
    issues = []

    if not item.text or len(item.text.strip()) < 3:
        issues.append(f"{item.id}: Text is too short or empty")

    if item.confidence < 0 or item.confidence > 1:
        issues.append(f"{item.id}: Confidence {item.confidence} out of range [0, 1]")

    if item.type not in ID_PREFIXES:
        issues.append(f"{item.id}: Unknown type '{item.type}'")

    return issues


def structure_items(
    merged_items: list[ExtractedItem],
    min_confidence: float = 0.5,
) -> tuple[StructuredItems, list[str]]:
    """Structure and validate merged items.

    Assigns IDs, filters by confidence, validates schema, and
    organizes into StructuredItems.

    Args:
        merged_items: Deduplicated list of items
        min_confidence: Minimum confidence threshold to include item

    Returns:
        Tuple of (StructuredItems, list of validation warnings)
    """
    warnings: list[str] = []

    # Filter by confidence
    filtered = [item for item in merged_items if item.confidence >= min_confidence]
    filtered_count = len(merged_items) - len(filtered)
    if filtered_count > 0:
        warnings.append(f"Filtered {filtered_count} low-confidence items (< {min_confidence})")

    # Assign IDs
    filtered = _assign_ids(filtered)

    # Validate each item
    for item in filtered:
        item_issues = _validate_item(item)
        warnings.extend(item_issues)

    # Organize into structured groups
    result = StructuredItems()

    for item in filtered:
        if item.type == "action":
            result.actions.append(item)
        elif item.type == "decision":
            result.decisions.append(item)
        elif item.type == "risk":
            result.risks.append(item)
        elif item.type == "question":
            result.questions.append(item)
        elif item.type == "followup":
            result.followups.append(item)

    return result, warnings


def items_to_json(items: StructuredItems) -> dict:
    """Convert StructuredItems to JSON-serializable dict.

    Args:
        items: Structured items

    Returns:
        Dict suitable for JSON serialization
    """
    def item_to_dict(item: ExtractedItem) -> dict:
        d = {
            "id": item.id,
            "type": item.type,
            "text": item.text,
            "confidence": item.confidence,
        }

        # Add optional fields if present
        if item.owner:
            d["owner"] = item.owner
        if item.due_date:
            d["due_date"] = item.due_date
        if item.blocking:
            d["blocking"] = item.blocking
        if item.source_timestamp:
            d["timestamp"] = item.source_timestamp
        if item.rationale:
            d["rationale"] = item.rationale
        if item.impact:
            d["impact"] = item.impact
        if item.mitigation:
            d["mitigation"] = item.mitigation
        if item.context:
            d["context"] = item.context
        if item.who_decides:
            d["who_decides"] = item.who_decides
        if item.timeline:
            d["timeline"] = item.timeline
        if item.status and item.type == "action":
            d["status"] = item.status

        return d

    return {
        "actions": [item_to_dict(i) for i in items.actions],
        "decisions": [item_to_dict(i) for i in items.decisions],
        "risks": [item_to_dict(i) for i in items.risks],
        "questions": [item_to_dict(i) for i in items.questions],
        "followups": [item_to_dict(i) for i in items.followups],
    }


def items_to_markdown_tables(items: StructuredItems) -> str:
    """Convert StructuredItems to markdown tables.

    Args:
        items: Structured items

    Returns:
        Markdown string with tables for each item type
    """
    sections = []

    # Action Items
    if items.actions:
        lines = ["### Action Items"]
        lines.append("| ID | Task | Owner | Due | Status |")
        lines.append("|----|------|-------|-----|--------|")
        for item in items.actions:
            owner = item.owner or "-"
            due = item.due_date or "-"
            status = item.status.title() if item.status else "Open"
            lines.append(f"| {item.id} | {item.text} | {owner} | {due} | {status} |")
        sections.append("\n".join(lines))

    # Decisions
    if items.decisions:
        lines = ["### Decisions"]
        lines.append("| ID | Decision | Rationale | Owner |")
        lines.append("|----|----------|-----------|-------|")
        for item in items.decisions:
            rationale = item.rationale or "-"
            owner = item.owner or "-"
            lines.append(f"| {item.id} | {item.text} | {rationale} | {owner} |")
        sections.append("\n".join(lines))

    # Risks & Concerns
    if items.risks:
        lines = ["### Risks & Concerns"]
        lines.append("| ID | Risk | Impact | Mitigation |")
        lines.append("|----|------|--------|------------|")
        for item in items.risks:
            impact = item.impact or "-"
            mitigation = item.mitigation or "-"
            lines.append(f"| {item.id} | {item.text} | {impact} | {mitigation} |")
        sections.append("\n".join(lines))

    # Open Questions
    if items.questions:
        lines = ["### Open Questions"]
        lines.append("| ID | Question | Context | Who Decides |")
        lines.append("|----|----------|---------|-------------|")
        for item in items.questions:
            context = item.context or "-"
            who_decides = item.who_decides or "-"
            lines.append(f"| {item.id} | {item.text} | {context} | {who_decides} |")
        sections.append("\n".join(lines))

    # Follow-ups
    if items.followups:
        lines = ["### Follow-ups"]
        lines.append("| ID | Item | Owner | Timeline |")
        lines.append("|----|------|-------|----------|")
        for item in items.followups:
            owner = item.owner or "-"
            timeline = item.timeline or "-"
            lines.append(f"| {item.id} | {item.text} | {owner} | {timeline} |")
        sections.append("\n".join(lines))

    if sections:
        return "\n\n".join(sections)
    else:
        return "_No structured items extracted._"
