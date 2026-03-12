"""Markdown formatting utilities for summaries and Obsidian exports."""

import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_PASS1_CONTEXT_BLOCK_RE = re.compile(
    r"\n## Source Context \([^)]+\)\n.*?\n(?=Follow (?:the|exact) )",
    re.DOTALL,
)
_PASS2_DRAFT_BLOCK_RE = re.compile(
    r"\nDraft:\n.*?\n\nReturn ONLY the final edited output\.",
    re.DOTALL,
)


def format_duration_human(seconds: int) -> str:
    """Return e.g. '45 min' or '1 hour 45 min'."""
    minutes = seconds // 60
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    remaining = minutes % 60
    hour_word = "hour" if hours == 1 else "hours"
    if remaining == 0:
        return f"{hours} {hour_word}"
    return f"{hours} {hour_word} {remaining} min"


def format_processing_time(seconds: float) -> str:
    """Return e.g. '4m 05s' or '45s'."""
    total = int(seconds)
    m, s = divmod(total, 60)
    if m == 0:
        return f"{s}s"
    return f"{m}m {s:02d}s"


def format_datetime_human(dt: datetime, tz_label: str) -> str:
    """Return e.g. 'March 16, 2026 at 2:30pm (CST)'."""
    month = dt.strftime("%B")
    day = dt.day
    year = dt.year
    hour_12 = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p").lower()
    time_str = f"{hour_12}:{minute}{ampm}" if minute != "00" else f"{hour_12}{ampm}"
    return f"{month} {day}, {year} at {time_str} ({tz_label})"


def week_folder(dt: datetime) -> str:
    """Return e.g. '2026 Week 12' for the ISO week containing dt. Zero-pads for correct sort."""
    iso_year, week_num, _ = dt.isocalendar()
    return f"{iso_year} Week {week_num:02d}"


def _sanitize_prompt_for_note(prompt: Optional[str], pass_title: str, role: str) -> str:
    if not prompt or not prompt.strip():
        return ""

    cleaned = prompt.strip()

    if role == "user" and pass_title == "Pass 1":
        cleaned = _PASS1_CONTEXT_BLOCK_RE.sub(
            "\n## Source Context\n[Omitted from note: transcript/context payload]\n\n",
            cleaned,
        )
    elif role == "user" and pass_title == "Pass 2":
        cleaned = _PASS2_DRAFT_BLOCK_RE.sub(
            "\nDraft:\n[Omitted from note: draft summary payload]\n\nReturn ONLY the final edited output.",
            cleaned,
        )

    return cleaned


def _build_folded_callout(title: str, body: str, callout_type: str = "note") -> str:
    """Return an Obsidian foldable callout collapsed by default."""
    cleaned = body.strip()
    if not cleaned:
        return ""
    quoted_lines = ["> " + line if line else ">" for line in cleaned.splitlines()]
    return f"> [!{callout_type}]- {title}\n" + "\n".join(quoted_lines)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _build_frontmatter(frontmatter: dict[str, Any] | None) -> str:
    if not frontmatter:
        return ""
    lines = ["---"]
    for key, value in frontmatter.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def build_obsidian_markdown(
    content: str,
    template_label: str,
    recorded_at: str,
    exported_at: str,
    duration_str: str,
    processing_time_str: str,
    transcript: str,
    meeting_display_id: Optional[str] = None,
    summary_version_number: Optional[int] = None,
    transcript_version_number: Optional[int] = None,
    frontmatter: Optional[dict[str, Any]] = None,
    pass1_system_prompt: Optional[str] = None,
    pass1_user_prompt: Optional[str] = None,
    pass2_system_prompt: Optional[str] = None,
    pass2_user_prompt: Optional[str] = None,
    revision_history: Optional[list[dict]] = None,
    revision_instruction: Optional[str] = None,
) -> str:
    """Assemble the final Obsidian markdown note."""
    revision_items: list[str] = []
    if revision_history:
        revision_items = []
        for entry in revision_history:
            if not isinstance(entry, dict):
                continue
            instruction = str(entry.get("instruction") or "").strip()
            if not instruction:
                continue
            created_at = str(entry.get("created_at") or "").strip()
            route = str(entry.get("route") or "").strip().replace("_", " ")
            used_transcript_context = bool(entry.get("used_transcript_context"))
            transcript_version_number = entry.get("transcript_version_number")
            detail_parts = []
            if created_at:
                detail_parts.append(created_at)
            if route:
                detail_parts.append(route.title())
            if used_transcript_context:
                detail_parts.append("Transcript-backed")
            if transcript_version_number:
                detail_parts.append(f"Transcript v{transcript_version_number}")
            detail_label = " | ".join(detail_parts)
            revision_items.append(
                f"- {detail_label}: {instruction}" if detail_label else f"- {instruction}"
            )
    elif revision_instruction and revision_instruction.strip():
        revision_items.append(f'- Revised: "{revision_instruction.strip()}"')

    revision_section = ""
    if revision_items:
        revision_section = _build_folded_callout(
            "Revision History",
            "\n".join(revision_items),
            callout_type="abstract",
        )
    
    # Ensure content has proper spacing for Obsidian
    content = content.strip()
    
    info_lines = [
        f"**Template**: {template_label}",
        f"**Recorded**: {recorded_at}",
        f"**Exported**: {exported_at}",
        f"**Meeting Length**: {duration_str}",
    ]
    if meeting_display_id:
        info_lines.insert(1, f"**Sidekick ID**: {meeting_display_id}")
    if summary_version_number:
        info_lines.append(f"**Summary Version**: v{summary_version_number}")
    if transcript_version_number:
        info_lines.append(f"**Transcript Version**: t{transcript_version_number}")
    if processing_time_str and processing_time_str != "N/A":
        info_lines.append(f"**Processing Time**: {processing_time_str}")

    info_section = _build_folded_callout(
        "Meeting Info",
        "\n".join(info_lines),
        callout_type="info",
    )

    prompt_sections = ""
    if any(
        part and part.strip()
        for part in (pass1_system_prompt, pass1_user_prompt, pass2_system_prompt, pass2_user_prompt)
    ):
        prompt_parts = []

        def _append_prompt_block(pass_title: str, role_title: str, prompt: Optional[str], role: str) -> None:
            note_prompt = _sanitize_prompt_for_note(prompt, pass_title, role)
            if not note_prompt:
                return
            prompt_parts.append(
                _build_folded_callout(
                    f"{pass_title}: {role_title}",
                    f"```text\n{note_prompt}\n```",
                )
            )

        _append_prompt_block("Pass 1", "System Prompt", pass1_system_prompt, "system")
        _append_prompt_block("Pass 1", "User Prompt", pass1_user_prompt, "user")
        _append_prompt_block("Pass 2", "System Prompt", pass2_system_prompt, "system")
        _append_prompt_block("Pass 2", "User Prompt", pass2_user_prompt, "user")

        prompt_sections = "\n\n".join(prompt_parts)
        if prompt_sections:
            prompt_sections = f"{prompt_sections}\n\n"

    transcript_section = _build_folded_callout(
        "Transcript",
        f"```text\n{transcript}\n```",
    )

    frontmatter_block = _build_frontmatter(frontmatter)
    
    return (
        f"{frontmatter_block + '\n\n' if frontmatter_block else ''}"
        f"{info_section}\n\n"
        f"---\n\n"
        f"{content}\n"
        f"\n---\n\n"
        f"{revision_section + '\n\n' if revision_section else ''}"
        f"{prompt_sections}"
        f"{transcript_section}\n"
    )
