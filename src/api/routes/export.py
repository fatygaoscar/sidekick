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
from src.core.speaker_labels import (
    build_user_facing_speaker_map,
    infer_strict_segment_speakers,
    resolve_user_facing_speaker_name,
)
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager
from src.summarization.prompts import PUBLIC_TEMPLATE_KEYS, TEMPLATE_INFO, get_template_content
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


class DraftUpdateRequest(BaseModel):
    content: str


class DraftReviseRequest(BaseModel):
    instruction: str


class CreateDraftRequest(BaseModel):
    source_summary_id: Optional[str] = None
    source_type: str = "manual_edit"


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


class SummaryJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


class SummaryJobResult(BaseModel):
    draft_summary_id: str
    summary_preview: str
    summary_content: str


class SummaryJobStatus(BaseModel):
    job_id: str
    session_id: str
    status: str
    stage: str
    message: str
    transcription_progress: float
    summarization_progress: float
    overall_progress: float
    created_at: str
    updated_at: str
    result: Optional[SummaryJobResult] = None
    error: Optional[str] = None


ProgressCallback = Callable[[str, str, Optional[float], Optional[float]], Awaitable[None] | None]
TranscriptionProgressCallback = Callable[[str, str, Optional[float]], Awaitable[None] | None]

_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_TASKS: dict[str, asyncio.Task] = {}
_TRANSCRIPTION_JOBS: dict[str, dict] = {}
_TRANSCRIPTION_TASKS: dict[str, asyncio.Task] = {}
_SUMMARY_JOBS: dict[str, dict] = {}
_SUMMARY_TASKS: dict[str, asyncio.Task] = {}


@router.get("/templates")
async def get_templates():
    """Get available summary templates with their prompts."""
    templates_with_prompts = {}
    for key in PUBLIC_TEMPLATE_KEYS:
        info = TEMPLATE_INFO[key]
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


def _speaker_identity(segment) -> str | None:
    return getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)


def _speaker_review_required(segments: list) -> bool:
    raw_clusters = {
        identity
        for identity in (_speaker_identity(segment) for segment in segments)
        if identity and str(identity).startswith("SPEAKER_")
    }
    return len(raw_clusters) > 1


def _speaker_review_blocks_summary(meeting) -> bool:
    return False


async def _update_meeting_speaker_review_state(
    repository: Repository,
    meeting_id: str,
    segments: list,
) -> None:
    required = _speaker_review_required(segments)
    completed_at = None if required else datetime.utcnow()
    await repository.update_meeting_settings(
        meeting_id,
        speaker_review_required=required,
        speaker_review_completed_at=completed_at,
    )


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


def _create_summary_job(session_id: str) -> dict:
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
    _SUMMARY_JOBS[job_id] = payload
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


def _update_summary_job(job_id: str, **fields) -> None:
    job = _SUMMARY_JOBS.get(job_id)
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


