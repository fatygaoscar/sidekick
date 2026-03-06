"""Session and meeting REST endpoints."""

import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.settings import get_settings
from src.core.datetime_utils import localize_datetime, timezone_label, to_utc_iso
from src.core.markdown_utils import (
    build_obsidian_markdown,
    format_datetime_human,
    format_duration_human,
    format_processing_time,
    week_folder,
)
from src.audio.storage import (
    assemble_chunks,
    cleanup_chunk_storage,
    clear_session_chunk_upload_state,
    ensure_session_audio_path,
    extension_from_content_type,
    get_audio_dir,
    get_available_chunks,
    get_missing_chunk_indices,
    get_session_audio_candidates,
    get_session_audio_path,
    media_type_for_path,
    write_chunk,
)
from src.sessions.manager import SessionManager
from src.sessions.repository import Repository, UNSET
from src.summarization.manager import SummarizationManager


router = APIRouter()
logger = logging.getLogger(__name__)


def _sanitize_title_for_filename(title: str) -> str:
    """Sanitize title for safe filesystem download names."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    return sanitized or "Untitled Recording"


def _build_formatted_title(
    started_at: datetime,
    title: str | None,
    timezone_name: str | None = None,
    timezone_offset_minutes: int | None = None,
) -> str:
    """Build the canonical recording title shown in history."""
    local_started_at = localize_datetime(started_at, timezone_name, timezone_offset_minutes)
    timestamp = local_started_at.strftime("%Y-%m-%d-%H%M")
    base_title = (title or "Untitled Recording").strip() or "Untitled Recording"
    return f"{timestamp} - {base_title}"


def _build_recorded_labels(
    started_at: datetime,
    timezone_name: str | None,
    timezone_offset_minutes: int | None,
) -> tuple[str, str, str]:
    local_started_at = localize_datetime(started_at, timezone_name, timezone_offset_minutes)
    date_label = local_started_at.strftime("%b %d, %Y")
    time_label = local_started_at.strftime("%I:%M %p").lstrip("0")
    tz_label = timezone_label(timezone_name, timezone_offset_minutes)
    return date_label, time_label, tz_label


def _has_exported_note_in_vault(
    vault_path: str | None,
    started_at: datetime,
    title: str | None,
    timezone_name: str | None,
    timezone_offset_minutes: int | None,
) -> bool:
    """Best-effort check for an exported note file for this recording."""
    return _find_exported_note_filename(
        vault_path=vault_path,
        started_at=started_at,
        title=title,
        timezone_name=timezone_name,
        timezone_offset_minutes=timezone_offset_minutes,
    ) is not None


def _find_exported_note_filename(
    vault_path: str | None,
    started_at: datetime,
    title: str | None,
    timezone_name: str | None,
    timezone_offset_minutes: int | None,
) -> str | None:
    """Find the most recent exported note filename for a recording."""
    if not vault_path:
        return None
    try:
        if not os.path.isdir(vault_path):
            return None
        prefix = _build_formatted_title(
            started_at,
            title,
            timezone_name=timezone_name,
            timezone_offset_minutes=timezone_offset_minutes,
        )
        # Export filenames use: "{prefix} [Template].md"
        file_prefix = f"{prefix} ["
        matches = [
            name
            for name in os.listdir(vault_path)
            if name.startswith(file_prefix) and name.endswith(".md")
        ]
        if not matches:
            return None

        matches.sort(
            key=lambda name: os.path.getmtime(os.path.join(vault_path, name)),
            reverse=True,
        )
        return matches[0]
    except Exception:
        return None


def get_session_manager(request: Request) -> SessionManager:
    """Dependency to get session manager."""
    return request.app.state.session_manager


def get_summarization_manager(request: Request) -> SummarizationManager:
    """Dependency to get summarization manager."""
    return request.app.state.summarization_manager


def get_repository(request: Request) -> Repository:
    """Dependency to get repository."""
    return request.app.state.repository


def _normalize_optional_text(value: str | None) -> str:
    return (value or "").strip()


def _can_resolve_speakers_from_attendees(meeting) -> bool:
    return bool(
        meeting
        and meeting.speaker_review_required
        and meeting.speaker_review_completed_at is None
        and _normalize_optional_text(getattr(meeting, "attendees", None))
    )


def _is_generic_speaker(label: str | None) -> bool:
    return bool(label and label.startswith("SPEAKER_"))


def _transcript_requires_speaker_review(segments: list) -> bool:
    raw_speakers = {
        getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)
        for segment in segments
    }
    unresolved = {speaker for speaker in raw_speakers if _is_generic_speaker(speaker)}
    return len(unresolved) > 1


def _serialize_summary(summary) -> dict:
    return {
        "id": str(summary.id),
        "meeting_id": str(summary.meeting_id),
        "content": summary.content,
        "backend": summary.backend,
        "model": summary.model,
        "created_at": to_utc_iso(summary.created_at),
        "processing_duration_seconds": summary.processing_duration_seconds,
        "template": summary.template,
        "template_key": summary.template_key,
        "status": summary.status,
        "source_type": summary.source_type,
        "saved_to_obsidian_at": to_utc_iso(summary.saved_to_obsidian_at),
        "obsidian_relative_path": summary.obsidian_relative_path,
    }


def _serialize_transcript_segments(segments: list) -> list[dict]:
    transcript_lines = []
    for segment in segments:
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        transcript_lines.append(
            {
                "id": str(segment.id),
                "timestamp": f"[{mins:02d}:{secs:02d}]",
                "text": segment.text,
                "speaker": getattr(segment, "speaker", None),
                "speaker_cluster": getattr(segment, "speaker_cluster", None),
                "is_important": segment.is_important,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
            }
        )
    return transcript_lines


def _build_speaker_cards(segments: list, session_id: str) -> list[dict]:
    grouped: dict[str, list] = {}
    for segment in segments:
        speaker_cluster = getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)
        if not speaker_cluster:
            continue
        grouped.setdefault(speaker_cluster, []).append(segment)

    cards: list[dict] = []
    for speaker_cluster, speaker_segments in sorted(grouped.items(), key=lambda item: item[0]):
        speaker_segments.sort(key=lambda segment: segment.start_time)
        first = speaker_segments[0]
        display_name = getattr(first, "speaker", None)
        clip_duration = min(5.0, max(0.5, float(first.end_time) - float(first.start_time)))
        cards.append(
            {
                "speaker_cluster": speaker_cluster,
                "display_name": None if display_name == speaker_cluster else display_name,
                "raw_label": speaker_cluster,
                "preview_text": first.text[:160],
                "clip_start": float(first.start_time),
                "clip_end": float(first.start_time) + clip_duration,
                "audio_url": f"/api/recordings/{session_id}/audio",
                "needs_name": _is_generic_speaker(speaker_cluster)
                and (not display_name or display_name == speaker_cluster),
            }
        )
    return cards


def _summary_is_out_of_date(meeting, summary) -> bool:
    if not summary:
        return False
    if (
        meeting.speaker_review_required
        and meeting.speaker_review_completed_at
        and summary.created_at < meeting.speaker_review_completed_at
    ):
        return True
    meeting_template_key = meeting.template_key or "meeting"
    if summary.template_key:
        if summary.template_key != meeting_template_key:
            return True
    elif meeting_template_key != "meeting":
        return True
    summary_prompt = _normalize_optional_text(summary.custom_prompt)
    meeting_prompt = _normalize_optional_text(meeting.custom_prompt)
    if (summary_prompt or meeting_prompt) and summary_prompt != meeting_prompt:
        return True
    summary_attendees = _normalize_optional_text(summary.attendees_snapshot)
    meeting_attendees = _normalize_optional_text(meeting.attendees)
    if (summary_attendees or meeting_attendees) and summary_attendees != meeting_attendees:
        return True
    return False


def _build_recording_workspace_state(
    session,
    meeting,
    segments: list,
    saved_summaries: list,
    draft_summary,
    latest_saved_summary,
) -> dict:
    current_summary = draft_summary or latest_saved_summary
    can_resolve_speakers_from_attendees = _can_resolve_speakers_from_attendees(meeting)
    return {
        "has_transcription": bool(session.has_transcription),
        "requires_speaker_review": bool(
            meeting and meeting.speaker_review_required and meeting.speaker_review_completed_at is None
        ),
        "can_resolve_speakers_from_attendees": can_resolve_speakers_from_attendees,
        "can_generate_summary": bool(
            session.has_transcription
            and (
                not meeting
                or not meeting.speaker_review_required
                or meeting.speaker_review_completed_at is not None
                or can_resolve_speakers_from_attendees
            )
        ),
        "has_unsaved_draft": draft_summary is not None,
        "summary_out_of_date": _summary_is_out_of_date(meeting, current_summary) if meeting else False,
        "transcript_segment_count": len(segments),
        "saved_summary_count": len(saved_summaries),
    }


# Request/Response models
class StartSessionRequest(BaseModel):
    mode: str = "work"
    submode: Optional[str] = None
    timezone_name: Optional[str] = None
    timezone_offset_minutes: Optional[int] = None


class StartMeetingRequest(BaseModel):
    title: Optional[str] = None


class UpdateMeetingRequest(BaseModel):
    title: Optional[str] = None


class MarkImportantRequest(BaseModel):
    note: Optional[str] = None
    duration_seconds: Optional[int] = None


class SummarizeRequest(BaseModel):
    prompt_type: str = "default"
    custom_instructions: Optional[str] = None


class FinalizeAudioRequest(BaseModel):
    mime_type: Optional[str] = None
    uploaded_chunks: Optional[int] = None
    expected_chunks: Optional[int] = None  # New: total expected chunk count


class SessionResponse(BaseModel):
    id: str
    mode: str
    submode: Optional[str]
    timezone_name: Optional[str]
    timezone_offset_minutes: Optional[int]
    is_active: bool
    started_at: str
    ended_at: Optional[str]

    class Config:
        from_attributes = True


class MeetingResponse(BaseModel):
    id: str
    session_id: str
    title: Optional[str]
    is_active: bool
    key_start: str
    key_stop: Optional[str]

    class Config:
        from_attributes = True


class ImportantMarkerResponse(BaseModel):
    id: str
    session_id: str
    meeting_id: Optional[str]
    marked_at: str
    duration_seconds: int
    note: Optional[str]

    class Config:
        from_attributes = True


class SummaryResponse(BaseModel):
    id: str
    meeting_id: str
    content: str
    backend: str
    model: str
    created_at: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]

    class Config:
        from_attributes = True


# Session endpoints
@router.post("/sessions", response_model=SessionResponse)
async def start_session(
    request: StartSessionRequest,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Start a new session."""
    session = await session_manager.start_session(
        mode=request.mode,
        submode=request.submode,
        timezone_name=request.timezone_name,
        timezone_offset_minutes=request.timezone_offset_minutes,
    )
    return SessionResponse(
        id=session.id,
        mode=session.mode,
        submode=session.submode,
        timezone_name=session.timezone_name,
        timezone_offset_minutes=session.timezone_offset_minutes,
        is_active=session.is_active,
        started_at=to_utc_iso(session.started_at),
        ended_at=to_utc_iso(session.ended_at),
    )


