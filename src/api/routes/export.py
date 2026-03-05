"""Export endpoints for Obsidian integration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config.settings import get_settings
from src.audio.storage import ensure_session_audio_path, get_session_audio_path
from src.core.datetime_utils import localize_datetime, timezone_label, to_utc_iso
from src.core.log_utils import pipeline_step
from src.core.markdown_utils import (
    build_obsidian_markdown,
    format_datetime_human,
    format_duration_human,
    format_processing_time,
    week_folder,
)
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager
from src.summarization.prompts import TEMPLATE_INFO, get_template_content
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
    attendees: Optional[str] = None


class ExportResponse(BaseModel):
    success: bool
    filename: str
    filepath: str
    obsidian_uri: Optional[str] = None
    summary_preview: str
    summary_content: Optional[str] = None


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
    title: Optional[str] = None
    result: Optional[ExportResponse] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


class SaveRequest(BaseModel):
    edited_summary: Optional[str] = None
    revision_instruction: Optional[str] = None


class RefineRequest(BaseModel):
    instruction: str
    current_summary: str


class TranscriptionJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class TranscriptionJobStatus(BaseModel):
    job_id: str
    session_id: str
    status: str
    stage: str
    message: str
    transcription_progress: float
    overall_progress: float
    created_at: str
    updated_at: str
    error: Optional[str] = None


ProgressCallback = Callable[[str, str, Optional[float], Optional[float]], Awaitable[None] | None]
TranscriptionProgressCallback = Callable[[str, str, Optional[float]], Awaitable[None] | None]

_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_TASKS: dict[str, asyncio.Task] = {}
_TRANSCRIPTION_JOBS: dict[str, dict] = {}
_TRANSCRIPTION_TASKS: dict[str, asyncio.Task] = {}


@router.get("/templates")
async def get_templates():
    """Get available summary templates with their prompts."""
    templates_with_prompts = {}
    for key, info in TEMPLATE_INFO.items():
        templates_with_prompts[key] = {
            **info,
            "prompt": get_template_content(key),
        }
    return {"templates": templates_with_prompts}


@router.get("/templates/{template_key}")
async def get_template(template_key: str):
    """Get a specific template's content."""
    if template_key not in TEMPLATE_INFO:
        raise HTTPException(status_code=404, detail="Template not found")
    return {
        "key": template_key,
        **TEMPLATE_INFO[template_key],
        "prompt": get_template_content(template_key),
    }


def _utc_now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc)) or ""


def _compute_overall_progress(stage: str, transcription_progress: float, summarization_progress: float) -> float:
    # 40% transcription, 55% pipeline summarization, 5% final write.
    if stage == "queued":
        return 0.0
    if stage == "transcribing":
        return min(0.40 * transcription_progress, 0.40)
    if stage == "summarizing":
        return 0.40 + min(0.55 * summarization_progress, 0.55)
    if stage == "writing":
        return 0.95
    if stage in ("completed", "ready"):
        return 1.0
    return min(0.40 * transcription_progress + 0.55 * summarization_progress, 0.95)


def _compute_transcription_job_progress(stage: str, transcription_progress: float) -> float:
    if stage == "queued":
        return 0.0
    if stage == "transcribing":
        return max(0.0, min(transcription_progress, 0.99))
    if stage == "writing":
        return 0.99
    if stage == "completed":
        return 1.0
    return max(0.0, min(transcription_progress, 1.0))


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


def _create_transcription_job(session_id: str) -> dict:
    job_id = str(uuid.uuid4())
    now = _utc_now_iso()
    payload = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "queued",
        "stage": "queued",
        "message": "Queued",
        "transcription_progress": 0.0,
        "overall_progress": 0.0,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    _TRANSCRIPTION_JOBS[job_id] = payload
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


def _update_transcription_job(job_id: str, **fields) -> None:
    job = _TRANSCRIPTION_JOBS.get(job_id)
    if not job:
        return
    job.update(fields)
    stage = str(job.get("stage", "queued"))
    transcription_progress = float(job.get("transcription_progress", 0.0))
    job["overall_progress"] = _compute_transcription_job_progress(stage, transcription_progress)
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