def _segments_to_transcript(segments: list) -> tuple[str, float]:
    """Format transcript segments into a timestamped string and the max end time."""
    lines = []
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
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        marker = " [IMPORTANT]" if segment.is_important else ""
        effective_speaker = getattr(segment, "speaker", None) or (inferred or {}).get("speaker")
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
                diarization_spans = await asyncio.to_thread(
                    diarize,
                    str(audio_path),
                    _settings.hf_token,
                )
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
                            speaker_cluster=(
                                assign_speaker(start, end, diarization_spans) if diarization_spans else None
                            ),
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
                    speaker_cluster=assign_speaker(start, end, diarization_spans) if diarization_spans else None,
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
                speaker_cluster=(
                    assign_speaker(0.0, audio_duration_seconds, diarization_spans)
                    if diarization_spans
                    else None
                ),
            )
            segment_count = 1

        await repository.set_session_has_transcription(session_id, True)
        step["segments"] = segment_count

    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=500, detail="Failed to build transcript segments from recording audio")
    await _update_meeting_speaker_review_state(repository, primary_meeting_id, segments)

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
    primary_meeting = None
    primary_meeting_id = None
    if meetings:
        primary_meeting = meetings[0]
        primary_meeting_id = primary_meeting.id
        await repository.update_meeting_settings(
            primary_meeting.id,
            title=request_payload.title,
            template_key=request_payload.template,
            custom_prompt=request_payload.custom_prompt,
        )
    else:
        primary_meeting = await repository.create_meeting(session_id=session.id, title=request_payload.title)
        primary_meeting_id = primary_meeting.id
        await repository.update_meeting_settings(
            primary_meeting_id,
            template_key=request_payload.template,
            custom_prompt=request_payload.custom_prompt,
        )

    # Reuse existing transcript when authoritative transcription exists and segments are present.
    existing_segments = await repository.get_segments(session_id=session_id) if session.has_transcription else []
    if session.has_transcription and existing_segments:
        await _emit_progress(progress_callback, "transcribing", "Reusing existing transcript", 0.2, 0.0)
        logger.info("[step] transcription | start | reuse=True | segments=%d", len(existing_segments))
        _tx_t0 = time.monotonic()

        # Run diarization on existing segments if enabled and not yet applied.
        _settings = get_settings()
        has_speaker_clusters = any(_speaker_identity(seg) for seg in existing_segments)
        should_diarize = _settings.diarization_enabled and _settings.hf_token
        # Do not overwrite reviewed speaker assignments during re-summarization.
        if primary_meeting and primary_meeting.speaker_review_completed_at:
            should_diarize = False
        else:
            should_diarize = should_diarize and not has_speaker_clusters
        
        if should_diarize:
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
                            diarize,
                            str(audio_path),
                            _settings.hf_token,
                            duration_limit=diarization_limit,
                        )
                        step["spans"] = len(diarization_spans)

                    speaker_updates = {
                        seg.id: {
                            "speaker": assign_speaker(seg.start_time, seg.end_time, diarization_spans),
                            "speaker_cluster": assign_speaker(
                                seg.start_time,
                                seg.end_time,
                                diarization_spans,
                            ),
                        }
                        for seg in existing_segments
                    }
                    await repository.update_segments_speaker_metadata(speaker_updates)
                    existing_segments = await repository.get_segments(session_id=session_id)
                    await _update_meeting_speaker_review_state(
                        repository,
                        primary_meeting_id,
                        existing_segments,
                    )
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

    # Unload Whisper to free VRAM for summarization model
    await transcription_manager.unload()
    logger.info("[step] transcription | unloaded model to free VRAM")

    # Generate summary
    template = request_payload.template

    await _emit_progress(progress_callback, "summarizing", "Generating summary", 1.0, 0.02)
    _sum_t0 = time.monotonic()
    logger.info(
        "[step] summarization | start | chars=%d | template=%s | custom_prompt_chars=%d",
        len(full_transcript),
        template,
        len((request_payload.custom_prompt or "").strip()),
    )

    def _on_sum_progress(p: float) -> None:
        if progress_callback:
            progress_callback("summarizing", "Generating summary", 1.0, min(p, 0.99))

    try:
        summary_result = await summarization_manager.summarize(
            transcript=full_transcript,
            prompt_type=template,
            custom_instructions=request_payload.custom_prompt,
            progress_callback=_on_sum_progress,
        )
    except Exception as e:
        logger.warning("[step] summarization | error | elapsed=%.1fs", time.monotonic() - _sum_t0)
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {str(e)}")

    summarization_duration = time.monotonic() - _sum_t0
    logger.info("[step] summarization | done | elapsed=%.1fs", summarization_duration)
    await _emit_progress(progress_callback, "summarizing", "Summary complete", 1.0, 1.0)

    template_label = TEMPLATE_INFO.get(template, {}).get("name", template.title())
    latest_saved = await repository.get_latest_summary(primary_meeting_id, status="saved")
    draft_summary = await repository.replace_draft_summary(
        meeting_id=primary_meeting_id,
        content=summary_result.content,
        backend=summary_result.backend,
        model=summary_result.model,
        prompt_tokens=summary_result.prompt_tokens,
        completion_tokens=summary_result.completion_tokens,
        processing_duration_seconds=summarization_duration,
        template=template_label,
        source_type="generated",
        parent_summary_id=latest_saved.id if latest_saved else None,
        template_key=template,
        custom_prompt=request_payload.custom_prompt,
        pass1_system_prompt=summary_result.prompt_audit.get("pass1_system_prompt"),
        pass1_user_prompt=summary_result.prompt_audit.get("pass1_user_prompt"),
        pass2_system_prompt=summary_result.prompt_audit.get("pass2_system_prompt"),
        pass2_user_prompt=summary_result.prompt_audit.get("pass2_user_prompt"),
        attendees_snapshot=None,
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
    existing_summaries = await repository.get_summaries(primary_meeting_id, status="saved")
    version_suffix = ""
    if existing_summaries:
        version_suffix = f" (v{len(existing_summaries) + 1})"
    
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
        "draft_summary_id": draft_summary.id,
        "meeting_id": primary_meeting_id,
        "template_key": template,
        "custom_prompt": request_payload.custom_prompt,
        "pass1_system_prompt": summary_result.prompt_audit.get("pass1_system_prompt"),
        "pass1_user_prompt": summary_result.prompt_audit.get("pass1_user_prompt"),
        "pass2_system_prompt": summary_result.prompt_audit.get("pass2_system_prompt"),
        "pass2_user_prompt": summary_result.prompt_audit.get("pass2_user_prompt"),
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


async def _build_summary_save_params(
    repository: Repository,
    session,
    meeting,
    *,
    summary_content: str,
    template_label: str,
    processing_duration_seconds: float | None,
    pass1_system_prompt: str | None = None,
    pass1_user_prompt: str | None = None,
    pass2_system_prompt: str | None = None,
    pass2_user_prompt: str | None = None,
) -> dict:
    """Assemble markdown and path metadata for saving a summary draft."""
    segments = await repository.get_segments(session_id=session.id)
    transcript, audio_duration_seconds = _segments_to_transcript(segments)

    local_started_at = localize_datetime(
        session.started_at,
        session.timezone_name,
        session.timezone_offset_minutes,
    )
    tz_label = timezone_label(session.timezone_name, session.timezone_offset_minutes)
    safe_title = re.sub(r'[<>:"/\\|?*]', "", (meeting.title or "Untitled Recording").strip())
    dow = local_started_at.strftime("%a")
    time_hhmm = local_started_at.strftime("%H%M")
    base_filename = f"{local_started_at.day:02d} {dow} {time_hhmm} - {safe_title or 'Untitled Recording'}"
    existing_summaries = await repository.get_summaries(meeting.id, status="saved")
    version_suffix = f" (v{len(existing_summaries) + 1})" if existing_summaries else ""
    filename = f"{base_filename}{version_suffix}.md"
    week_folder_name = week_folder(local_started_at)
    relative_path = f"Meetings/{week_folder_name}/{filename}"
    recorded_at = format_datetime_human(local_started_at, tz_label)
    processing_time_str = (
        format_processing_time(processing_duration_seconds)
        if processing_duration_seconds
        else ""
    )
    duration_str = format_duration_human(int(audio_duration_seconds))

    return {
        "summary_content": summary_content,
        "template_label": template_label,
        "transcript": transcript,
        "recorded_at": recorded_at,
        "tz_label": tz_label,
        "session_timezone_name": session.timezone_name,
        "session_timezone_offset_minutes": session.timezone_offset_minutes,
        "duration_str": duration_str,
        "processing_time_str": processing_time_str,
        "filename": filename,
        "relative_path": relative_path,
        "pass1_system_prompt": pass1_system_prompt,
        "pass1_user_prompt": pass1_user_prompt,
        "pass2_system_prompt": pass2_system_prompt,
        "pass2_user_prompt": pass2_user_prompt,
    }


async def _run_summary_job(
    job_id: str,
    session_id: str,
    summarization_manager: SummarizationManager,
    repository: Repository,
) -> None:
    """Generate or regenerate a draft summary from an existing reviewed transcript."""

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
        _update_summary_job(job_id, **updates)

    try:
        session = await repository.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        meeting = await repository.get_primary_meeting(session_id)
        if not meeting:
            raise HTTPException(status_code=400, detail="No meeting found for this recording")

        segments = await repository.get_segments(session_id=session_id)
        if not session.has_transcription or not segments:
            raise HTTPException(
                status_code=409,
                detail="Transcription must complete before generating a summary",
            )
        transcript, _ = _build_transcript_from_segments(segments)
        template_key = meeting.template_key or "meeting"
        await _emit_progress(update_progress, "summarizing", "Generating summary", 1.0, 0.02)
        started_at = time.monotonic()

        def on_progress(progress: float) -> None:
            update_progress("summarizing", "Generating summary", 1.0, min(progress, 0.99))

        summary_result = await summarization_manager.summarize(
            transcript=transcript,
            prompt_type=template_key,
            custom_instructions=meeting.custom_prompt,
            progress_callback=on_progress,
        )
        processing_duration = time.monotonic() - started_at
        template_label = TEMPLATE_INFO.get(template_key, {}).get("name", template_key.title())
        latest_saved = await repository.get_latest_summary(meeting.id, status="saved")
        draft = await repository.replace_draft_summary(
            meeting_id=meeting.id,
            content=summary_result.content,
            backend=summary_result.backend,
            model=summary_result.model,
            prompt_tokens=summary_result.prompt_tokens,
            completion_tokens=summary_result.completion_tokens,
            processing_duration_seconds=processing_duration,
            template=template_label,
            source_type="resummarized" if latest_saved else "generated",
            parent_summary_id=latest_saved.id if latest_saved else None,
            template_key=template_key,
            custom_prompt=meeting.custom_prompt,
            pass1_system_prompt=summary_result.prompt_audit.get("pass1_system_prompt"),
            pass1_user_prompt=summary_result.prompt_audit.get("pass1_user_prompt"),
            pass2_system_prompt=summary_result.prompt_audit.get("pass2_system_prompt"),
            pass2_user_prompt=summary_result.prompt_audit.get("pass2_user_prompt"),
            attendees_snapshot=None,
        )
        preview = (
            summary_result.content[:200] + "..."
            if len(summary_result.content) > 200
            else summary_result.content
        )
        _update_summary_job(
            job_id,
            status="completed",
            stage="completed",
            message="Summary draft ready",
            transcription_progress=1.0,
            summarization_progress=1.0,
            result={
                "draft_summary_id": draft.id,
                "summary_preview": preview,
                "summary_content": summary_result.content,
            },
            error=None,
        )
    except HTTPException as exc:
        _update_summary_job(
            job_id,
            status="failed",
            stage="failed",
            message="Summary generation failed",
            error=str(exc.detail),
        )
    except Exception as exc:
        _update_summary_job(
            job_id,
            status="failed",
            stage="failed",
            message="Summary generation failed",
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
        pass1_system_prompt=bp.get("pass1_system_prompt"),
        pass1_user_prompt=bp.get("pass1_user_prompt"),
        pass2_system_prompt=bp.get("pass2_system_prompt"),
        pass2_user_prompt=bp.get("pass2_user_prompt"),
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


@router.post("/recordings/{session_id}/summary-job", response_model=SummaryJobCreateResponse)
async def start_summary_job(
    session_id: str,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Generate a draft summary from the current recording workspace settings."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    job = _create_summary_job(session_id)
    job_id = str(job["job_id"])
    _update_summary_job(job_id, message="Starting summary generation")

    task = asyncio.create_task(
        _run_summary_job(
            job_id=job_id,
            session_id=session_id,
            summarization_manager=summarization_manager,
            repository=repository,
        )
    )
    _SUMMARY_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _SUMMARY_TASKS.pop(job_id, None))

    return SummaryJobCreateResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/api/summary-jobs/{job_id}",
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


@router.get("/summary-jobs/{job_id}", response_model=SummaryJobStatus)
async def get_summary_job(job_id: str):
    """Get async summary-draft generation status."""
    job = _SUMMARY_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Summary job not found")
    return SummaryJobStatus(**job)


@router.get("/export-jobs/{job_id}", response_model=ExportJobStatus)
async def get_export_job(job_id: str):
    """Get asynchronous export job status."""
    job = _EXPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return ExportJobStatus(**job)


@router.post("/export-jobs/{job_id}/save")
async def save_export_job(
    job_id: str,
    request: SaveRequest,
    repository: Repository = Depends(get_repository),
):
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

    summary = (
        request.edited_summary.strip()
        if request.edited_summary and request.edited_summary.strip()
        else bp["summary_content"]
    )

    markdown_content = build_obsidian_markdown(
        content=summary,
        template_label=bp["template_label"],
        recorded_at=bp["recorded_at"],
        exported_at=exported_at,
        duration_str=bp["duration_str"],
        processing_time_str=bp["processing_time_str"],
        transcript=bp["transcript"],
        pass1_system_prompt=bp.get("pass1_system_prompt"),
        pass1_user_prompt=bp.get("pass1_user_prompt"),
        pass2_system_prompt=bp.get("pass2_system_prompt"),
        pass2_user_prompt=bp.get("pass2_user_prompt"),
        revision_instruction=request.revision_instruction,
    )

    settings = get_settings()
    with pipeline_step(logger, "vault_write", path=bp["relative_path"]):
        filepath, obsidian_uri = await _write_obsidian_file(
            markdown_content, bp["relative_path"], settings.obsidian_vault_path
        )

    draft_id = bp.get("draft_summary_id")
    saved_at = datetime.utcnow()
    if draft_id:
        if request.edited_summary and request.edited_summary.strip():
            source_type = "ai_revised" if request.revision_instruction else "manual_edit"
            await repository.update_summary(
                draft_id,
                content=summary,
                source_type=source_type,
            )
        await repository.save_draft_summary(
            draft_id,
            saved_to_obsidian_at=saved_at,
            obsidian_relative_path=bp["relative_path"],
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


@router.post("/recordings/{session_id}/summary-draft")
async def create_summary_draft(
    session_id: str,
    request: CreateDraftRequest,
    repository: Repository = Depends(get_repository),
):
    """Create an editable draft from the latest saved summary when needed."""
    meeting = await repository.get_primary_meeting(session_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Recording not found")

    existing_draft = await repository.get_draft_summary(meeting.id)
    if existing_draft:
        return {"draft_summary_id": existing_draft.id}

    source_summary_id = request.source_summary_id
    if not source_summary_id:
        latest_saved = await repository.get_latest_summary(meeting.id, status="saved")
        if not latest_saved:
            raise HTTPException(status_code=404, detail="No saved summary available to revise")
        source_summary_id = latest_saved.id

    try:
        draft = await repository.create_draft_from_summary(
            source_summary_id,
            source_type=request.source_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {"draft_summary_id": draft.id}


@router.patch("/summary-drafts/{summary_id}")
async def update_summary_draft(
    summary_id: str,
    request: DraftUpdateRequest,
    repository: Repository = Depends(get_repository),
):
    """Update draft content after manual edits."""
    draft = await repository.get_summary(summary_id)
    if not draft or draft.status != "draft":
        raise HTTPException(status_code=404, detail="Draft summary not found")

    updated = await repository.update_summary(
        summary_id,
        content=request.content,
        source_type="manual_edit",
    )
    return {"draft_summary_id": updated.id, "content": updated.content}


@router.post("/summary-drafts/{summary_id}/revise")
async def revise_summary_draft(
    summary_id: str,
    request: DraftReviseRequest,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Apply a single AI revision pass to a saved draft."""
    draft = await repository.get_summary(summary_id)
    if not draft or draft.status != "draft":
        raise HTTPException(status_code=404, detail="Draft summary not found")

    try:
        revised = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=draft.content,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(exc)}")

    updated = await repository.update_summary(
        summary_id,
        content=revised,
        source_type="ai_revised",
    )
    return {"draft_summary_id": updated.id, "content": updated.content}


@router.post("/summary-drafts/{summary_id}/save")
async def save_summary_draft(
    summary_id: str,
    repository: Repository = Depends(get_repository),
):
    """Commit a draft summary as a saved version and write the Obsidian note."""
    draft = await repository.get_summary(summary_id)
    if not draft or draft.status != "draft":
        raise HTTPException(status_code=404, detail="Draft summary not found")

    meeting = await repository.get_meeting(draft.meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    session = await repository.get_session(meeting.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    params = await _build_summary_save_params(
        repository,
        session,
        meeting,
        summary_content=draft.content,
        template_label=draft.template or TEMPLATE_INFO.get(
            draft.template_key or meeting.template_key or "meeting",
            {},
        ).get("name", "Meeting"),
        processing_duration_seconds=draft.processing_duration_seconds,
        pass1_system_prompt=draft.pass1_system_prompt,
        pass1_user_prompt=draft.pass1_user_prompt,
        pass2_system_prompt=draft.pass2_system_prompt,
        pass2_user_prompt=draft.pass2_user_prompt,
    )

    obsidian_uri = None
    settings = get_settings()
    if settings.obsidian_vault_path:
        local_exported_at = localize_datetime(
            datetime.now(timezone.utc),
            params.get("session_timezone_name"),
            params.get("session_timezone_offset_minutes"),
        )
        exported_at = format_datetime_human(local_exported_at, params["tz_label"])
        markdown_content = build_obsidian_markdown(
            content=params["summary_content"],
            template_label=params["template_label"],
            recorded_at=params["recorded_at"],
            exported_at=exported_at,
            duration_str=params["duration_str"],
            processing_time_str=params["processing_time_str"],
            transcript=params["transcript"],
            pass1_system_prompt=params.get("pass1_system_prompt"),
            pass1_user_prompt=params.get("pass1_user_prompt"),
            pass2_system_prompt=params.get("pass2_system_prompt"),
            pass2_user_prompt=params.get("pass2_user_prompt"),
        )
        _, obsidian_uri = await _write_obsidian_file(
            markdown_content,
            params["relative_path"],
            settings.obsidian_vault_path,
        )

    saved_summary = await repository.save_draft_summary(
        summary_id,
        saved_to_obsidian_at=datetime.utcnow(),
        obsidian_relative_path=params["relative_path"] if settings.obsidian_vault_path else None,
    )

    return {
        "success": True,
        "summary_id": saved_summary.id,
        "filename": params["filename"],
        "obsidian_uri": obsidian_uri,
        "relative_path": params["relative_path"],
    }


@router.get("/transcription-jobs/{job_id}", response_model=TranscriptionJobStatus)
async def get_transcription_job(job_id: str):
    """Get transcription-only job status."""
    job = _TRANSCRIPTION_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    return TranscriptionJobStatus(**job)
