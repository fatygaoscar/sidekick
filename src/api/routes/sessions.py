"""Session and meeting REST endpoints."""

import os
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.audio.storage import (
    clear_session_chunk_upload_state,
    extension_from_content_type,
    get_audio_dir,
    get_session_audio_candidates,
    get_session_chunk_meta_path,
    get_session_audio_path,
    get_session_partial_audio_path,
    media_type_for_path,
    read_session_chunk_meta,
    write_session_chunk_meta,
)
from src.sessions.manager import SessionManager
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager


router = APIRouter()


def _sanitize_title_for_filename(title: str) -> str:
    """Sanitize title for safe filesystem download names."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    return sanitized or "Untitled Recording"


def _build_formatted_title(started_at: datetime, title: str | None) -> str:
    """Build the canonical recording title shown in history."""
    timestamp = started_at.strftime("%Y-%m-%d-%H%M")
    base_title = (title or "Untitled Recording").strip() or "Untitled Recording"
    return f"{timestamp} - {base_title}"


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


class SessionResponse(BaseModel):
    id: str
    mode: str
    submode: Optional[str]
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
    )
    return SessionResponse(
        id=session.id,
        mode=session.mode,
        submode=session.submode,
        is_active=session.is_active,
        started_at=session.started_at.isoformat(),
        ended_at=session.ended_at.isoformat() if session.ended_at else None,
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
        is_active=session.is_active,
        started_at=session.started_at.isoformat(),
        ended_at=session.ended_at.isoformat() if session.ended_at else None,
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
        key_start=meeting.key_start.isoformat(),
        key_stop=meeting.key_stop.isoformat() if meeting.key_stop else None,
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
        key_start=meeting.key_start.isoformat(),
        key_stop=meeting.key_stop.isoformat() if meeting.key_stop else None,
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
        key_start=meeting.key_start.isoformat(),
        key_stop=meeting.key_stop.isoformat() if meeting.key_stop else None,
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
        marked_at=marker.marked_at.isoformat(),
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
        created_at=summary.created_at.isoformat(),
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
                created_at=s.created_at.isoformat(),
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
    started_at: str
    ended_at: Optional[str]
    duration_seconds: int
    segment_count: int
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
        started_at = datetime.fromisoformat(recording["started_at"])
        recording["formatted_title"] = _build_formatted_title(started_at, recording.get("title"))
        recording["has_audio"] = get_session_audio_path(recording["id"]) is not None
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
    clear_session_chunk_upload_state(session_id)
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

    # Build transcript
    transcript_lines = []
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

    return {
        "id": session.id,
        "title": title,
        "formatted_title": _build_formatted_title(session.started_at, title),
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": duration_seconds,
        "segment_count": len(segments),
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
                "key_start": m.key_start.isoformat(),
                "key_stop": m.key_stop.isoformat() if m.key_stop else None,
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
    """Upload encoded audio for a completed recording session."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Audio payload is empty")

    extension = extension_from_content_type(request.headers.get("content-type", ""))

    audio_dir = get_audio_dir()
    clear_session_chunk_upload_state(session_id)
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
    """Append one encoded audio chunk for a recording session."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="chunk_index must be non-negative")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Audio chunk payload is empty")

    incoming_extension = extension_from_content_type(request.headers.get("content-type", ""))
    meta = read_session_chunk_meta(session_id) or {
        "next_chunk_index": 0,
        "extension": incoming_extension,
    }

    expected_chunk_index = int(meta.get("next_chunk_index", 0))
    if chunk_index < expected_chunk_index:
        return {
            "status": "duplicate",
            "session_id": session_id,
            "chunk_index": chunk_index,
            "expected_chunk_index": expected_chunk_index,
        }
    if chunk_index > expected_chunk_index:
        raise HTTPException(
            status_code=409,
            detail="Chunk index out of order",
            headers={"X-Expected-Chunk-Index": str(expected_chunk_index)},
        )

    extension = str(meta.get("extension") or incoming_extension or "webm")
    part_path = get_session_partial_audio_path(session_id)
    with part_path.open("ab") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())

    meta["next_chunk_index"] = expected_chunk_index + 1
    meta["extension"] = extension
    write_session_chunk_meta(session_id, meta)

    return {
        "status": "appended",
        "session_id": session_id,
        "chunk_index": chunk_index,
        "next_chunk_index": meta["next_chunk_index"],
        "bytes": len(body),
    }


@router.post("/recordings/{session_id}/audio/finalize")
async def finalize_recording_audio(
    session_id: str,
    request: FinalizeAudioRequest,
    repository: Repository = Depends(get_repository),
):
    """Finalize chunked recording audio into stable file for export/playback."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    meta = read_session_chunk_meta(session_id)
    if not meta:
        audio_path = get_session_audio_path(session_id)
        if audio_path:
            return {
                "status": "already_finalized",
                "session_id": session_id,
                "audio_url": f"/api/recordings/{session_id}/audio",
            }
        raise HTTPException(status_code=400, detail="No chunked upload state found")

    expected_chunks = int(meta.get("next_chunk_index", 0))
    client_chunks = request.uploaded_chunks
    if client_chunks is not None and client_chunks > expected_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"Missing uploaded chunks (server={expected_chunks}, client={client_chunks})",
        )

    part_path = get_session_partial_audio_path(session_id)
    if not part_path.exists() or part_path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="Chunked audio file is empty")

    for existing in get_session_audio_candidates(session_id):
        existing.unlink()

    extension = str(
        meta.get("extension")
        or extension_from_content_type(request.mime_type or "")
        or "webm"
    )
    final_path = get_audio_dir() / f"{session_id}.{extension}"
    part_path.replace(final_path)

    chunk_meta_path = get_session_chunk_meta_path(session_id)
    if chunk_meta_path.exists():
        chunk_meta_path.unlink()

    return {
        "status": "finalized",
        "session_id": session_id,
        "chunks": expected_chunks,
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
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    title = None
    meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
    for meeting in meetings:
        if meeting.title:
            title = meeting.title
            break

    stem = _build_formatted_title(session.started_at, title)
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
