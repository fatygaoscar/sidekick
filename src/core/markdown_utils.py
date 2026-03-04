"""Markdown formatting utilities for summaries and Obsidian exports."""

import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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


def build_obsidian_markdown(
    content: str,
    template_label: str,
    recorded_at: str,
    exported_at: str,
    duration_str: str,
    processing_time_str: str,
    transcript: str,
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
        f"<details>\n<summary>Full Transcript</summary>\n\n"
        f"{transcript}\n\n"
        f"</details>\n"
    )