def _segments_to_transcript(segments: list) -> tuple[str, float]:
    """Format transcript segments into a timestamped string and the max end time."""
    lines = []
    duration = 0.0
    for segment in segments:
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        marker = " [IMPORTANT]" if segment.is_important else ""
        speaker_prefix = f"{segment.speaker}: " if getattr(segment, "speaker", None) else ""
        lines.append(f"[{mins:02d}:{secs:02d}]{marker} {speaker_prefix}{segment.text}")
        duration = max(duration, float(segment.end_time))
    return "\n".join(lines).strip(), duration


async def _transcribe_and_persist_session(
    session_id: str,
    session,
    repository: Repository,
    transcription_manager: TranscriptionManager,
    primary_meeting_id: str,
    progress_callback: Optional[TranscriptionProgressCallback] = None,
) -> tuple[str, float]:
    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=400, detail="No recording audio available for this session")

    if progress_callback:
        maybe_awaitable = progress_callback("transcribing", "Starting transcription", 0.05)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    def on_transcription_progress(progress: float, message: str) -> None:
        if progress_callback:
            adjusted = 0.05 + (progress * 0.90)
            progress_callback("transcribing", message, adjusted)

    try:
        transcription_result, audio_duration_seconds = await transcription_manager.transcribe_file(
            audio_path,
            progress_callback=on_transcription_progress,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to transcribe recording audio: {str(e)}")

    full_text = transcription_result.text.strip()
    if not full_text:
        raise HTTPException(status_code=400, detail="No speech detected in recording audio")

    # Run speaker diarization if enabled (non-blocking on failure)
    from src.transcription.diarize import assign_speaker, diarize
    diarization_spans: list[tuple[float, float, str]] = []
    _settings = get_settings()
    if _settings.diarization_enabled and _settings.hf_token:
        try:
            with pipeline_step(logger, "diarization") as step:
                diarization_spans = await asyncio.to_thread(diarize, str(audio_path), _settings.hf_token)
                step["spans"] = len(diarization_spans)
        except Exception as exc:
            logger.warning(f"Diarization failed, continuing without speaker labels: {exc}")

    segment_count = 0
    with pipeline_step(logger, "segment_building", words=len(transcription_result.words or [])) as step:
        await repository.delete_segments_for_session(session_id)

        if transcription_result.words:
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
                            speaker=assign_speaker(start, end, diarization_spans) if diarization_spans else None,
                        )
                        segment_count += 1
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
                    speaker=assign_speaker(start, end, diarization_spans) if diarization_spans else None,
                )
                segment_count += 1
        else:
            await repository.add_segment(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                text=full_text,
                start_time=0.0,
                end_time=audio_duration_seconds,
                confidence=transcription_result.confidence,
                speaker=assign_speaker(0.0, audio_duration_seconds, diarization_spans) if diarization_spans else None,
            )
            segment_count = 1

        await repository.set_session_has_transcription(session_id, True)
        step["segments"] = segment_count

    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=500, detail="Failed to build transcript segments from recording audio")

    transcript, _ = _segments_to_transcript(segments)

    if progress_callback:
        maybe_awaitable = progress_callback("transcribing", "Transcription complete", 1.0)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    return transcript, audio_duration_seconds


def _build_transcript_from_segments(segments: list) -> tuple[str, float]:
    transcript, duration = _segments_to_transcript(segments)
    if not transcript:
        raise HTTPException(status_code=500, detail="Stored transcript segments are empty")
    return transcript, duration


