"""Markdown formatting utilities for summaries and Obsidian exports."""

import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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


def build_obsidian_markdown(
    content: str,
    template_label: str,
    recorded_at: str,
    exported_at: str,
    duration_str: str,
    processing_time_str: str,
    transcript: str,
    pass1_system_prompt: Optional[str] = None,
    pass1_user_prompt: Optional[str] = None,
    pass2_system_prompt: Optional[str] = None,
    pass2_user_prompt: Optional[str] = None,
    revision_instruction: Optional[str] = None,
) -> str:
    """Assemble the final Obsidian markdown note."""
    revision_line = ""
    if revision_instruction and revision_instruction.strip():
        revision_line = f'**Revised**: "{revision_instruction.strip()}"\n'
    
    # Ensure content has proper spacing for Obsidian
    content = content.strip()
    
    processing_line = ""
    if processing_time_str and processing_time_str != "N/A":
        processing_line = f"**Processing Time**: {processing_time_str}\n"

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
            prompt_parts.append(f"### {pass_title}: {role_title}\n\n```text\n{note_prompt}\n```")

        _append_prompt_block("Pass 1", "System Prompt", pass1_system_prompt, "system")
        _append_prompt_block("Pass 1", "User Prompt", pass1_user_prompt, "user")
        _append_prompt_block("Pass 2", "System Prompt", pass2_system_prompt, "system")
        _append_prompt_block("Pass 2", "User Prompt", pass2_user_prompt, "user")

        prompt_sections = "\n\n".join(prompt_parts)
        if prompt_sections:
            prompt_sections = f"{prompt_sections}\n\n"
    
    return (
        f"**Template**: {template_label}\n"
        f"{revision_line}"
        f"**Recorded**: {recorded_at}\n"
        f"**Exported**: {exported_at}\n"
        f"**Meeting Length**: {duration_str}\n"
        f"{processing_line}"
        f"\n---\n\n"
        f"{content}\n"
        f"\n---\n\n"
        f"{prompt_sections}"
        f"<details>\n<summary>Transcript</summary>\n\n"
        f"```text\n{transcript}\n```\n\n"
        f"</details>\n"
    )