@router.get("/sessions/current", response_model=SessionResponse)
async def get_current_session(
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Get the current active session."""
    session = session_manager.current_session
    if not session:
        raise HTTPException(status_code=404, detail="No active session")
    return SessionResponse(
        id=session.id,
        mode=session.mode,
        submode=session.submode,
        timezone_name=session.timezone_name,
        timezone_offset_minutes=session.timezone_offset_minutes,
        is_active=session.is_active,
        started_at=to_utc_iso(session.started_at),
        ended_at=to_utc_iso(session.ended_at),
    )


@router.delete("/sessions/current")
async def end_current_session(
    session_manager: SessionManager = Depends(get_session_manager),
):
    """End the current session."""
    session = await session_manager.end_session()
    if not session:
        raise HTTPException(status_code=404, detail="No active session")
    return {"status": "ended", "session_id": session.id}


# Meeting endpoints
@router.post("/sessions/{session_id}/meetings", response_model=MeetingResponse)
async def start_meeting(
    session_id: str,
    request: StartMeetingRequest,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Start a new meeting (Key Start)."""
    if not session_manager.current_session:
        raise HTTPException(status_code=404, detail="No active session")
    if session_manager.current_session.id != session_id:
        raise HTTPException(status_code=400, detail="Session ID mismatch")

    meeting = await session_manager.start_meeting(title=request.title)
    if not meeting:
        raise HTTPException(status_code=500, detail="Failed to start meeting")

    return MeetingResponse(
        id=meeting.id,
        session_id=meeting.session_id,
        title=meeting.title,
        is_active=meeting.is_active,
        key_start=to_utc_iso(meeting.key_start),
        key_stop=to_utc_iso(meeting.key_stop),
    )


@router.put("/sessions/{session_id}/meetings/{meeting_id}", response_model=MeetingResponse)
async def end_or_update_meeting(
    session_id: str,
    meeting_id: str,
    request: UpdateMeetingRequest = None,
    action: str = "stop",
    session_manager: SessionManager = Depends(get_session_manager),
):
    """End a meeting (Key Stop) or update meeting details."""
    if not session_manager.current_meeting:
        raise HTTPException(status_code=404, detail="No active meeting")
    if session_manager.current_meeting.id != meeting_id:
        raise HTTPException(status_code=400, detail="Meeting ID mismatch")

    if action == "stop":
        meeting = await session_manager.end_meeting()
    else:
        # Just update title if provided
        meeting = session_manager.current_meeting
        # Note: would need to add title update to session manager

    if not meeting:
        raise HTTPException(status_code=500, detail="Failed to update meeting")

    return MeetingResponse(
        id=meeting.id,
        session_id=meeting.session_id,
        title=meeting.title,
        is_active=meeting.is_active,
        key_start=to_utc_iso(meeting.key_start),
        key_stop=to_utc_iso(meeting.key_stop),
    )


@router.get("/sessions/{session_id}/meetings/current", response_model=MeetingResponse)
async def get_current_meeting(
    session_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Get the current active meeting."""
    meeting = session_manager.current_meeting
    if not meeting:
        raise HTTPException(status_code=404, detail="No active meeting")
    return MeetingResponse(
        id=meeting.id,
        session_id=meeting.session_id,
        title=meeting.title,
        is_active=meeting.is_active,
        key_start=to_utc_iso(meeting.key_start),
        key_stop=to_utc_iso(meeting.key_stop),
    )


# Important marker endpoint
@router.post("/sessions/{session_id}/important", response_model=ImportantMarkerResponse)
async def mark_important(
    session_id: str,
    request: MarkImportantRequest,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Mark the current moment as important."""
    if not session_manager.current_session:
        raise HTTPException(status_code=404, detail="No active session")
    if session_manager.current_session.id != session_id:
        raise HTTPException(status_code=400, detail="Session ID mismatch")

    marker = await session_manager.mark_important(
        note=request.note,
        duration_seconds=request.duration_seconds,
    )

    return ImportantMarkerResponse(
        id=marker.id,
        session_id=marker.session_id,
        meeting_id=marker.meeting_id,
        marked_at=to_utc_iso(marker.marked_at),
        duration_seconds=marker.duration_seconds,
        note=marker.note,
    )


# Summarization endpoint
@router.post("/meetings/{meeting_id}/summarize", response_model=SummaryResponse)
async def summarize_meeting(
    meeting_id: str,
    request: SummarizeRequest,
    session_manager: SessionManager = Depends(get_session_manager),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Generate a summary for a meeting."""
    result = await summarization_manager.summarize_meeting(
        session_manager=session_manager,
        meeting_id=meeting_id,
        prompt_type=request.prompt_type,
        custom_instructions=request.custom_instructions,
    )

    # Save summary to database
    summary = await session_manager._repo.add_summary(
        meeting_id=meeting_id,
        content=result.content,
        backend=result.backend,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )

    return SummaryResponse(
        id=summary.id,
        meeting_id=summary.meeting_id,
        content=summary.content,
        backend=summary.backend,
        model=summary.model,
        created_at=to_utc_iso(summary.created_at),
        prompt_tokens=summary.prompt_tokens,
        completion_tokens=summary.completion_tokens,
    )


@router.get("/meetings/{meeting_id}/transcript")
async def get_meeting_transcript(
    meeting_id: str,
    include_important: bool = True,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Get the transcript for a meeting."""
    transcript = await session_manager.get_meeting_transcript(
        meeting_id=meeting_id,
        include_important_tags=include_important,
    )
    return {"meeting_id": meeting_id, "transcript": transcript}


@router.get("/meetings/{meeting_id}/summaries")
async def get_meeting_summaries(
    meeting_id: str,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Get all summaries for a meeting."""
    summaries = await session_manager._repo.get_summaries(meeting_id)
    return {
        "meeting_id": meeting_id,
        "summaries": [
            SummaryResponse(
                id=s.id,
                meeting_id=s.meeting_id,
                content=s.content,
                backend=s.backend,
                model=s.model,
                created_at=to_utc_iso(s.created_at),
                prompt_tokens=s.prompt_tokens,
                completion_tokens=s.completion_tokens,
            )
            for s in summaries
        ],
    }


# Recording list response model
class RecordingResponse(BaseModel):
    id: str
    title: Optional[str]
    formatted_title: str
    timezone_name: Optional[str]
    timezone_offset_minutes: Optional[int]
    recorded_date_label: Optional[str]
    recorded_time_label: Optional[str]
    recorded_timezone_label: Optional[str]
    started_at: str
    ended_at: Optional[str]
    duration_seconds: int
    segment_count: int
    has_transcription: bool
    has_summary: bool
    has_draft: bool = False
    needs_speaker_review: bool = False
    has_audio: bool

    class Config:
        from_attributes = True


class UpdateRecordingSettingsRequest(BaseModel):
    title: Optional[str] = None
    template_key: Optional[str] = None
    custom_prompt: Optional[str] = None
    attendees: Optional[str] = None


class UpdateSpeakerAssignmentsRequest(BaseModel):
    assignments: dict[str, str]


@router.get("/recordings", response_model=List[RecordingResponse])
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    repository: Repository = Depends(get_repository),
):
    """List past recording sessions."""
    sessions = await repository.get_sessions_list(limit=limit, offset=offset)
    for recording in sessions:
        started_at = datetime.fromisoformat(recording["started_at"].replace("Z", "+00:00"))
        timezone_name = recording.get("timezone_name")
        timezone_offset_minutes = recording.get("timezone_offset_minutes")
        recording["formatted_title"] = _build_formatted_title(
            started_at,
            recording.get("title"),
            timezone_name=timezone_name,
            timezone_offset_minutes=timezone_offset_minutes,
        )
        if timezone_name is not None or timezone_offset_minutes is not None:
            date_label, time_label, tz_label = _build_recorded_labels(
                started_at,
                timezone_name=timezone_name,
                timezone_offset_minutes=timezone_offset_minutes,
            )
            recording["recorded_date_label"] = date_label
            recording["recorded_time_label"] = time_label
            recording["recorded_timezone_label"] = tz_label
        else:
            recording["recorded_date_label"] = None
            recording["recorded_time_label"] = None
            recording["recorded_timezone_label"] = None
        recording["has_audio"] = get_session_audio_path(recording["id"]) is not None
        if not recording["has_audio"] and recording.get("ended_at"):
            recording["has_audio"] = ensure_session_audio_path(recording["id"]) is not None
    return sessions


@router.get("/recordings/{session_id}/workspace")
async def get_recording_workspace(
    session_id: str,
    repository: Repository = Depends(get_repository),
):
    """Return the unified recording workspace state for new and past recordings."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    meeting = await repository.get_primary_meeting(session_id, create_if_missing=True)
    if not meeting:
        raise HTTPException(status_code=400, detail="No meeting found for this recording")

    segments = await repository.get_segments(session_id=session_id)
    if session.has_transcription and segments:
        inferred_requires_review = _transcript_requires_speaker_review(segments)
        if inferred_requires_review and not meeting.speaker_review_required:
            meeting = await repository.update_meeting_settings(
                meeting.id,
                speaker_review_required=True,
            )
    saved_summaries = await repository.get_summaries(meeting.id, status="saved")
    draft_summary = await repository.get_draft_summary(meeting.id)
    latest_saved_summary = saved_summaries[0] if saved_summaries else None

    duration_seconds = 0
    if segments:
        duration_seconds = int(max(segment.end_time for segment in segments))
    elif session.ended_at:
        elapsed = (session.ended_at - session.started_at).total_seconds()
        duration_seconds = max(0, int(elapsed))

    if session.timezone_name is not None or session.timezone_offset_minutes is not None:
        date_label, time_label, tz_label = _build_recorded_labels(
            session.started_at,
            session.timezone_name,
            session.timezone_offset_minutes,
        )
    else:
        date_label, time_label, tz_label = None, None, None

    audio_path = get_session_audio_path(session.id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session.id)

    settings = get_settings()
    vault_name = (
        os.path.basename(settings.obsidian_vault_path.rstrip("/"))
        if settings.obsidian_vault_path
        else ""
    )
    open_in_obsidian_uri = None
    if latest_saved_summary and latest_saved_summary.obsidian_relative_path and vault_name:
        open_in_obsidian_uri = (
            f"obsidian://open?"
            f"vault={urllib.parse.quote(vault_name)}&"
            f"file={urllib.parse.quote(latest_saved_summary.obsidian_relative_path)}"
        )
    elif latest_saved_summary:
        search_query = _build_formatted_title(
            session.started_at,
            meeting.title,
            timezone_name=session.timezone_name,
            timezone_offset_minutes=session.timezone_offset_minutes,
        )
        exported_note_filename = _find_exported_note_filename(
            settings.obsidian_vault_path,
            session.started_at,
            meeting.title,
            session.timezone_name,
            session.timezone_offset_minutes,
        )
        if vault_name and exported_note_filename:
            open_in_obsidian_uri = (
                f"obsidian://open?"
                f"vault={urllib.parse.quote(vault_name)}&"
                f"file={urllib.parse.quote(exported_note_filename)}"
            )
        elif vault_name:
            open_in_obsidian_uri = (
                f"obsidian://search?"
                f"vault={urllib.parse.quote(vault_name)}&"
                f"query={urllib.parse.quote(search_query)}"
            )

    speaker_cards = _build_speaker_cards(segments, session_id) if session.has_transcription else []

    return {
        "recording": {
            "id": session.id,
            "meeting_id": meeting.id,
            "title": meeting.title,
            "formatted_title": _build_formatted_title(
                session.started_at,
                meeting.title,
                timezone_name=session.timezone_name,
                timezone_offset_minutes=session.timezone_offset_minutes,
            ),
            "timezone_name": session.timezone_name,
            "timezone_offset_minutes": session.timezone_offset_minutes,
            "recorded_date_label": date_label,
            "recorded_time_label": time_label,
            "recorded_timezone_label": tz_label,
            "started_at": to_utc_iso(session.started_at),
            "ended_at": to_utc_iso(session.ended_at),
            "duration_seconds": duration_seconds,
            "segment_count": len(segments),
            "has_transcription": bool(session.has_transcription),
            "has_audio": audio_path is not None,
            "audio_url": f"/api/recordings/{session.id}/audio" if audio_path else None,
            "audio_download_url": (
                f"/api/recordings/{session.id}/audio?download=true" if audio_path else None
            ),
        },
        "settings": {
            "title": meeting.title,
            "template_key": meeting.template_key or "meeting",
            "custom_prompt": meeting.custom_prompt,
            "attendees": meeting.attendees,
        },
        "speaker_review": {
            "required": bool(meeting.speaker_review_required),
            "completed": (not meeting.speaker_review_required)
            or meeting.speaker_review_completed_at is not None,
            "completed_at": to_utc_iso(meeting.speaker_review_completed_at),
            "speakers": speaker_cards,
        },
        "transcript": _serialize_transcript_segments(segments) if session.has_transcription else [],
        "draft_summary": _serialize_summary(draft_summary) if draft_summary else None,
        "saved_summaries": [_serialize_summary(summary) for summary in saved_summaries],
        "active_summary": _serialize_summary(draft_summary or latest_saved_summary)
        if (draft_summary or latest_saved_summary)
        else None,
        "obsidian": {
            "open_uri": open_in_obsidian_uri,
            "latest_relative_path": latest_saved_summary.obsidian_relative_path
            if latest_saved_summary
            else None,
        },
        "state": _build_recording_workspace_state(
            session,
            meeting,
            segments,
            saved_summaries,
            draft_summary,
            latest_saved_summary,
        ),
    }


@router.patch("/recordings/{session_id}/settings")
async def update_recording_settings(
    session_id: str,
    request: UpdateRecordingSettingsRequest,
    repository: Repository = Depends(get_repository),
):
    """Persist workspace settings inline without blocking processing."""
    meeting = await repository.get_primary_meeting(session_id, create_if_missing=True)
    if not meeting:
        raise HTTPException(status_code=404, detail="Recording not found")

    normalized_title = UNSET
    if request.title is not None:
        normalized_title = request.title.strip() or None
    normalized_template_key = request.template_key if request.template_key is not None else UNSET
    normalized_custom_prompt = UNSET
    if request.custom_prompt is not None:
        normalized_custom_prompt = request.custom_prompt.strip() or None
    normalized_attendees = UNSET
    if request.attendees is not None:
        normalized_attendees = request.attendees.strip() or None

    updated = await repository.update_meeting_settings(
        meeting.id,
        title=normalized_title,
        template_key=normalized_template_key,
        custom_prompt=normalized_custom_prompt,
        attendees=normalized_attendees,
    )
    return {
        "success": True,
        "meeting_id": updated.id,
        "title": updated.title,
        "template_key": updated.template_key,
        "custom_prompt": updated.custom_prompt,
        "attendees": updated.attendees,
    }


@router.get("/recordings/{session_id}/speakers")
async def get_recording_speakers(
    session_id: str,
    repository: Repository = Depends(get_repository),
):
    """Return speaker cards for the workspace speaker-review step."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    segments = await repository.get_segments(session_id=session_id)
    return {
        "audio_url": f"/api/recordings/{session_id}/audio",
        "speakers": _build_speaker_cards(segments, session_id),
    }


@router.put("/recordings/{session_id}/speakers")
async def update_recording_speakers(
    session_id: str,
    request: UpdateSpeakerAssignmentsRequest,
    repository: Repository = Depends(get_repository),
):
    """Save speaker assignments and mark summary freshness stale."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    meeting = await repository.get_primary_meeting(session_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    segments = await repository.get_segments(session_id=session_id)
    updates: dict[str, str | None] = {}
    for segment in segments:
        speaker_cluster = getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)
        if not speaker_cluster:
            continue
        mapped_name = request.assignments.get(speaker_cluster)
        if mapped_name and mapped_name.strip():
            updates[segment.id] = mapped_name.strip()

    if updates:
        await repository.update_segments_speakers(updates)

    await repository.update_meeting_settings(
        meeting.id,
        speaker_review_required=_transcript_requires_speaker_review(segments),
        speaker_review_completed_at=datetime.utcnow(),
    )
    return {"success": True, "updated": len(updates)}


class SaveSummaryRequest(BaseModel):
    content: str
    revision_instruction: Optional[str] = None
    meeting_id: Optional[str] = None


@router.post("/recordings/{session_id}/summaries")
async def save_recording_summary(
    session_id: str,
    request: SaveSummaryRequest,
    repository: Repository = Depends(get_repository),
):
    """Save a new summary for a recording session."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meetings = sorted(session.meetings, key=lambda m: m.key_start)
    if not meetings:
        raise HTTPException(status_code=400, detail="No meeting found for this session")

    # Use specified meeting or primary
    target_meeting_id = request.meeting_id or meetings[0].id
    primary_meeting = next((m for m in meetings if m.id == target_meeting_id), meetings[0])
    
    # Get original summary to copy metadata if possible
    existing_summaries = await repository.get_summaries(primary_meeting.id, status="saved")
    original_summary = existing_summaries[0] if existing_summaries else None
    
    # Save to DB (always committed; vault write is best-effort below)
    summary = await repository.add_summary(
        meeting_id=primary_meeting.id,
        content=request.content,
        backend="manual",
        model="user-refined",
        template=original_summary.template if original_summary else None,
        processing_duration_seconds=original_summary.processing_duration_seconds if original_summary else None,
        status="saved",
        source_type="manual_edit",
        template_key=primary_meeting.template_key,
        custom_prompt=primary_meeting.custom_prompt,
        attendees_snapshot=primary_meeting.attendees,
    )

    # Best-effort vault write
    settings = get_settings()
    if settings.obsidian_vault_path:
        # Determine filename with versioning
        version_suffix = ""
        if len(existing_summaries) >= 1:
            version_suffix = f" (v{len(existing_summaries) + 1})"
            
        local_started_at = localize_datetime(
            session.started_at,
            session.timezone_name,
            session.timezone_offset_minutes,
        )
        tz_label = timezone_label(session.timezone_name, session.timezone_offset_minutes)
        
        # Get title
        title = primary_meeting.title or "Untitled Recording"
        safe_title = re.sub(r'[<>:"/\\|?*]', '', title.strip())
        dow = local_started_at.strftime("%a")
        time_hhmm = local_started_at.strftime("%H%M")
        
        filename = f"{local_started_at.day:02d} {dow} {time_hhmm} - {safe_title}{version_suffix}.md"
        week_folder_name = week_folder(local_started_at)
        relative_path = f"Meetings/{week_folder_name}/{filename}"
        
        # Build metadata for markdown
        recorded_at = format_datetime_human(local_started_at, tz_label)
        local_exported_at = localize_datetime(
            datetime.now(timezone.utc),
            session.timezone_name,
            session.timezone_offset_minutes,
        )
        exported_at = format_datetime_human(local_exported_at, tz_label)
        
        # Get transcript
        segments = await repository.get_segments(session_id=session_id)
        from src.api.routes.export import _segments_to_transcript
        full_transcript, audio_duration_seconds = _segments_to_transcript(segments)
        
        duration_str = format_duration_human(int(audio_duration_seconds))
        processing_time_str = ""
        if original_summary and original_summary.processing_duration_seconds:
            processing_time_str = format_processing_time(original_summary.processing_duration_seconds)

        markdown_content = build_obsidian_markdown(
            content=request.content,
            template_label="Refined",
            recorded_at=recorded_at,
            exported_at=exported_at,
            duration_str=duration_str,
            processing_time_str=processing_time_str,
            transcript=full_transcript,
            revision_instruction=request.revision_instruction,
        )

        obsidian_uri = None
        try:
            from src.api.routes.export import _write_obsidian_file
            _, obsidian_uri = await _write_obsidian_file(
                markdown_content, relative_path, settings.obsidian_vault_path
            )
            await repository.update_summary(
                summary.id,
                saved_to_obsidian_at=datetime.utcnow(),
                obsidian_relative_path=relative_path,
            )
        except Exception as e:
            logger.warning("save_recording_summary: vault write failed (non-fatal): %s", e)

        return {"success": True, "summary_id": summary.id, "obsidian_uri": obsidian_uri}

    return {"success": True, "summary_id": summary.id}


class RenameRecordingRequest(BaseModel):
    title: str


@router.patch("/recordings/{session_id}/title")
async def rename_recording(
    session_id: str,
    request: RenameRecordingRequest,
    repository: Repository = Depends(get_repository),
):
    """Rename a recording."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    meetings = sorted(session.meetings, key=lambda m: m.key_start)
    if not meetings:
        raise HTTPException(status_code=400, detail="No meeting found")
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    await repository.update_meeting_title(meetings[0].id, title)
    return {"success": True, "title": title}


@router.delete("/recordings/{session_id}")
async def delete_recording(
    session_id: str,
    repository: Repository = Depends(get_repository),
):
    """Delete a recording and all its associated data."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    await repository.delete_session(session_id)
    audio_path = get_session_audio_path(session_id)
    if audio_path and audio_path.exists():
        audio_path.unlink()
    # Clear old sequential append state
    clear_session_chunk_upload_state(session_id)
    # Clear new chunk storage (all clients)
    cleanup_chunk_storage(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/recordings/{session_id}")
async def get_recording(
    session_id: str,
    repository: Repository = Depends(get_repository),
):
    """Get details for a specific recording."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    # Get segments for this session
    segments = await repository.get_segments(session_id=session_id)

    # Get meetings and their summaries
    meetings = session.meetings if hasattr(session, 'meetings') else []

    # Calculate duration
    duration_seconds = 0
    if segments:
        duration_seconds = int(max(s.end_time for s in segments))
    elif session.ended_at:
        elapsed = (session.ended_at - session.started_at).total_seconds()
        duration_seconds = max(0, int(elapsed))

    # Build transcript only after authoritative transcription has been run.
    transcript_lines = []
    if session.has_transcription:
        for segment in segments:
            mins = int(segment.start_time // 60)
            secs = int(segment.start_time % 60)
            timestamp = f"[{mins:02d}:{secs:02d}]"
            transcript_lines.append({
                "timestamp": timestamp,
                "text": segment.text,
                "speaker": getattr(segment, "speaker", None),
                "is_important": segment.is_important,
                "start_time": segment.start_time,
            })

    title = None
    if meetings:
        ordered_meetings = sorted(meetings, key=lambda m: m.key_start)
        for meeting in ordered_meetings:
            if meeting.title:
                title = meeting.title
                break

    audio_path = get_session_audio_path(session.id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session.id)

    has_summary = False
    latest_summary = None
    summary_meeting_title = None
    summary_meeting_id = None
    for meeting in meetings:
        summaries = await repository.get_summaries(meeting.id)
        if summaries:
            has_summary = True
            # Get the most recent summary for this meeting
            current_latest = sorted(summaries, key=lambda s: s.created_at, reverse=True)[0]
            if not latest_summary or current_latest.created_at > latest_summary.created_at:
                latest_summary = current_latest
                summary_meeting_title = meeting.title
                summary_meeting_id = meeting.id

    all_summaries_list = []
    if summary_meeting_id:
        raw = await repository.get_summaries(summary_meeting_id)
        all_summaries_list = sorted(raw, key=lambda s: s.created_at)

    settings = get_settings()
    if not has_summary:
        # Check vault for existing note if DB has no summary (legacy migration)
        # Note: this might be why summaries from different meetings show up if titles are generic.
        has_summary = _has_exported_note_in_vault(
            settings.obsidian_vault_path,
            session.started_at,
            title,
            session.timezone_name,
            session.timezone_offset_minutes,
        )

    if session.timezone_name is not None or session.timezone_offset_minutes is not None:
        date_label, time_label, tz_label = _build_recorded_labels(
            session.started_at,
            session.timezone_name,
            session.timezone_offset_minutes,
        )
    else:
        date_label, time_label, tz_label = None, None, None

    vault_name = os.path.basename(settings.obsidian_vault_path.rstrip("/")) if settings.obsidian_vault_path else ""
    search_query = _build_formatted_title(
        session.started_at,
        title,
        timezone_name=session.timezone_name,
        timezone_offset_minutes=session.timezone_offset_minutes,
    )
    exported_note_filename = _find_exported_note_filename(
        settings.obsidian_vault_path,
        session.started_at,
        title,
        session.timezone_name,
        session.timezone_offset_minutes,
    )
    open_in_obsidian_uri = None
    if has_summary and vault_name:
        if exported_note_filename:
            open_in_obsidian_uri = (
                f"obsidian://open?"
                f"vault={urllib.parse.quote(vault_name)}&"
                f"file={urllib.parse.quote(exported_note_filename)}"
            )
        else:
            open_in_obsidian_uri = (
                f"obsidian://search?"
                f"vault={urllib.parse.quote(vault_name)}&"
                f"query={urllib.parse.quote(search_query)}"
            )

    return {
        "id": session.id,
        "title": title,
        "formatted_title": _build_formatted_title(
            session.started_at,
            title,
            timezone_name=session.timezone_name,
            timezone_offset_minutes=session.timezone_offset_minutes,
        ),
        "timezone_name": session.timezone_name,
        "timezone_offset_minutes": session.timezone_offset_minutes,
        "recorded_date_label": date_label,
        "recorded_time_label": time_label,
        "recorded_timezone_label": tz_label,
        "started_at": to_utc_iso(session.started_at),
        "ended_at": to_utc_iso(session.ended_at),
        "duration_seconds": duration_seconds,
        "segment_count": len(segments),
        "has_transcription": bool(session.has_transcription),
        "has_summary": has_summary,
        "summary": latest_summary.content if latest_summary else None,
        "summary_meeting_id": summary_meeting_id,
        "summary_meeting_title": summary_meeting_title,
        "summary_created_at": to_utc_iso(latest_summary.created_at) if latest_summary else None,
        "summary_processing_duration": latest_summary.processing_duration_seconds if latest_summary else None,
        "open_in_obsidian_uri": open_in_obsidian_uri,
        "has_audio": audio_path is not None,
        "audio_url": f"/api/recordings/{session.id}/audio" if audio_path else None,
        "audio_download_url": (
            f"/api/recordings/{session.id}/audio?download=true" if audio_path else None
        ),
        "transcript": transcript_lines,
        "meetings": [
            {
                "id": m.id,
                "title": m.title,
                "key_start": to_utc_iso(m.key_start),
                "key_stop": to_utc_iso(m.key_stop),
            }
            for m in meetings
        ],
        "all_summaries": [
            {
                "id": str(s.id),
                "content": s.content,
                "backend": s.backend,
                "model": s.model,
                "created_at": to_utc_iso(s.created_at),
                "processing_duration_seconds": s.processing_duration_seconds,
                "template": s.template,
            }
            for s in all_summaries_list
        ],
    }


@router.put("/recordings/{session_id}/audio")
async def upload_recording_audio(
    session_id: str,
    request: Request,
    repository: Repository = Depends(get_repository),
):
    """Upload encoded audio for a completed recording session.

    This is the authoritative, guaranteed upload path. It clears ALL chunk
    storage for the session (all clients) and overwrites any partial data.
    """
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Audio payload is empty")

    extension = extension_from_content_type(request.headers.get("content-type", ""))

    audio_dir = get_audio_dir()
    # Clear old sequential append state
    clear_session_chunk_upload_state(session_id)
    # Clear new chunk storage (all clients)
    cleanup_chunk_storage(session_id)
    # Remove any existing finalized audio
    for existing in get_session_audio_candidates(session_id):
        existing.unlink()

    audio_path = audio_dir / f"{session_id}.{extension}"
    audio_path.write_bytes(body)

    return {
        "status": "uploaded",
        "session_id": session_id,
        "bytes": len(body),
        "audio_url": f"/api/recordings/{session_id}/audio",
    }


@router.put("/recordings/{session_id}/audio/chunks/{chunk_index}")
async def upload_recording_audio_chunk(
    session_id: str,
    chunk_index: int,
    request: Request,
    repository: Repository = Depends(get_repository),
):
    """Store one encoded audio chunk for a recording session.

    Chunks are stored individually by index, namespaced by client ID.
    This is idempotent and order-independent - chunks can arrive in any order.

    Requires X-Client-ID header to isolate uploads from different devices.
    """
    client_id = request.headers.get("X-Client-ID")
    if not client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header required")

    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="chunk_index must be non-negative")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Audio chunk payload is empty")

    # Store chunk (idempotent - skips if same size already exists)
    chunk_path = write_chunk(session_id, client_id, chunk_index, body)

    return {
        "status": "stored",
        "session_id": session_id,
        "chunk_index": chunk_index,
        "bytes": len(body),
        "path": str(chunk_path.name),
    }


@router.post("/recordings/{session_id}/audio/finalize")
async def finalize_recording_audio(
    session_id: str,
    body: FinalizeAudioRequest,
    request: Request,
    repository: Repository = Depends(get_repository),
):
    """Finalize chunked recording audio into stable file for export/playback.

    Requires X-Client-ID header - only assembles chunks from that specific client.
    Returns 409 with missing chunk indices if incomplete.
    """
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    # Check for already finalized audio
    existing_audio_path = get_session_audio_path(session_id)
    if existing_audio_path:
        return {
            "status": "already_finalized",
            "session_id": session_id,
            "audio_url": f"/api/recordings/{session_id}/audio",
        }

    client_id = request.headers.get("X-Client-ID")
    if not client_id:
        raise HTTPException(status_code=400, detail="X-Client-ID header required")

    # Determine expected chunk count
    expected_count = body.expected_chunks or body.uploaded_chunks
    if expected_count is None or expected_count <= 0:
        raise HTTPException(status_code=400, detail="expected_chunks or uploaded_chunks required")

    # Check for missing chunks
    missing = get_missing_chunk_indices(session_id, client_id, expected_count)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Incomplete chunks: missing {len(missing)} of {expected_count}",
            headers={"X-Missing-Chunks": ",".join(str(i) for i in missing[:20])},
        )

    # Determine extension
    extension = extension_from_content_type(body.mime_type or "")

    # Assemble chunks from this client
    final_path = assemble_chunks(session_id, client_id, expected_count, extension)
    if not final_path:
        raise HTTPException(status_code=400, detail="Failed to assemble chunks")

    return {
        "status": "finalized",
        "session_id": session_id,
        "chunks": expected_count,
        "bytes": final_path.stat().st_size,
        "audio_url": f"/api/recordings/{session_id}/audio",
    }


@router.get("/recordings/{session_id}/audio")
async def get_recording_audio(
    session_id: str,
    download: bool = False,
    repository: Repository = Depends(get_repository),
):
    """Stream or download stored audio for a recording session."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    title = None
    meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
    for meeting in meetings:
        if meeting.title:
            title = meeting.title
            break

    stem = _build_formatted_title(
        session.started_at,
        title,
        timezone_name=session.timezone_name,
        timezone_offset_minutes=session.timezone_offset_minutes,
    )
    safe_name = _sanitize_title_for_filename(stem)
    filename = f"{safe_name}{audio_path.suffix.lower()}"
    media_type = media_type_for_path(audio_path)

    if download:
        return FileResponse(
            path=audio_path,
            media_type=media_type,
            filename=filename,
        )

    return FileResponse(path=audio_path, media_type=media_type)


class SpeakerClip(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    text: str


class SpeakerClipsResponse(BaseModel):
    clips: list[SpeakerClip]
    audio_url: str


@router.get("/recordings/{session_id}/speaker-clips", response_model=SpeakerClipsResponse)
async def get_speaker_clips(
    session_id: str,
    repository: Repository = Depends(get_repository),
):
    """Get audio clips for each unique speaker in the recording."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=404, detail="No transcript segments found")

    # Group segments by speaker
    speaker_segments: dict[str, list] = {}
    for seg in segments:
        spk = getattr(seg, "speaker", None)
        if spk:
            if spk not in speaker_segments:
                speaker_segments[spk] = []
            speaker_segments[spk].append(seg)

    if not speaker_segments:
        raise HTTPException(status_code=400, detail="No speakers found in transcript. Run diarization first.")

    # Build clips - first utterance from each speaker
    clips = []
    for speaker, segs in sorted(speaker_segments.items()):
        # Sort by start time and get first segment
        sorted_segs = sorted(segs, key=lambda s: s.start_time)
        first_seg = sorted_segs[0]
        
        # Use 5 seconds of audio starting from segment start
        clip_duration = min(5.0, float(first_seg.end_time) - float(first_seg.start_time))
        if clip_duration < 0.5:
            clip_duration = min(5.0, float(first_seg.end_time))
        
        clip = SpeakerClip(
            speaker=speaker,
            start_time=float(first_seg.start_time),
            end_time=float(first_seg.start_time) + clip_duration,
            text=first_seg.text[:100],
        )
        clips.append(clip)

    return SpeakerClipsResponse(
        clips=clips,
        audio_url=f"/api/recordings/{session_id}/audio",
    )


class SpeakerMappingRequest(BaseModel):
    mapping: dict[str, str]


@router.post("/recordings/{session_id}/speaker-mapping")
async def set_speaker_mapping(
    session_id: str,
    request: SpeakerMappingRequest,
    repository: Repository = Depends(get_repository),
):
    """Update speaker labels for all segments based on user-provided mapping."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=404, detail="No transcript segments found")

    meeting = await repository.get_primary_meeting(session_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Update each segment's speaker
    updates = {}
    for seg in segments:
        speaker_key = getattr(seg, "speaker_cluster", None) or getattr(seg, "speaker", None)
        if speaker_key and speaker_key in request.mapping:
            new_name = request.mapping[speaker_key]
            updates[seg.id] = new_name

    if updates:
        await repository.update_segments_speakers(updates)
        await repository.update_meeting_settings(
            meeting.id,
            speaker_review_completed_at=datetime.utcnow(),
        )

    logger.info("speaker_mapping: updated %d segments for session %s", len(updates), session_id)

    return {"success": True, "updated": len(updates), "mapping": request.mapping}
