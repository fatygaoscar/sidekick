"""Concise stable renderer for meeting notes."""

from __future__ import annotations

from .ranking import RenderSelection, select_for_render
from .types import ExtractedItem, MeetingContextProfile, StructuredItems, TopicSegment


def _default_context_profile() -> MeetingContextProfile:
    return MeetingContextProfile(
        ui_summary_mode="auto",
        primary_mode="general_business",
        priority_weights={},
        render_profile="concise_default_v1",
    )


def _summary_bullet(item: ExtractedItem) -> str:
    if item.type == "decision":
        return f"- Decision: {item.text.rstrip('.') }."
    if item.type == "action":
        owner = item.owner or "TBD"
        return f"- Action: {owner} to {item.text.rstrip('.') }."
    if item.type == "constraint":
        return f"- Constraint: {item.text.rstrip('.') }."
    if item.type == "issue":
        return f"- Issue: {item.text.rstrip('.') }."
    if item.type == "question":
        return f"- Open question: {item.text.rstrip('.') }."
    return f"- Main discussion: {item.text.rstrip('.') }."


def _render_summary(selection: RenderSelection) -> str:
    bullets = []
    seen: set[str] = set()
    for item in selection.summary_items:
        bullet = _summary_bullet(item)
        if bullet not in seen:
            bullets.append(bullet)
            seen.add(bullet)
    return "\n".join(bullets[:3] or ["- The meeting focused on decisions, follow-ups, and key discussion points."])


def _render_decisions(selection: RenderSelection) -> str:
    if not selection.decisions:
        return "- None"
    return "\n".join(f"- {item.text}" for item in selection.decisions[:5])


def _render_actions(selection: RenderSelection) -> str:
    lines = ["| Owner | Action | Due |", "|-------|--------|-----|"]
    if not selection.actions:
        lines.append("| TBD | None | |")
        return "\n".join(lines)
    for item in selection.actions[:6]:
        lines.append(f"| {item.owner or 'TBD'} | {item.text} | {item.due_date or ''} |")
    return "\n".join(lines)


def _discussion_bullet(item: ExtractedItem) -> str:
    if item.type == "constraint":
        return f"- Constraint: {item.text.rstrip('.') }."
    if item.type == "question":
        return f"- Open question: {item.text.rstrip('.') }."
    if item.type == "issue":
        return f"- Issue: {item.text.rstrip('.') }."
    if item.speaker:
        return f"- Main point: {item.speaker} raised {item.text.rstrip('.') }."
    return f"- {item.text.rstrip('.') }."


def _render_discussion(selection: RenderSelection) -> str:
    if not selection.discussion_topics:
        return "- None"

    lines: list[str] = []
    for topic in selection.discussion_topics[:3]:
        lines.append(f"### {topic.label}")
        lines.append("")
        rendered = []
        seen: set[str] = set()
        for item in topic.items[:3]:
            bullet = _discussion_bullet(item)
            if bullet not in seen:
                rendered.append(bullet)
                seen.add(bullet)
        lines.extend(rendered[:3] or ["- No high-signal discussion points remained after filtering."])
        lines.append("")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def render_selection(selection: RenderSelection, context_profile: MeetingContextProfile | None = None) -> str:
    """Render strict-budget four-section meeting notes from a selected item set."""
    context_profile = context_profile or _default_context_profile()
    parts = [
        "## Summary",
        "",
        _render_summary(selection),
        "",
        "## Key Decisions",
        "",
        _render_decisions(selection),
        "",
        "## Action Items",
        "",
        _render_actions(selection),
        "",
        "## Discussion Notes",
        "",
        _render_discussion(selection),
    ]
    return "\n".join(parts).strip()


def render_meeting_summary(
    items: StructuredItems,
    context_profile: MeetingContextProfile | None = None,
    topics: list[TopicSegment] | None = None,
) -> str:
    """Render strict-budget four-section meeting notes from validated items."""
    context_profile = context_profile or _default_context_profile()
    topics = topics or []
    selection = select_for_render(items, topics)
    return render_selection(selection, context_profile)
