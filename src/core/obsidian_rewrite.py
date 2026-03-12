"""Pure helpers for rebuilding Obsidian exports from stored Sidekick data."""

from __future__ import annotations

import json
from datetime import datetime

from src.core.datetime_utils import localize_datetime, timezone_label
from src.core.markdown_utils import (
    build_obsidian_markdown,
    format_datetime_human,
    format_duration_human,
    format_processing_time,
)
from src.core.obsidian_exports import meeting_display_id, meeting_month_folder
from src.core.speaker_labels import (
    build_user_facing_speaker_map,
    infer_strict_segment_speakers,
    resolve_user_facing_speaker_name,
)
from src.summarization.prompts import TEMPLATE_INFO


def safe_json_loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def summary_revision_history_from_json(workflow_data_json: str | None) -> list[dict]:
    workflow_data = safe_json_loads(workflow_data_json, {})
    history = workflow_data.get("revision_history", []) if isinstance(workflow_data, dict) else []
    return history if isinstance(history, list) else []


def _speaker_identity(segment) -> str | None:
    if hasattr(segment, "keys"):
        return segment["speaker_cluster"] or segment["speaker"] or None
    return getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None) or None


def _segment_value(segment, key: str, default=None):
    if hasattr(segment, "keys"):
        return segment[key] if key in segment.keys() else default
    return getattr(segment, key, default)


def segments_to_transcript(segments: list) -> tuple[str, float]:
    """Format transcript segments into timestamped export text and duration."""
    lines: list[str] = []
    duration = 0.0
    inferred_speakers = infer_strict_segment_speakers(segments)
    fallback_map = build_user_facing_speaker_map(
        (
            (inferred or {}).get("speaker_cluster")
            or _speaker_identity(segment)
            or (inferred or {}).get("speaker")
        )
        for segment, inferred in zip(segments, inferred_speakers)
    )
    for segment, inferred in zip(segments, inferred_speakers):
        start_time = float(_segment_value(segment, "start_time", 0.0) or 0.0)
        end_time = float(_segment_value(segment, "end_time", 0.0) or 0.0)
        mins = int(start_time // 60)
        secs = int(start_time % 60)
        marker = " [IMPORTANT]" if _segment_value(segment, "is_important", False) else ""
        effective_speaker = _segment_value(segment, "speaker") or (inferred or {}).get("speaker")
        effective_raw_label = (
            _speaker_identity(segment)
            or (inferred or {}).get("speaker_cluster")
            or (inferred or {}).get("speaker")
        )
        speaker = resolve_user_facing_speaker_name(
            effective_speaker,
            effective_raw_label,
            fallback_map,
        )
        speaker_prefix = f"{speaker}: " if speaker else ""
        lines.append(
            f"[{mins:02d}:{secs:02d}]{marker} {speaker_prefix}{_segment_value(segment, 'text', '')}"
        )
        duration = max(duration, end_time)
    return "\n".join(lines).strip(), duration


def build_saved_summary_frontmatter(
    *,
    meeting_id: str,
    summary_id: str,
    transcript_version_id: str | None,
    summary_version_number: int,
    transcript_version_number: int | None,
    local_started_at: datetime,
    template_key: str | None,
    recording_duration_minutes: int,
    tags: list[str],
    exported_at_local: datetime | None,
    export_status: str,
) -> dict[str, object]:
    frontmatter: dict[str, object] = {
        "type": "meeting-note",
        "sidekick_meeting_id": meeting_id,
        "sidekick_summary_id": summary_id,
        "sidekick_transcript_version_id": transcript_version_id,
        "sidekick_summary_version": summary_version_number,
        "sidekick_export_status": export_status,
        "meeting_date": local_started_at.strftime("%Y-%m-%d"),
        "meeting_month": meeting_month_folder(local_started_at),
        "template_key": template_key or "",
        "recording_duration_minutes": recording_duration_minutes,
        "tags": list(tags or []),
    }
    if transcript_version_number:
        frontmatter["sidekick_transcript_version"] = transcript_version_number
    if exported_at_local:
        frontmatter["exported_at"] = exported_at_local.isoformat()
    return frontmatter


def build_saved_summary_markdown(
    *,
    meeting_id: str,
    summary_id: str,
    transcript_version_id: str | None,
    summary_version_number: int,
    transcript_version_number: int | None,
    meeting_title: str | None,
    template_key: str | None,
    template_label: str | None,
    summary_content: str,
    processing_duration_seconds: float | None,
    workflow_data_json: str | None,
    pass1_system_prompt: str | None,
    pass1_user_prompt: str | None,
    pass2_system_prompt: str | None,
    pass2_user_prompt: str | None,
    session_started_at: datetime,
    session_timezone_name: str | None,
    session_timezone_offset_minutes: int | None,
    transcript_segments: list,
    tags: list[str],
    export_status: str,
    exported_at_source: datetime | None,
) -> str:
    transcript, audio_duration_seconds = segments_to_transcript(transcript_segments)
    if not transcript:
        raise ValueError("Stored transcript segments are empty")

    local_started_at = localize_datetime(
        session_started_at,
        session_timezone_name,
        session_timezone_offset_minutes,
    )
    tz_label = timezone_label(session_timezone_name, session_timezone_offset_minutes)
    exported_at_local = localize_datetime(
        exported_at_source or session_started_at,
        session_timezone_name,
        session_timezone_offset_minutes,
    )
    frontmatter = build_saved_summary_frontmatter(
        meeting_id=meeting_id,
        summary_id=summary_id,
        transcript_version_id=transcript_version_id,
        summary_version_number=summary_version_number,
        transcript_version_number=transcript_version_number,
        local_started_at=local_started_at,
        template_key=template_key,
        recording_duration_minutes=max(1, int(audio_duration_seconds // 60)),
        tags=tags,
        exported_at_local=exported_at_local,
        export_status=export_status,
    )
    resolved_template_label = (
        template_label
        or TEMPLATE_INFO.get(template_key or "", {}).get("name")
        or (template_key or "meeting").replace("_", " ").title()
    )
    revision_history = summary_revision_history_from_json(workflow_data_json)
    return build_obsidian_markdown(
        content=(summary_content or "").strip(),
        template_label=resolved_template_label,
        recorded_at=format_datetime_human(local_started_at, tz_label),
        exported_at=format_datetime_human(exported_at_local, tz_label),
        duration_str=format_duration_human(int(audio_duration_seconds)),
        processing_time_str=(
            format_processing_time(processing_duration_seconds)
            if processing_duration_seconds
            else ""
        ),
        transcript=transcript,
        meeting_display_id=meeting_display_id(meeting_id),
        summary_version_number=summary_version_number,
        transcript_version_number=transcript_version_number,
        frontmatter=frontmatter,
        pass1_system_prompt=pass1_system_prompt,
        pass1_user_prompt=pass1_user_prompt,
        pass2_system_prompt=pass2_system_prompt,
        pass2_user_prompt=pass2_user_prompt,
        revision_history=revision_history,
    )