async def _write_obsidian_file(
    markdown_content: str,
    relative_path: str,
    obsidian_vault_path: str,
) -> tuple[str, str]:
    """Write markdown to vault at relative_path (e.g. 'Meetings/2026 Week 12/16 Mon 0930 - Title.md').

    Creates subdirectories as needed. Returns (absolute_filepath, obsidian_uri).
    """
    vault_path = Path(obsidian_vault_path)
    if not vault_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Obsidian vault path does not exist: {obsidian_vault_path}",
        )
    filepath = vault_path / Path(relative_path)
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(markdown_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")

    vault_name = vault_path.name
    # Obsidian URIs use forward slashes regardless of OS
    uri_path = relative_path.replace("\\", "/")
    obsidian_uri = (
        f"obsidian://open?"
        f"vault={urllib.parse.quote(vault_name)}&"
        f"file={urllib.parse.quote(uri_path)}"
    )
    return str(filepath), obsidian_uri


async def _run_export_pipeline(
    session_id: str,
    request_payload: ExportRequest,
    summarization_manager: SummarizationManager,
    repository: Repository,
    transcription_manager: TranscriptionManager,
    progress_callback: Optional[ProgressCallback] = None,
) -> tuple[ExportResponse, dict]:
    """Build summary for a recording. Does NOT write to vault.

    Returns (ExportResponse, build_params). Vault write is deferred to the /save endpoint.
    build_params contains everything needed to assemble the final markdown at save time.

    Authoritative export pipeline:
      audio file -> transcription -> transcript segments -> summary -> markdown components
    """
    settings = get_settings()

    await _emit_progress(progress_callback, "transcribing", "Preparing recording", 0.03, 0.0)

    # Get session and its transcript
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

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

    # Reuse existing transcript when authoritative transcription exists and segments are present.
    existing_segments = await repository.get_segments(session_id=session_id) if session.has_transcription else []
    if session.has_transcription and existing_segments:
        await _emit_progress(progress_callback, "transcribing", "Reusing existing transcript", 0.2, 0.0)
        logger.info("[step] transcription | start | reuse=True | segments=%d", len(existing_segments))
        _tx_t0 = time.monotonic()

        # Run diarization on existing segments if enabled and not yet applied.
        _settings = get_settings()
        has_speakers = any(getattr(seg, "speaker", None) for seg in existing_segments)
        if _settings.diarization_enabled and _settings.hf_token and not has_speakers:
            audio_path = get_session_audio_path(session_id)
            if not audio_path and session.ended_at:
                audio_path = ensure_session_audio_path(session_id)
            if audio_path:
                await _emit_progress(progress_callback, "transcribing", "Running speaker diarization", 0.5, 0.0)
                try:
                    # Calculate speech end time to limit diarization processing
                    speech_end_time = max((seg.end_time for seg in existing_segments), default=None)
                    diarization_limit = speech_end_time + 5.0 if speech_end_time else None

                    from src.transcription.diarize import assign_speaker, diarize
                    limit_str = f"{diarization_limit:.1f}s" if diarization_limit else "none"
                    with pipeline_step(logger, "diarization", limit=limit_str) as step:
                        diarization_spans = await asyncio.to_thread(
                            diarize, str(audio_path), _settings.hf_token, duration_limit=diarization_limit
                        )
                        step["spans"] = len(diarization_spans)

                    speaker_updates = {
                        seg.id: assign_speaker(seg.start_time, seg.end_time, diarization_spans)
                        for seg in existing_segments
                    }
                    await repository.update_segments_speakers(speaker_updates)
                    existing_segments = await repository.get_segments(session_id=session_id)
                except Exception as exc:
                    logger.warning(f"Diarization failed on existing segments, continuing: {exc}")

        full_transcript, audio_duration_seconds = _build_transcript_from_segments(existing_segments)
        logger.info("[step] transcription | done | elapsed=%.1fs | chars=%d", time.monotonic() - _tx_t0, len(full_transcript))
        await _emit_progress(progress_callback, "transcribing", "Transcription complete", 1.0, 0.0)
    else:
        with pipeline_step(logger, "transcription") as step:
            full_transcript, audio_duration_seconds = await _transcribe_and_persist_session(
                session_id=session_id,
                session=session,
                repository=repository,
                transcription_manager=transcription_manager,
                primary_meeting_id=primary_meeting_id,
                progress_callback=(
                    lambda stage, message, progress: (
                        progress_callback(stage, message, progress, 0.0) if progress_callback else None
                    )
                ),
            )
            step["chars"] = len(full_transcript)

    # Generate summary
    template = request_payload.template

    await _emit_progress(progress_callback, "summarizing", "Generating summary", 1.0, 0.02)
    _sum_t0 = time.monotonic()
    logger.info("[step] summarization | start | chars=%d | template=%s", len(full_transcript), template)

    def _on_sum_progress(p: float) -> None:
        if progress_callback:
            progress_callback("summarizing", "Generating summary", 1.0, min(p, 0.99))

    try:
        summary_result = await summarization_manager.summarize(
            transcript=full_transcript,
            prompt_type=template,
            custom_instructions=request_payload.custom_prompt,
            attendees=request_payload.attendees,
            progress_callback=_on_sum_progress,
        )
    except Exception as e:
        logger.warning("[step] summarization | error | elapsed=%.1fs", time.monotonic() - _sum_t0)
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {str(e)}")

    summarization_duration = time.monotonic() - _sum_t0
    logger.info("[step] summarization | done | elapsed=%.1fs", summarization_duration)
    await _emit_progress(progress_callback, "summarizing", "Summary complete", 1.0, 1.0)

    # Persist resolved speaker names back to DB so the transcript UI shows real names
    if summary_result.speaker_map:
        segs = await repository.get_segments(session_id=session_id)
        speaker_updates = {
            seg.id: summary_result.speaker_map[seg.speaker]
            for seg in segs
            if getattr(seg, "speaker", None) in summary_result.speaker_map
        }
        if speaker_updates:
            await repository.update_segments_speakers(speaker_updates)
            logger.info(f"Resolved speaker labels for {len(speaker_updates)} segments: {summary_result.speaker_map}")

    template_label = TEMPLATE_INFO.get(template, {}).get("name", template.title())

    await repository.add_summary(
        meeting_id=primary_meeting_id,
        content=summary_result.content,
        backend=summary_result.backend,
        model=summary_result.model,
        prompt_tokens=summary_result.prompt_tokens,
        completion_tokens=summary_result.completion_tokens,
        processing_duration_seconds=summarization_duration,
        template=template_label,
    )

    summary_content = summary_result.content
    processing_time_str = format_processing_time(summarization_duration)

    # Build filename and folder
    local_started_at = localize_datetime(
        session.started_at,
        session.timezone_name,
        session.timezone_offset_minutes,
    )
    tz_label = timezone_label(session.timezone_name, session.timezone_offset_minutes)
    safe_title = re.sub(r'[<>:"/\\|?*]', '', request_payload.title.strip())
    dow = local_started_at.strftime("%a")   # Mon, Tue, …
    time_hhmm = local_started_at.strftime("%H%M")  # 0930
    base_filename = f"{local_started_at.day:02d} {dow} {time_hhmm} - {safe_title}"
    
    # Check for existing summaries to determine version
    existing_summaries = await repository.get_summaries(primary_meeting_id)
    version_suffix = ""
    if len(existing_summaries) > 1:
        version_suffix = f" (v{len(existing_summaries)})"
    
    filename = f"{base_filename}{version_suffix}.md"
    week_folder_name = week_folder(local_started_at)
    relative_path = f"Meetings/{week_folder_name}/{filename}"

    recorded_at = format_datetime_human(local_started_at, tz_label)
    duration_str = format_duration_human(int(audio_duration_seconds))

    await _emit_progress(progress_callback, "writing", "Summary ready", 1.0, 1.0)

    build_params = {
        "summary_content": summary_content,
        "transcript": full_transcript,
        "template_label": template_label,
        "recorded_at": recorded_at,
        "tz_label": tz_label,
        "session_timezone_name": session.timezone_name,
        "session_timezone_offset_minutes": session.timezone_offset_minutes,
        "duration_str": duration_str,
        "processing_time_str": processing_time_str,
        "filename": filename,
        "relative_path": relative_path,
    }

    preview = summary_content[:200] + "..." if len(summary_content) > 200 else summary_content
    response = ExportResponse(
        success=True,
        filename=filename,
        filepath="",        # filled in at save time
        obsidian_uri=None,  # filled in at save time
        summary_preview=preview,
        summary_content=summary_content,
    )
    return response, build_params


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

    _job_t0 = time.monotonic()
    logger.info(
        "[step] export_job | start | session=%s | template=%s | job=%s",
        session_id, request_payload.template, job_id,
    )
    try:
        result, build_params = await _run_export_pipeline(
            session_id=session_id,
            request_payload=request_payload,
            summarization_manager=summarization_manager,
            repository=repository,
            transcription_manager=transcription_manager,
            progress_callback=update_progress,
        )
        job = _EXPORT_JOBS.get(job_id, {})
        job["build_params"] = build_params
        _update_export_job(
            job_id,
            status="ready",
            stage="ready",
            message="Summary ready for review",
            transcription_progress=1.0,
            summarization_progress=1.0,
            result=result.model_dump(),
            error=None,
        )
        logger.info("[step] export_job | done | elapsed=%.1fs | job=%s", time.monotonic() - _job_t0, job_id)
    except HTTPException as exc:
        logger.warning(
            "[step] export_job | error | elapsed=%.1fs | status=%d | job=%s",
            time.monotonic() - _job_t0, exc.status_code, job_id,
        )
        _update_export_job(
            job_id,
            status="failed",
            stage="failed",
            message="Export failed",
            error=str(exc.detail),
        )
    except Exception as exc:
        logger.warning(
            "[step] export_job | error | elapsed=%.1fs | job=%s",
            time.monotonic() - _job_t0, job_id,
        )
        _update_export_job(
            job_id,
            status="failed",
            stage="failed",
            message="Export failed",
            error=str(exc),
        )


async def _run_transcription_job(
    job_id: str,
    session_id: str,
    repository: Repository,
    transcription_manager: TranscriptionManager,
) -> None:
    def update_progress(stage: str, message: str, transcription_progress: Optional[float]) -> None:
        updates = {
            "status": "running",
            "stage": stage,
            "message": message,
        }
        if transcription_progress is not None:
            updates["transcription_progress"] = max(0.0, min(1.0, transcription_progress))
        _update_transcription_job(job_id, **updates)

    try:
        session = await repository.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
        if meetings:
            primary_meeting_id = meetings[0].id
        else:
            meeting = await repository.create_meeting(session_id=session.id, title="Untitled Recording")
            primary_meeting_id = meeting.id

        await _transcribe_and_persist_session(
            session_id=session_id,
            session=session,
            repository=repository,
            transcription_manager=transcription_manager,
            primary_meeting_id=primary_meeting_id,
            progress_callback=update_progress,
        )

        _update_transcription_job(
            job_id,
            status="completed",
            stage="completed",
            message="Transcription complete",
            transcription_progress=1.0,
            error=None,
        )
    except HTTPException as exc:
        _update_transcription_job(
            job_id,
            status="failed",
            stage="failed",
            message="Transcription failed",
            error=str(exc.detail),
        )
    except Exception as exc:
        _update_transcription_job(
            job_id,
            status="failed",
            stage="failed",
            message="Transcription failed",
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
    """Synchronous export endpoint (legacy). Builds and immediately writes to vault."""
    settings = get_settings()
    result, bp = await _run_export_pipeline(
        session_id=session_id,
        request_payload=request,
        summarization_manager=summarization_manager,
        repository=repository,
        transcription_manager=transcription_manager,
    )
    local_exported_at = localize_datetime(
        datetime.now(timezone.utc),
        bp.get("session_timezone_name"),
        bp.get("session_timezone_offset_minutes"),
    )
    exported_at = format_datetime_human(local_exported_at, bp["tz_label"])
    markdown_content = build_obsidian_markdown(
        content=bp["summary_content"],
        template_label=bp["template_label"],
        recorded_at=bp["recorded_at"],
        exported_at=exported_at,
        duration_str=bp["duration_str"],
        processing_time_str=bp["processing_time_str"],
        transcript=bp["transcript"],
    )
    filepath, obsidian_uri = await _write_obsidian_file(
        markdown_content, bp["relative_path"], settings.obsidian_vault_path
    )
    result.filepath = filepath
    result.obsidian_uri = obsidian_uri
    return result


@router.post("/recordings/{session_id}/transcription-job", response_model=TranscriptionJobCreateResponse)
async def start_transcription_job(
    session_id: str,
    repository: Repository = Depends(get_repository),
    transcription_manager: TranscriptionManager = Depends(get_transcription_manager),
):
    """Run authoritative transcription only (no summary/export)."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    job = _create_transcription_job(session_id)
    job_id = str(job["job_id"])
    _update_transcription_job(job_id, message="Starting transcription")

    task = asyncio.create_task(
        _run_transcription_job(
            job_id=job_id,
            session_id=session_id,
            repository=repository,
            transcription_manager=transcription_manager,
        )
    )
    _TRANSCRIPTION_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _TRANSCRIPTION_TASKS.pop(job_id, None))

    return TranscriptionJobCreateResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/api/transcription-jobs/{job_id}",
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
    job["title"] = request.title
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


@router.post("/export-jobs/{job_id}/save")
async def save_export_job(job_id: str, request: SaveRequest):
    """Assemble final markdown and write to Obsidian vault."""
    job = _EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if job.get("status") not in ("ready", "completed"):
        raise HTTPException(status_code=400, detail="Job is not ready to save")

    bp = job.get("build_params")
    result_data = job.get("result")
    if not bp or not result_data:
        raise HTTPException(status_code=400, detail="Job is missing build data")

    # Compute exported_at at the moment the user actually saves
    local_exported_at = localize_datetime(
        datetime.now(timezone.utc),
        bp.get("session_timezone_name"),
        bp.get("session_timezone_offset_minutes"),
    )
    exported_at = format_datetime_human(local_exported_at, bp["tz_label"])

    summary = request.edited_summary.strip() if request.edited_summary and request.edited_summary.strip() else bp["summary_content"]

    markdown_content = build_obsidian_markdown(
        content=summary,
        template_label=bp["template_label"],
        recorded_at=bp["recorded_at"],
        exported_at=exported_at,
        duration_str=bp["duration_str"],
        processing_time_str=bp["processing_time_str"],
        transcript=bp["transcript"],
        revision_instruction=request.revision_instruction,
    )

    settings = get_settings()
    with pipeline_step(logger, "vault_write", path=bp["relative_path"]):
        filepath, obsidian_uri = await _write_obsidian_file(
            markdown_content, bp["relative_path"], settings.obsidian_vault_path
        )

    _update_export_job(job_id, status="completed", stage="completed", message="Saved to Obsidian")

    return {
        "success": True,
        "filename": bp["filename"],
        "filepath": filepath,
        "obsidian_uri": obsidian_uri,
        "summary_preview": result_data.get("summary_preview", ""),
    }


@router.post("/export-jobs/{job_id}/refine")
async def refine_export_job(
    job_id: str,
    request: RefineRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Apply a single AI revision pass to the current summary (active job)."""
    job = _EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if job.get("status") not in ("ready", "completed"):
        raise HTTPException(status_code=400, detail="Job is not ready to refine")

    try:
        revised = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=request.current_summary,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(e)}")

    return {"revised_summary": revised}


@router.post("/summaries/refine")
async def refine_summary(
    request: RefineRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Apply a single AI revision pass to any provided summary text."""
    try:
        revised = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=request.current_summary,
        )
        return {"revised_summary": revised}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(e)}")


@router.get("/transcription-jobs/{job_id}", response_model=TranscriptionJobStatus)
async def get_transcription_job(job_id: str):
    """Get transcription-only job status."""
    job = _TRANSCRIPTION_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    return TranscriptionJobStatus(**job)
