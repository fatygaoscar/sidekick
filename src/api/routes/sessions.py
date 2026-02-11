"""Session and meeting REST endpoints."""

import os
import re
import urllib.parse
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.settings import get_settings
from src.core.datetime_utils import localize_datetime, timezone_label, to_utc_iso
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
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager


router = APIRouter()


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
    has_audio: bool

    class Config:
        from_attributes = True


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
    for meeting in meetings:
        summaries = await repository.get_summaries(meeting.id)
        if summaries:
            has_summary = True
            break

    settings = get_settings()
    if not has_summary:
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
