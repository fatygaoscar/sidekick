"""Narrative generation pass for the pipeline.

Generates narrative summary from structured items, with coverage checking
and patching to ensure all items are referenced.
"""

import re
from typing import Callable, Awaitable, Optional

from .types import ExtractedItem, StructuredItems


# Type alias for the LLM call function
LLMCallFunc = Callable[[str, str], Awaitable[str]]


def get_narrative_system_prompt(template: str) -> str:
    """Get the system prompt for narrative generation.

    Args:
        template: Template type being used

    Returns:
        System prompt string
    """
    return f"""You are an expert meeting summarizer creating a narrative summary for a {template} meeting.

You have been provided with structured items (actions, decisions, risks, questions, follow-ups) that were extracted from this meeting. Your task is to write a flowing narrative summary that:

1. **References the structured items** by their IDs (e.g., "We decided to proceed with the new architecture (D-001)")
2. **Tells the story** of the meeting in a logical flow
3. **Maintains the template's style** - formal for strategic reviews, conversational for standups, detailed for working sessions
4. **Covers all items** - every item ID should appear at least once in your narrative
5. **Groups related items** - don't just list items, weave them into coherent paragraphs

Structure Guidelines:
- Start with an executive summary (2-3 sentences)
- Organize by topic/theme rather than by item type
- Use natural transitions between topics
- Reference item IDs in parentheses after mentioning them
- End with clear next steps referencing relevant action/follow-up IDs

Example of good referencing:
"The team agreed to migrate to the new API (D-001). John will lead the migration effort (A-001) with a target completion by end of month (A-002). There's concern about backwards compatibility (R-001), which Sarah will investigate (F-001)."

Do NOT:
- Simply list items in a table format (that's provided separately)
- Skip any item IDs
- Invent new items not in the provided list
- Use generic phrasing that doesn't reference specific items"""


def get_narrative_user_prompt(
    items: StructuredItems,
    template: str,
    transcript_preview: str,
) -> str:
    """Get the user prompt for narrative generation.

    Args:
        items: Structured items to reference
        template: Template type
        transcript_preview: First/last portions of transcript for context

    Returns:
        User prompt string
    """
    # Build items summary
    item_lines = []

    if items.actions:
        item_lines.append("**Actions:**")
        for item in items.actions:
            owner_str = f" (Owner: {item.owner})" if item.owner else ""
            due_str = f" [Due: {item.due_date}]" if item.due_date else ""
            item_lines.append(f"- {item.id}: {item.text}{owner_str}{due_str}")

    if items.decisions:
        item_lines.append("\n**Decisions:**")
        for item in items.decisions:
            rationale_str = f" - Rationale: {item.rationale}" if item.rationale else ""
            item_lines.append(f"- {item.id}: {item.text}{rationale_str}")

    if items.risks:
        item_lines.append("\n**Risks:**")
        for item in items.risks:
            impact_str = f" - Impact: {item.impact}" if item.impact else ""
            item_lines.append(f"- {item.id}: {item.text}{impact_str}")

    if items.questions:
        item_lines.append("\n**Open Questions:**")
        for item in items.questions:
            who_str = f" (Needs input from: {item.who_decides})" if item.who_decides else ""
            item_lines.append(f"- {item.id}: {item.text}{who_str}")

    if items.followups:
        item_lines.append("\n**Follow-ups:**")
        for item in items.followups:
            owner_str = f" (Owner: {item.owner})" if item.owner else ""
            item_lines.append(f"- {item.id}: {item.text}{owner_str}")

    items_text = "\n".join(item_lines) if item_lines else "_No items extracted._"

    return f"""Write a narrative summary for this {template} meeting.

**STRUCTURED ITEMS TO REFERENCE:**
{items_text}

**TRANSCRIPT CONTEXT:**
{transcript_preview}

---

Write a flowing narrative summary that references ALL of the above item IDs.
Remember to include each ID in parentheses when mentioning that item.
Organize by topic/theme, not by item type."""


