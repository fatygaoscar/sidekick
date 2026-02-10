"""Export endpoints for Obsidian integration."""

from __future__ import annotations

import asyncio
import re
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config.settings import get_settings
from src.audio.storage import get_session_audio_path
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager
from src.summarization.prompts import TEMPLATE_INFO
from src.transcription.manager import TranscriptionManager


router = APIRouter()


def get_summarization_manager(request: Request) -> SummarizationManager:
    """Dependency to get summarization manager."""
    return request.app.state.summarization_manager


def get_repository(request: Request) -> Repository:
    """Dependency to get repository."""
    return request.app.state.repository


def get_transcription_manager(request: Request) -> TranscriptionManager:
    """Dependency to get transcription manager."""
    return request.app.state.transcription_manager


class ExportRequest(BaseModel):
    title: str
    template: str = "meeting"
    custom_prompt: Optional[str] = None


class ExportResponse(BaseModel):
    success: bool
    filename: str
    filepath: str
    obsidian_uri: Optional[str] = None
    summary_preview: str


class ExportJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class ExportJobStatus(BaseModel):
    job_id: str
    session_id: str
    status: str
    stage: str
    message: str
    transcription_progress: float
    summarization_progress: float
    overall_progress: float
    result: Optional[ExportResponse] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


ProgressCallback = Callable[[str, str, Optional[float], Optional[float]], Awaitable[None] | None]

_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_TASKS: dict[str, asyncio.Task] = {}


@router.get("/templates")
async def get_templates():
    """Get available summary templates."""
    return {"templates": TEMPLATE_INFO}


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _compute_overall_progress(stage: str, transcription_progress: float, summarization_progress: float) -> float:
    # 65% transcription, 30% summarization, 5% final write.
    if stage == "queued":
        return 0.0
    if stage == "transcribing":
        return min(0.65 * transcription_progress, 0.65)
    if stage == "summarizing":
        return 0.65 + min(0.30 * summarization_progress, 0.30)
    if stage == "writing":
        return 0.95
    if stage == "completed":
        return 1.0
    return min(0.65 * transcription_progress + 0.30 * summarization_progress, 0.95)


def _create_export_job(session_id: str) -> dict:
    job_id = str(uuid.uuid4())
    now = _utc_now_iso()
    payload = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "queued",
        "stage": "queued",
        "message": "Queued",
        "transcription_progress": 0.0,
        "summarization_progress": 0.0,
        "overall_progress": 0.0,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    _EXPORT_JOBS[job_id] = payload
    return payload


def _update_export_job(job_id: str, **fields) -> None:
    job = _EXPORT_JOBS.get(job_id)
    if not job:
        return

    job.update(fields)
    stage = str(job.get("stage", "queued"))
    transcription_progress = float(job.get("transcription_progress", 0.0))
    summarization_progress = float(job.get("summarization_progress", 0.0))
    job["overall_progress"] = _compute_overall_progress(
        stage,
        transcription_progress,
        summarization_progress,
    )
    job["updated_at"] = _utc_now_iso()


async def _emit_progress(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    transcription_progress: Optional[float] = None,
    summarization_progress: Optional[float] = None,
) -> None:
    if not callback:
        return

    maybe_awaitable = callback(stage, message, transcription_progress, summarization_progress)
    if asyncio.iscoroutine(maybe_awaitable):
        await maybe_awaitable


