"""Deterministic validation rules for concise meeting summarization."""

from __future__ import annotations

import re

from .types import ExtractedItem, StructuredItems


_COMMITMENT_CUES = (
    "we decided",
    "agreed",
    "we'll ",
    "we will ",
    "move forward with",
    "the plan is",
    "let's proceed",
)

_QUESTION_CUES = (
    "should we",
    "what if",
    "questioned whether",
    "?",
)

_LOW_SIGNAL_CUES = (
    "thanks",
    "thank you",
    "good morning",
    "good afternoon",
    "sounds good",
    "just kidding",
)


def _has_commitment(item: ExtractedItem) -> bool:
    haystack = " ".join(
        [
            item.text.lower(),
            item.context.lower() if item.context else "",
            item.evidence.quote.lower() if item.evidence and item.evidence.quote else "",
        ]
    )
    return any(cue in haystack for cue in _COMMITMENT_CUES)


def _has_question_language(item: ExtractedItem) -> bool:
    haystack = " ".join(
        [
            item.text.lower(),
            item.context.lower() if item.context else "",
            item.evidence.quote.lower() if item.evidence and item.evidence.quote else "",
        ]
    )
    return any(cue in haystack for cue in _QUESTION_CUES)


def validate_items(items: list[ExtractedItem]) -> tuple[list[ExtractedItem], list[str]]:
    """Validate and adjust extracted items using deterministic rules."""
    warnings: list[str] = []
    validated: list[ExtractedItem] = []

    for item in items:
        haystack = " ".join(
            [
                item.text.lower(),
                item.context.lower() if item.context else "",
                item.evidence.quote.lower() if item.evidence and item.evidence.quote else "",
            ]
        )

        if item.type == "decision":
            if not item.evidence.turn_ids:
                item.validation_flags.append("decision_missing_evidence")
            if not _has_commitment(item):
                item.validation_flags.append("decision_missing_commitment")
            if _has_question_language(item):
                item.validation_flags.append("decision_unresolved_language")
            if item.validation_flags:
                if _has_question_language(item):
                    item.type = "question"
                    item.status = "open"
                else:
                    item.type = "discussion_point"
                    item.status = "open"

        if item.type == "action":
            if not item.owner:
                item.owner = "TBD"
                item.validation_flags.append("owner_missing")
            if not item.evidence.turn_ids:
                item.validation_flags.append("action_missing_evidence")

        if item.type == "question":
            item.status = "open"

        if item.type in {"proposal", "observation", "direction"}:
            item.type = "discussion_point"
            item.status = "open"

        if item.type == "issue" and item.importance < 0.45:
            item.type = "discussion_point"
            item.validation_flags.append("issue_low_impact")

        if any(cue in haystack for cue in _LOW_SIGNAL_CUES):
            item.dropped = True
            item.validation_flags.append("low_signal")

        item.validated = item.type != "decision" or not any(
            flag in item.validation_flags for flag in ("decision_missing_evidence", "decision_missing_commitment", "decision_unresolved_language")
        )
        if item.type != "decision":
            item.validated = True

        validated.append(item)

    for item in validated:
        if item.type == "decision" and item.status != "confirmed":
            warnings.append(f"{item.id}: decision not confirmed")
        if item.type == "action" and item.owner == "TBD":
            warnings.append(f"{item.id}: action owner unresolved")

    return validated, warnings


def validated_to_structured(items: list[ExtractedItem]) -> StructuredItems:
    structured = StructuredItems()
    for item in items:
        if item.type == "action":
            structured.actions.append(item)
        elif item.type == "decision":
            structured.decisions.append(item)
        elif item.type == "constraint":
            structured.constraints.append(item)
        elif item.type == "issue":
            structured.issues.append(item)
        elif item.type == "question":
            structured.questions.append(item)
        elif item.type == "discussion_point":
            structured.discussion_points.append(item)
    return structured