def check_coverage(narrative: str, items: StructuredItems) -> tuple[float, list[str]]:
    """Check how many item IDs are referenced in the narrative.

    Args:
        narrative: Generated narrative text
        items: Structured items that should be referenced

    Returns:
        Tuple of (coverage_score 0-1, list of missing IDs)
    """
    all_ids = items.all_ids()
    if not all_ids:
        return 1.0, []

    missing_ids = []
    for item_id in all_ids:
        # Check if ID appears in narrative
        if item_id not in narrative:
            missing_ids.append(item_id)

    coverage = (len(all_ids) - len(missing_ids)) / len(all_ids)
    return coverage, missing_ids


def get_patch_system_prompt() -> str:
    """Get the system prompt for patching missing coverage."""
    return """You are helping complete a meeting summary that's missing some item references.

You will be given:
1. The current narrative summary
2. A list of item IDs that are NOT yet mentioned
3. Details about those missing items

Your task is to write 1-2 short paragraphs that naturally incorporate the missing items.
These paragraphs will be APPENDED to the existing summary.

Guidelines:
- Reference each missing ID in parentheses
- Keep the same tone and style as the existing narrative
- Create natural groupings (e.g., "Additionally, several action items were noted...")
- Be concise - this is supplementary content"""


def get_patch_user_prompt(
    narrative: str,
    missing_items: list[ExtractedItem],
) -> str:
    """Get the user prompt for patching.

    Args:
        narrative: Current narrative (last 500 chars for context)
        missing_items: Items that need to be added

    Returns:
        User prompt string
    """
    # Get narrative tail for context
    context = narrative[-500:] if len(narrative) > 500 else narrative

    # Build missing items text
    missing_text_lines = []
    for item in missing_items:
        detail = f"{item.id} ({item.type}): {item.text}"
        if item.owner:
            detail += f" [Owner: {item.owner}]"
        missing_text_lines.append(f"- {detail}")

    missing_text = "\n".join(missing_text_lines)
    return f"""**END OF CURRENT SUMMARY:**
...{context}

**MISSING ITEMS TO ADD:**
{missing_text}

Write 1-2 paragraphs to naturally incorporate these missing items.
Reference each ID in parentheses."""


async def generate_narrative(
    items: StructuredItems,
    template: str,
    transcript: str,
    llm_call: LLMCallFunc,
    min_coverage: float = 0.9,
) -> tuple[str, float]:
    """Generate narrative summary from structured items.

    Args:
        items: Structured items to reference
        template: Template type for tone/style
        transcript: Full transcript (used for context preview)
        llm_call: Async function to call the LLM
        min_coverage: Minimum coverage score before patching

    Returns:
        Tuple of (narrative_text, coverage_score)
    """
    # Create transcript preview (first and last portions)
    lines = transcript.strip().split("\n")
    if len(lines) <= 30:
        preview = transcript
    else:
        first_lines = "\n".join(lines[:15])
        last_lines = "\n".join(lines[-15:])
        preview = f"{first_lines}\n\n[... middle of meeting omitted for brevity ...]\n\n{last_lines}"

    # Generate initial narrative
    system_prompt = get_narrative_system_prompt(template)
    user_prompt = get_narrative_user_prompt(items, template, preview)

    narrative = await llm_call(system_prompt, user_prompt)

    # Check coverage
    coverage, missing_ids = check_coverage(narrative, items)

    # If coverage is sufficient, return
    if coverage >= min_coverage:
        return narrative, coverage

    # Otherwise, patch the missing items
    missing_items = [item for item in items.all_items() if item.id in missing_ids]

    if missing_items:
        patch_system = get_patch_system_prompt()
        patch_user = get_patch_user_prompt(narrative, missing_items)

        patch_text = await llm_call(patch_system, patch_user)

        # Append patch to narrative
        narrative = f"{narrative}\n\n{patch_text}"

        # Recheck coverage
        coverage, _ = check_coverage(narrative, items)

    return narrative, coverage