async def _run_export_pipeline(
    session_id: str,
    request_payload: ExportRequest,
    summarization_manager: SummarizationManager,
    repository: Repository,
    transcription_manager: TranscriptionManager,
    progress_callback: Optional[ProgressCallback] = None,
) -> ExportResponse:
    """Export a recording to Obsidian vault.

    Authoritative export pipeline:
      audio file -> transcription -> transcript segments -> summary -> markdown
    """
    settings = get_settings()

    await _emit_progress(progress_callback, "transcribing", "Preparing recording", 0.03, 0.0)

    # Get session and its transcript
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    audio_path = get_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=400, detail="No recording audio available for this session")

    # Persist the user-provided title for history/recordings pages.
    meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
    primary_meeting_id = None
    if meetings:
        primary_meeting = meetings[0]
        primary_meeting_id = primary_meeting.id
        if primary_meeting.title != request_payload.title:
            await repository.update_meeting_title(primary_meeting.id, request_payload.title)
    else:
        meeting = await repository.create_meeting(session_id=session.id, title=request_payload.title)
        primary_meeting_id = meeting.id

    # Export transcription pipeline is source of truth:
    # transcribe full recording audio, then replace transcript segments for session.
    await _emit_progress(progress_callback, "transcribing", "Transcribing audio", 0.08, 0.0)
    try:
        transcription_result, audio_duration_seconds = await transcription_manager.transcribe_file(audio_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to transcribe recording audio: {str(e)}")

    full_text = transcription_result.text.strip()
    if not full_text:
        raise HTTPException(status_code=400, detail="No speech detected in recording audio")

    await repository.delete_segments_for_session(session_id)

    if transcription_result.words:
        # Create readable transcript segments from word timestamps.
        buffer_words: list[dict] = []
        max_words_per_segment = 24
        max_segment_duration = 14.0

        def flush_words(words: list[dict]) -> tuple[str, float, float] | None:
            if not words:
                return None
            text = " ".join(str(w.get("word", "")).strip() for w in words).strip()
            if not text:
                return None
            start = float(words[0].get("start", 0.0))
            end = float(words[-1].get("end", start))
            return text, start, end

        for word in transcription_result.words:
            token = str(word.get("word", "")).strip()
            if not token:
                continue

            if not buffer_words:
                buffer_words.append(word)
                continue

            first_start = float(buffer_words[0].get("start", 0.0))
            segment_elapsed = float(word.get("end", first_start)) - first_start
            hit_limit = len(buffer_words) >= max_words_per_segment or segment_elapsed >= max_segment_duration
            sentence_end = token.endswith((".", "!", "?"))

            buffer_words.append(word)
            if hit_limit or sentence_end:
                parsed = flush_words(buffer_words)
                if parsed:
                    text, start, end = parsed
                    await repository.add_segment(
                        session_id=session_id,
                        meeting_id=primary_meeting_id,
                        text=text,
                        start_time=start,
                        end_time=end,
                        confidence=transcription_result.confidence,
                    )
                buffer_words = []

        parsed = flush_words(buffer_words)
        if parsed:
            text, start, end = parsed
            await repository.add_segment(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                text=text,
                start_time=start,
                end_time=end,
                confidence=transcription_result.confidence,
            )
    else:
        await repository.add_segment(
            session_id=session_id,
            meeting_id=primary_meeting_id,
            text=full_text,
            start_time=0.0,
            end_time=audio_duration_seconds,
            confidence=transcription_result.confidence,
        )

    await _emit_progress(progress_callback, "transcribing", "Transcript complete", 1.0, 0.0)

    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=500, detail="Failed to build transcript segments from recording audio")

    # Build transcript with timestamps
    transcript_lines = []
    for segment in segments:
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        timestamp = f"[{mins:02d}:{secs:02d}]"
        marker = " [IMPORTANT]" if segment.is_important else ""
        transcript_lines.append(f"{timestamp}{marker} {segment.text}")

    full_transcript = "\n".join(transcript_lines).strip()

    # Generate summary using the selected template
    template = request_payload.template
    custom_instructions = request_payload.custom_prompt if template == "custom" else None

    await _emit_progress(progress_callback, "summarizing", "Generating summary", 1.0, 0.08)
    try:
        summary_result = await summarization_manager.summarize(
            transcript=full_transcript,
            prompt_type=template,
            custom_instructions=custom_instructions,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {str(e)}")

    await _emit_progress(progress_callback, "summarizing", "Summary complete", 1.0, 1.0)

    # Calculate duration
    duration_seconds = int(audio_duration_seconds)
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    # Build filename: YYYY-MM-DD-HHMM - [title] [Template].md
    timestamp = session.started_at.strftime("%Y-%m-%d-%H%M")
    template_label = TEMPLATE_INFO.get(template, {}).get("name", template.title())
    safe_title = re.sub(r'[<>:"/\\|?*]', '', request_payload.title)  # Remove invalid filename chars
    filename = f"{timestamp} - {safe_title} [{template_label}].md"

    # Build markdown content
    recorded_at = session.started_at.strftime("%Y-%m-%d %H:%M")
    markdown_content = f"""**Template**: {template_label}
**Recorded**: {recorded_at}
**Duration**: {duration_str}

---

{summary_result.content}

---

<details>
<summary>Full Transcript</summary>

{full_transcript}

</details>
"""

    # Write to Obsidian vault
    await _emit_progress(progress_callback, "writing", "Writing note to vault", 1.0, 1.0)
    vault_path = Path(settings.obsidian_vault_path)
    if not vault_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Obsidian vault path does not exist: {settings.obsidian_vault_path}",
        )

    filepath = vault_path / filename
    try:
        filepath.write_text(markdown_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")

    # Build Obsidian URI
    vault_name = vault_path.name
    file_without_ext = filename.rsplit('.', 1)[0]
    obsidian_uri = (
        f"obsidian://open?"
        f"vault={urllib.parse.quote(vault_name)}&"
        f"file={urllib.parse.quote(file_without_ext)}"
    )

    # Summary preview (first 200 chars)
    preview = summary_result.content[:200] + "..." if len(summary_result.content) > 200 else summary_result.content

    return ExportResponse(
        success=True,
        filename=filename,
        filepath=str(filepath),
        obsidian_uri=obsidian_uri,
        summary_preview=preview,
    )


async def _run_export_job(
    job_id: str,
    session_id: str,
    request_payload: ExportRequest,
    summarization_manager: SummarizationManager,
    repository: Repository,
    transcription_manager: TranscriptionManager,
) -> None:
    def update_progress(
        stage: str,
        message: str,
        transcription_progress: Optional[float],
        summarization_progress: Optional[float],
    ) -> None:
        updates = {
            "status": "running",
            "stage": stage,
            "message": message,
        }
        if transcription_progress is not None:
            updates["transcription_progress"] = max(0.0, min(1.0, transcription_progress))
        if summarization_progress is not None:
            updates["summarization_progress"] = max(0.0, min(1.0, summarization_progress))
        _update_export_job(job_id, **updates)

    try:
        result = await _run_export_pipeline(
            session_id=session_id,
            request_payload=request_payload,
            summarization_manager=summarization_manager,
            repository=repository,
            transcription_manager=transcription_manager,
            progress_callback=update_progress,
        )
        _update_export_job(
            job_id,
            status="completed",
            stage="completed",
            message="Export complete",
            transcription_progress=1.0,
            summarization_progress=1.0,
            result=result.model_dump(),
            error=None,
        )
    except HTTPException as exc:
        _update_export_job(
            job_id,
            status="failed",
            stage="failed",
            message="Export failed",
            error=str(exc.detail),
        )
    except Exception as exc:
        _update_export_job(
            job_id,
            status="failed",
            stage="failed",
            message="Export failed",
            error=str(exc),
        )


@router.post("/recordings/{session_id}/export-obsidian", response_model=ExportResponse)
async def export_to_obsidian(
    session_id: str,
    request: ExportRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
    repository: Repository = Depends(get_repository),
    transcription_manager: TranscriptionManager = Depends(get_transcription_manager),
):
    """Synchronous export endpoint (legacy + recordings page)."""
    return await _run_export_pipeline(
        session_id=session_id,
        request_payload=request,
        summarization_manager=summarization_manager,
        repository=repository,
        transcription_manager=transcription_manager,
    )


@router.post("/recordings/{session_id}/export-obsidian-job", response_model=ExportJobCreateResponse)
async def export_to_obsidian_job(
    session_id: str,
    request: ExportRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
    repository: Repository = Depends(get_repository),
    transcription_manager: TranscriptionManager = Depends(get_transcription_manager),
):
    """Start asynchronous export job and return job id for polling."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    job = _create_export_job(session_id)
    job_id = str(job["job_id"])
    _update_export_job(job_id, message="Starting export")

    task = asyncio.create_task(
        _run_export_job(
            job_id=job_id,
            session_id=session_id,
            request_payload=request,
            summarization_manager=summarization_manager,
            repository=repository,
            transcription_manager=transcription_manager,
        )
    )
    _EXPORT_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _EXPORT_TASKS.pop(job_id, None))

    return ExportJobCreateResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/api/export-jobs/{job_id}",
    )


@router.get("/export-jobs/{job_id}", response_model=ExportJobStatus)
async def get_export_job(job_id: str):
    """Get asynchronous export job status."""
    job = _EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return ExportJobStatus(**job)
