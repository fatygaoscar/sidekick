"""Export endpoints for Obsidian integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
)
from src.core.obsidian_exports import (
    build_archive_relative_path,
    copy_obsidian_markdown_with_frontmatter_updates,
    extract_tags_from_frontmatter,
    find_note_by_summary_id,
    resolve_latest_export_target,
    write_obsidian_markdown_atomic,
)
from src.core.speaker_labels import (
    build_user_facing_speaker_map,
    infer_strict_segment_speakers,
    resolve_user_facing_speaker_name,
)
from src.sessions.repository import Repository
from src.summarization.manager import (
    SummarizationManager,
    classify_revision_route,
    select_revision_evidence_windows,
)
from src.summarization.prompts import (
    DEFAULT_TEMPLATE_KEY,
    PUBLIC_TEMPLATE_KEYS,
    TEMPLATE_INFO,
    get_template_content,
    normalize_template_key,
)
from src.transcription.manager import TranscriptionManager
from src.transcription.audio_decode import media_duration_seconds
from src.transcription.speaker_review_state import (
    speaker_identity,
    speaker_review_update_fields,
)
from src.transcription.speaker_profiles import apply_profile_matches_to_segments, match_segments_to_profiles
from src.workspace_chat.service import WorkspaceChatService


router = APIRouter()


def _safe_json_loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _summary_revision_history(summary) -> list[dict]:
    workflow_data = _safe_json_loads(getattr(summary, "workflow_data_json", None), {})
    history = workflow_data.get("revision_history", []) if isinstance(workflow_data, dict) else []
    return history if isinstance(history, list) else []


def _saved_summary_version_map(saved_summaries: list) -> dict[str, int]:
    ordered = list(saved_summaries or [])
    total_saved = len(ordered)
    return {
        str(getattr(summary, "id", "")): total_saved - index
        for index, summary in enumerate(ordered)
    }


def _latest_exported_summary(saved_summaries: list):
    return next(
        (
            summary
            for summary in (saved_summaries or [])
            if getattr(summary, "saved_to_obsidian_at", None)
            or getattr(summary, "obsidian_relative_path", None)
        ),
        None,
    )


def _build_obsidian_frontmatter(
    *,
    meeting_id: str,
    summary_id: str | None,
    transcript_version_id: str | None,
    display_id: str,
    summary_version_number: int,
    transcript_version_number: int | None,
    meeting_title: str,
    local_started_at: datetime,
    target,
    template_key: str | None,
    local_exported_at: datetime | None = None,
    recording_duration_minutes: int | None = None,
    tags: list[str] | None = None,
) -> dict[str, object]:
    frontmatter: dict[str, object] = {
        "type": "meeting-note",
        "sidekick_meeting_id": meeting_id,
        "sidekick_summary_id": summary_id,
        "sidekick_transcript_version_id": transcript_version_id,
        "sidekick_summary_version": summary_version_number,
        "sidekick_export_status": "latest",
        "meeting_date": local_started_at.strftime("%Y-%m-%d"),
        "meeting_month": target.meeting_month,
        "template_key": template_key or "",
        "tags": list(tags or []),
    }
    if transcript_version_id:
        frontmatter["sidekick_transcript_version_id"] = transcript_version_id
    if transcript_version_number:
        frontmatter["sidekick_transcript_version"] = transcript_version_number
    if recording_duration_minutes is not None:
        frontmatter["recording_duration_minutes"] = recording_duration_minutes
    if local_exported_at:
        frontmatter["exported_at"] = local_exported_at.isoformat()
    return frontmatter


def _resolve_exported_note_path(
    summary,
    *,
    obsidian_vault_path: str,
) -> tuple[str | None, bool]:
    """Return the on-disk note path for a saved export and whether it was rediscovered elsewhere."""
    source_relative_path = getattr(summary, "obsidian_relative_path", None)
    if not source_relative_path:
        return None, False
    vault_root = Path(obsidian_vault_path)
    source_path = vault_root / Path(source_relative_path)
    if source_path.exists():
        return source_relative_path, False
    discovered_relative_path = find_note_by_summary_id(
        obsidian_vault_path=obsidian_vault_path,
        summary_id=str(getattr(summary, "id", "")),
    )
    if discovered_relative_path and discovered_relative_path != source_relative_path:
        return discovered_relative_path, True
    return None, False


def _load_preserved_tags(
    latest_exported_summary,
    *,
    obsidian_vault_path: str | None,
) -> list[str]:
    """Load user-managed tags from the current latest exported note when available."""
    if not latest_exported_summary or not obsidian_vault_path:
        return []
    resolved_relative_path, _ = _resolve_exported_note_path(
        latest_exported_summary,
        obsidian_vault_path=obsidian_vault_path,
    )
    if not resolved_relative_path:
        return []
    note_path = Path(obsidian_vault_path) / Path(resolved_relative_path)
    preserved_tags = extract_tags_from_frontmatter(note_path)
    return list(preserved_tags or [])


def _build_revision_history_entry(
    *,
    instruction: str,
    route: str,
    used_transcript_context: bool,
    transcript_version_id: str | None,
    transcript_version_number: int | None,
    evidence_window_count: int,
    evidence_segment_count: int,
    changed: bool,
    backend: str | None,
    model: str | None,
    template_key: str | None = None,
    template_guidance_used: bool = True,
    structure_valid: bool = True,
    structure_repair_applied: bool = False,
    structure_notes: str | None = None,
    source: str = "workspace_summary",
) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "created_at": to_utc_iso(datetime.now(timezone.utc).replace(tzinfo=None)),
        "kind": "ai_revise",
        "source": source,
        "instruction": instruction.strip(),
        "route": route,
        "used_transcript_context": bool(used_transcript_context),
        "transcript_version_id": transcript_version_id,
        "transcript_version_number": transcript_version_number,
        "evidence_window_count": max(0, int(evidence_window_count or 0)),
        "evidence_segment_count": max(0, int(evidence_segment_count or 0)),
        "changed": bool(changed),
        "backend": backend,
        "model": model,
        "template_key": template_key,
        "template_guidance_used": bool(template_guidance_used),
        "structure_valid": bool(structure_valid),
        "structure_repair_applied": bool(structure_repair_applied),
        "structure_notes": structure_notes,
    }


def _normalize_refine_result(
    result: object,
    *,
    current_summary: str,
    route: str,
) -> dict[str, object]:
    if isinstance(result, dict):
        normalized = dict(result)
        normalized.setdefault("revised_summary", current_summary)
        normalized.setdefault("changed", normalized.get("revised_summary") != current_summary)
        normalized.setdefault("route", route)
        normalized.setdefault("used_transcript_context", False)
        normalized.setdefault("evidence_window_count", 0)
        normalized.setdefault("structure_valid", True)
        normalized.setdefault("template_guidance_used", False)
        normalized.setdefault("structure_repair_applied", False)
        return normalized
    revised_summary = str(result or "").strip() or current_summary
    return {
        "revised_summary": revised_summary,
        "changed": revised_summary != current_summary,
        "route": route,
        "used_transcript_context": False,
        "evidence_window_count": 0,
        "structure_valid": True,
        "template_guidance_used": False,
        "structure_repair_applied": False,
    }


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
    template: str = DEFAULT_TEMPLATE_KEY
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
    transcript_version_id: Optional[str] = None
    preserve_existing_draft: bool = False


class StartTranscriptionJobRequest(BaseModel):
    mode: str = "initial"
    source_transcript_version_id: Optional[str] = None
    expected_speaker_count: Optional[int] = None
    late_join_offset_seconds: Optional[float] = None
    repair_reason: Optional[str] = None


class StartSummaryJobRequest(BaseModel):
    transcript_version_id: Optional[str] = None


class TranscriptionJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str
    transcript_version_id: Optional[str] = None
    transcript_version_number: Optional[int] = None


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
    transcript_version_id: Optional[str] = None
    transcript_version_number: Optional[int] = None


class SummaryJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str
    transcript_version_id: Optional[str] = None


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
    transcript_version_id: Optional[str] = None


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


def _speaker_review_blocks_summary(meeting) -> bool:
    return False


async def _update_transcript_version_speaker_review_state(
    repository: Repository,
    transcript_version_id: str,
    segments: list,
) -> None:
    await repository.update_transcript_version(
        transcript_version_id,
        **speaker_review_update_fields(segments),
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


def _create_transcription_job(
    session_id: str,
    *,
    transcript_version_id: str | None = None,
    transcript_version_number: int | None = None,
) -> dict:
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
        "transcript_version_id": transcript_version_id,
        "transcript_version_number": transcript_version_number,
        "created_at": now,
        "updated_at": now,
    }
    _TRANSCRIPTION_JOBS[job_id] = payload
    return payload


def _find_active_transcription_job(session_id: str) -> dict | None:
    active_jobs = [
        job
        for job in _TRANSCRIPTION_JOBS.values()
        if job.get("session_id") == session_id
        and str(job.get("status")) in {"queued", "running"}
    ]
    if not active_jobs:
        return None
    return max(active_jobs, key=lambda job: str(job.get("updated_at", "")))


def _create_summary_job(session_id: str, *, transcript_version_id: str | None = None) -> dict:
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
        "transcript_version_id": transcript_version_id,
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
            or speaker_identity(segment)
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
            speaker_identity(segment)
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
    transcript_version_id: str,
    progress_callback: Optional[TranscriptionProgressCallback] = None,
    expected_speaker_count: int | None = None,
    late_join_offset_seconds: float | None = None,
    repair_reason: str | None = None,
) -> tuple[str, float, dict[str, object]]:
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
            progress_callback("transcribing", message, progress)

    try:
        transcription_result = await transcription_manager.transcribe_file(
            audio_path,
            progress_callback=on_transcription_progress,
            expected_speaker_count=expected_speaker_count,
            late_join_offset_seconds=late_join_offset_seconds,
            repair_reason=repair_reason,
        )
    except Exception as e:
        message = str(e)
        if message.startswith("Speaker detection could not confidently separate"):
            raise HTTPException(status_code=422, detail=message)
        raise HTTPException(status_code=500, detail=f"Failed to transcribe recording audio: {message}")

    full_text = transcription_result.text.strip()
    if not full_text:
        raise HTTPException(status_code=400, detail="No speech detected in recording audio")

    try:
        list_profiles = getattr(repository, "list_speaker_profiles", None)
        profiles = await list_profiles() if callable(list_profiles) else []
        if profiles and transcription_result.segments:
            profile_matches = match_segments_to_profiles(
                audio_path=str(audio_path),
                segments=transcription_result.segments,
                hf_token=get_settings().hf_token,
                profiles=profiles,
            )
            if profile_matches:
                apply_profile_matches_to_segments(
                    segments=transcription_result.segments,
                    matches=profile_matches,
                )
    except Exception as exc:
        logger.warning("Speaker profile matching skipped during transcription: %s", exc)

    segment_count = 0
    with pipeline_step(logger, "segment_building", segments=len(transcription_result.segments)) as step:
        await repository.delete_segments_for_transcript_version(transcript_version_id)
        for segment in transcription_result.segments:
            text = str(segment.text).strip()
            start = float(segment.start)
            end = float(segment.end)
            if not text or end <= start:
                continue

            await repository.add_segment(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                text=text,
                start_time=start,
                end_time=end,
                confidence=transcription_result.confidence,
                speaker=segment.speaker,
                speaker_cluster=segment.speaker_cluster,
                transcript_version_id=transcript_version_id,
            )
            segment_count += 1

        if segment_count == 0:
            raise HTTPException(status_code=500, detail="Failed to build transcript segments from recording audio")

        await repository.set_session_has_transcription(session_id, True)
        await repository.reindex_session_transcript_search(session_id)
        step["segments"] = segment_count

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
    )
    if not segments:
        raise HTTPException(status_code=500, detail="Failed to build transcript segments from recording audio")
    await _update_transcript_version_speaker_review_state(
        repository,
        transcript_version_id,
        segments,
    )

    transcript, _ = _segments_to_transcript(segments)

    if progress_callback:
        maybe_awaitable = progress_callback("transcribing", "Transcription complete", 1.0)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    return transcript, transcription_result.duration_seconds, {
        "diarization_backend": transcription_result.diarization_backend,
        "diarization_model": transcription_result.diarization_model,
        "repair_strategy": transcription_result.repair_strategy,
        "diarization_actual_speaker_count": transcription_result.diarization_actual_speaker_count,
        "diarization_unassigned_segment_count": transcription_result.diarization_unassigned_segment_count,
        "diarization_unassigned_segment_ratio": transcription_result.diarization_unassigned_segment_ratio,
        "repair_quality_gate_passed": transcription_result.repair_quality_gate_passed,
    }


def _normalize_transcription_persist_result(result) -> tuple[str, float, dict[str, object]]:
    if isinstance(result, tuple):
        if len(result) == 3:
            transcript, duration_seconds, metadata = result
            return str(transcript), float(duration_seconds), dict(metadata or {})
        if len(result) == 2:
            transcript, duration_seconds = result
            return str(transcript), float(duration_seconds), {
                "diarization_backend": None,
                "diarization_model": None,
                "repair_strategy": None,
                "diarization_actual_speaker_count": None,
                "diarization_unassigned_segment_count": None,
                "diarization_unassigned_segment_ratio": None,
                "repair_quality_gate_passed": None,
            }
    raise ValueError("Unexpected transcription persist result shape")


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
    try:
        filepath, obsidian_uri = write_obsidian_markdown_atomic(
            markdown_content,
            relative_path,
            obsidian_vault_path,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")
    return filepath, obsidian_uri


async def _archive_previous_export_if_needed(
    repository: Repository,
    *,
    summary_to_archive,
    archive_relative_path: str | None,
    obsidian_vault_path: str,
) -> None:
    """Copy the previous latest export into the hidden versions tree and update its DB path."""
    if not summary_to_archive or not archive_relative_path:
        return
    source_relative_path, was_rediscovered = _resolve_exported_note_path(
        summary_to_archive,
        obsidian_vault_path=obsidian_vault_path,
    )
    if not source_relative_path:
        return
    if was_rediscovered:
        await repository.update_summary(
            summary_to_archive.id,
            obsidian_relative_path=source_relative_path,
        )
        return
    vault_root = Path(obsidian_vault_path)
    source_path = vault_root / Path(source_relative_path)
    if not source_path.exists():
        return
    try:
        copied = copy_obsidian_markdown_with_frontmatter_updates(
            obsidian_vault_path=obsidian_vault_path,
            source_relative_path=source_relative_path,
            destination_relative_path=archive_relative_path,
            frontmatter_updates={"sidekick_export_status": "archived"},
            remove_frontmatter_keys=("sidekick_is_latest_export",),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to archive previous export: {exc}")
    if copied:
        await repository.update_summary(
            summary_to_archive.id,
            obsidian_relative_path=archive_relative_path,
        )


async def _persist_latest_summary_export(
    repository: Repository,
    *,
    summary_id: str,
    markdown_content: str,
    latest_relative_path: str,
    obsidian_vault_path: str,
    previous_latest_summary=None,
    previous_archive_relative_path: str | None = None,
    saved_at: datetime | None = None,
) -> tuple[str, str]:
    """Archive the previous latest export, write the new latest note, and persist DB paths."""
    await _archive_previous_export_if_needed(
        repository,
        summary_to_archive=previous_latest_summary,
        archive_relative_path=previous_archive_relative_path,
        obsidian_vault_path=obsidian_vault_path,
    )
    filepath, obsidian_uri = await _write_obsidian_file(
        markdown_content,
        latest_relative_path,
        obsidian_vault_path,
    )
    await repository.update_summary(
        summary_id,
        saved_to_obsidian_at=saved_at or datetime.utcnow(),
        obsidian_relative_path=latest_relative_path,
    )
    return filepath, obsidian_uri


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
            template_key=normalize_template_key(request_payload.template),
            custom_prompt=request_payload.custom_prompt,
        )
    else:
        primary_meeting = await repository.create_meeting(session_id=session.id, title=request_payload.title)
        primary_meeting_id = primary_meeting.id
        await repository.update_meeting_settings(
            primary_meeting_id,
            template_key=normalize_template_key(request_payload.template),
            custom_prompt=request_payload.custom_prompt,
        )

    # Reuse existing transcript when authoritative transcription exists and segments are present.
    existing_segments = await repository.get_segments(session_id=session_id) if session.has_transcription else []
    if session.has_transcription and existing_segments:
        await _emit_progress(progress_callback, "transcribing", "Reusing existing transcript", 0.2, 0.0)
        logger.info("[step] transcription | start | reuse=True | segments=%d", len(existing_segments))
        _tx_t0 = time.monotonic()

        full_transcript, audio_duration_seconds = _build_transcript_from_segments(existing_segments)
        logger.info("[step] transcription | done | elapsed=%.1fs | chars=%d", time.monotonic() - _tx_t0, len(full_transcript))
        await _emit_progress(progress_callback, "transcribing", "Transcription complete", 1.0, 0.0)
    else:
        await repository.ensure_transcript_versions(session_id)
        transcript_version = await repository.get_latest_transcript_version(session_id, include_processing=True)
        if transcript_version is None:
            transcript_version = await repository.create_transcript_version(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                version_number=1,
                status="processing",
                source_type="initial_transcription",
                template_key=request_payload.template,
                custom_prompt=request_payload.custom_prompt,
            )
        with pipeline_step(logger, "transcription") as step:
            full_transcript, audio_duration_seconds, transcription_meta = _normalize_transcription_persist_result(
                await _transcribe_and_persist_session(
                    session_id=session_id,
                    session=session,
                    repository=repository,
                    transcription_manager=transcription_manager,
                    primary_meeting_id=primary_meeting_id,
                    transcript_version_id=str(transcript_version.id),
                    progress_callback=(
                        lambda stage, message, progress: (
                            progress_callback(stage, message, progress, 0.0) if progress_callback else None
                        )
                    ),
                )
            )
            await repository.update_transcript_version(
                str(transcript_version.id),
                status="ready",
                transcription_backend=str(get_settings().transcription_backend),
                transcription_model=getattr(transcription_manager.active_engine, "name", None),
                diarization_backend=transcription_meta.get("diarization_backend"),
                diarization_model=transcription_meta.get("diarization_model"),
            )
            step["chars"] = len(full_transcript)

    # Unload Whisper to free VRAM for summarization model
    await transcription_manager.unload()
    logger.info("[step] transcription | unloaded model to free VRAM")

    # Generate summary
    template = normalize_template_key(request_payload.template)

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
        workflow_data_json=json.dumps(summary_result.workflow_data) if summary_result.workflow_data else None,
    )

    summary_content = summary_result.content
    processing_time_str = format_processing_time(summarization_duration)

    local_started_at = localize_datetime(
        session.started_at,
        session.timezone_name,
        session.timezone_offset_minutes,
    )
    tz_label = timezone_label(session.timezone_name, session.timezone_offset_minutes)
    existing_summaries = await repository.get_summaries(primary_meeting_id, status="saved")
    version_map = _saved_summary_version_map(existing_summaries)
    latest_exported_summary = _latest_exported_summary(existing_summaries)
    target = resolve_latest_export_target(
        vault_path=settings.obsidian_vault_path,
        meeting_id=primary_meeting_id,
        title=request_payload.title,
        local_started_at=local_started_at,
        preferred_relative_path=(
            latest_exported_summary.obsidian_relative_path
            if latest_exported_summary
            else None
        ),
    )
    summary_version_number = len(existing_summaries) + 1
    previous_archive_relative_path = None
    if latest_exported_summary:
        archived_version_number = version_map.get(str(latest_exported_summary.id))
        if archived_version_number:
            previous_archive_relative_path = build_archive_relative_path(
                target,
                archived_version_number,
            )
    preserved_tags = _load_preserved_tags(
        latest_exported_summary,
        obsidian_vault_path=settings.obsidian_vault_path,
    )

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
        "processing_duration_seconds": summarization_duration,
        "filename": target.filename,
        "relative_path": target.relative_path,
        "draft_summary_id": draft_summary.id,
        "meeting_id": primary_meeting_id,
        "transcript_version_id": str(transcript_version.id),
        "template_key": template,
        "custom_prompt": request_payload.custom_prompt,
        "meeting_display_id": target.display_id,
        "summary_version_number": summary_version_number,
        "transcript_version_number": int(transcript_version.version_number),
        "frontmatter": _build_obsidian_frontmatter(
            meeting_id=str(primary_meeting_id),
            summary_id=None,
            transcript_version_id=str(transcript_version.id),
            display_id=target.display_id,
            summary_version_number=summary_version_number,
            transcript_version_number=int(transcript_version.version_number),
            meeting_title=(request_payload.title or "Untitled Recording").strip() or "Untitled Recording",
            local_started_at=local_started_at,
            target=target,
            template_key=template,
            recording_duration_minutes=max(1, int(audio_duration_seconds // 60)),
            tags=preserved_tags,
        ),
        "latest_exported_summary": latest_exported_summary,
        "previous_archive_relative_path": previous_archive_relative_path,
        "pass1_system_prompt": summary_result.prompt_audit.get("pass1_system_prompt"),
        "pass1_user_prompt": summary_result.prompt_audit.get("pass1_user_prompt"),
        "pass2_system_prompt": summary_result.prompt_audit.get("pass2_system_prompt"),
        "pass2_user_prompt": summary_result.prompt_audit.get("pass2_user_prompt"),
        "revision_history": [],
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
    mode: str = "initial",
    source_transcript_version_id: str | None = None,
    expected_speaker_count: int | None = None,
    late_join_offset_seconds: float | None = None,
    repair_reason: str | None = None,
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
            primary_meeting = meetings[0]
        else:
            primary_meeting = await repository.create_meeting(
                session_id=session.id,
                title="Untitled Recording",
            )
        primary_meeting_id = primary_meeting.id

        await repository.ensure_transcript_versions(session_id)
        latest_version = await repository.get_latest_transcript_version(
            session_id,
            include_processing=True,
        )
        source_version = None
        if source_transcript_version_id:
            source_version = await repository.get_transcript_version_for_session(
                session_id,
                source_transcript_version_id,
            )
        if source_version is None:
            source_version = latest_version

        created_version_id: str | None = None
        if mode == "retranscribe":
            next_version_number = (latest_version.version_number if latest_version else 0) + 1
            created_version = await repository.create_transcript_version(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                version_number=next_version_number,
                parent_version_id=source_version.id if source_version else None,
                status="processing",
                source_type="retranscription_repair" if repair_reason else "retranscription",
                template_key=normalize_template_key(
                    source_version.template_key if source_version and source_version.template_key else DEFAULT_TEMPLATE_KEY
                ),
                custom_prompt=source_version.custom_prompt if source_version else None,
                diarization_expected_speaker_count=expected_speaker_count,
                diarization_late_join_offset_seconds=late_join_offset_seconds,
                diarization_repair_source_version_id=source_version.id if source_version else None,
                repair_strategy=None,
                repair_reason=repair_reason,
            )
            transcript_version = created_version
            created_version_id = created_version.id
        elif latest_version:
            transcript_version = latest_version
            if transcript_version.status != "processing":
                transcript_version = await repository.update_transcript_version(
                    latest_version.id,
                    status="processing",
                )
        else:
            transcript_version = await repository.create_transcript_version(
                session_id=session_id,
                meeting_id=primary_meeting_id,
                version_number=1,
                status="processing",
                source_type="initial_transcription",
                template_key=normalize_template_key(
                    primary_meeting.template_key or DEFAULT_TEMPLATE_KEY
                ),
                custom_prompt=primary_meeting.custom_prompt,
            )
            created_version_id = transcript_version.id

        _update_transcription_job(
            job_id,
            transcript_version_id=str(transcript_version.id),
            transcript_version_number=int(transcript_version.version_number),
        )

        _, _, transcription_meta = _normalize_transcription_persist_result(
            await _transcribe_and_persist_session(
                session_id=session_id,
                session=session,
                repository=repository,
                transcription_manager=transcription_manager,
                primary_meeting_id=primary_meeting_id,
                transcript_version_id=str(transcript_version.id),
                progress_callback=update_progress,
                expected_speaker_count=expected_speaker_count,
                late_join_offset_seconds=late_join_offset_seconds,
                repair_reason=repair_reason,
            )
        )

        await repository.update_transcript_version(
            str(transcript_version.id),
            status="ready",
            transcription_backend=str(get_settings().transcription_backend),
            transcription_model=getattr(transcription_manager.active_engine, "name", None),
            diarization_backend=transcription_meta.get("diarization_backend"),
            diarization_model=transcription_meta.get("diarization_model"),
            repair_strategy=transcription_meta.get("repair_strategy"),
            diarization_actual_speaker_count=transcription_meta.get("diarization_actual_speaker_count"),
            diarization_unassigned_segment_count=transcription_meta.get("diarization_unassigned_segment_count"),
            diarization_unassigned_segment_ratio=transcription_meta.get("diarization_unassigned_segment_ratio"),
            repair_quality_gate_passed=transcription_meta.get("repair_quality_gate_passed"),
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
        if 'created_version_id' in locals() and created_version_id:
            await repository.update_transcript_version(created_version_id, status="failed")
        _update_transcription_job(
            job_id,
            status="failed",
            stage="failed",
            message="Transcription failed",
            error=str(exc.detail),
        )
    except Exception as exc:
        if 'created_version_id' in locals() and created_version_id:
            await repository.update_transcript_version(created_version_id, status="failed")
        logger.exception(
            "transcription job failed",
            extra={
                "job_id": job_id,
                "session_id": session_id,
                "mode": mode,
                "source_transcript_version_id": source_transcript_version_id,
            },
        )
        _update_transcription_job(
            job_id,
            status="failed",
            stage="failed",
            message="Transcription failed",
            error=str(exc),
        )
    finally:
        try:
            await transcription_manager.unload()
            logger.info(
                "[step] transcription_cleanup | done | job_id=%s | session_id=%s",
                job_id,
                session_id,
            )
        except Exception:
            logger.warning(
                "[step] transcription_cleanup | failed | job_id=%s | session_id=%s",
                job_id,
                session_id,
                exc_info=True,
            )


async def _build_summary_save_params(
    repository: Repository,
    session,
    meeting,
    *,
    transcript_version_id: str | None,
    summary_id: str | None = None,
    summary_content: str,
    template_label: str,
    processing_duration_seconds: float | None,
    pass1_system_prompt: str | None = None,
    pass1_user_prompt: str | None = None,
    pass2_system_prompt: str | None = None,
    pass2_user_prompt: str | None = None,
    workflow_data_json: str | None = None,
) -> dict:
    """Assemble markdown and path metadata for saving a summary draft."""
    segments = await repository.get_segments(
        session_id=session.id,
        transcript_version_id=transcript_version_id,
    )
    transcript, audio_duration_seconds = _segments_to_transcript(segments)

    local_started_at = localize_datetime(
        session.started_at,
        session.timezone_name,
        session.timezone_offset_minutes,
    )
    tz_label = timezone_label(session.timezone_name, session.timezone_offset_minutes)
    settings = get_settings()
    existing_summaries = await repository.get_summaries(
        meeting.id,
        status="saved",
    )
    version_map = _saved_summary_version_map(existing_summaries)
    latest_exported_summary = _latest_exported_summary(existing_summaries)
    next_version_number = len(existing_summaries) + 1
    preferred_relative_path = (
        latest_exported_summary.obsidian_relative_path
        if latest_exported_summary
        else None
    )
    target = resolve_latest_export_target(
        vault_path=settings.obsidian_vault_path,
        meeting_id=meeting.id,
        title=meeting.title,
        local_started_at=local_started_at,
        preferred_relative_path=preferred_relative_path,
    )
    previous_archive_relative_path = None
    if latest_exported_summary:
        archived_version_number = version_map.get(str(latest_exported_summary.id))
        if archived_version_number:
            previous_archive_relative_path = build_archive_relative_path(
                target,
                archived_version_number,
            )
    preserved_tags = _load_preserved_tags(
        latest_exported_summary,
        obsidian_vault_path=settings.obsidian_vault_path,
    )
    recorded_at = format_datetime_human(local_started_at, tz_label)
    processing_time_str = (
        format_processing_time(processing_duration_seconds)
        if processing_duration_seconds
        else ""
    )
    duration_str = format_duration_human(int(audio_duration_seconds))
    transcript_version = None
    if transcript_version_id:
        transcript_version = await repository.get_transcript_version_for_session(
            session.id,
            transcript_version_id,
        )
    transcript_version_number = (
        int(getattr(transcript_version, "version_number", 0)) or None
    )
    frontmatter = _build_obsidian_frontmatter(
        meeting_id=str(meeting.id),
        summary_id=summary_id,
        transcript_version_id=transcript_version_id,
        display_id=target.display_id,
        summary_version_number=next_version_number,
        transcript_version_number=transcript_version_number,
        meeting_title=(meeting.title or "Untitled Recording").strip() or "Untitled Recording",
        local_started_at=local_started_at,
        target=target,
        template_key=getattr(meeting, "template_key", None),
        recording_duration_minutes=max(1, int(audio_duration_seconds // 60)),
        tags=preserved_tags,
    )
    workflow_data = _safe_json_loads(workflow_data_json, {})
    revision_history = workflow_data.get("revision_history", []) if isinstance(workflow_data, dict) else []
    if not isinstance(revision_history, list):
        revision_history = []

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
        "filename": target.filename,
        "relative_path": target.relative_path,
        "meeting_display_id": target.display_id,
        "summary_version_number": next_version_number,
        "transcript_version_number": transcript_version_number,
        "frontmatter": frontmatter,
        "latest_exported_summary": latest_exported_summary,
        "previous_archive_relative_path": previous_archive_relative_path,
        "pass1_system_prompt": pass1_system_prompt,
        "pass1_user_prompt": pass1_user_prompt,
        "pass2_system_prompt": pass2_system_prompt,
        "pass2_user_prompt": pass2_user_prompt,
        "revision_history": revision_history,
    }


async def _run_summary_job(
    job_id: str,
    session_id: str,
    summarization_manager: SummarizationManager,
    repository: Repository,
    transcript_version_id: str | None = None,
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

        await repository.ensure_transcript_versions(session_id)
        transcript_version = None
        if transcript_version_id:
            transcript_version = await repository.get_transcript_version_for_session(
                session_id,
                transcript_version_id,
            )
        if transcript_version is None:
            transcript_version = await repository.get_latest_transcript_version(session_id)
        if not transcript_version:
            raise HTTPException(status_code=409, detail="Transcription must complete before generating a summary")

        segments = await repository.get_segments(
            session_id=session_id,
            transcript_version_id=str(transcript_version.id),
        )
        if not session.has_transcription or not segments:
            raise HTTPException(
                status_code=409,
                detail="Transcription must complete before generating a summary",
            )
        transcript, _ = _build_transcript_from_segments(segments)
        template_key = normalize_template_key(transcript_version.template_key or DEFAULT_TEMPLATE_KEY)
        await _emit_progress(update_progress, "summarizing", "Generating summary", 1.0, 0.02)
        started_at = time.monotonic()

        def on_progress(progress: float) -> None:
            update_progress("summarizing", "Generating summary", 1.0, min(progress, 0.99))

        summary_result = await summarization_manager.summarize(
            transcript=transcript,
            prompt_type=template_key,
            custom_instructions=transcript_version.custom_prompt,
            progress_callback=on_progress,
        )
        processing_duration = time.monotonic() - started_at
        template_label = TEMPLATE_INFO.get(template_key, {}).get("name", template_key.title())
        latest_saved = await repository.get_latest_summary(
            meeting.id,
            status="saved",
            transcript_version_id=str(transcript_version.id),
        )
        draft = await repository.replace_draft_summary(
            meeting_id=meeting.id,
            transcript_version_id=str(transcript_version.id),
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
            custom_prompt=transcript_version.custom_prompt,
            pass1_system_prompt=summary_result.prompt_audit.get("pass1_system_prompt"),
            pass1_user_prompt=summary_result.prompt_audit.get("pass1_user_prompt"),
            pass2_system_prompt=summary_result.prompt_audit.get("pass2_system_prompt"),
            pass2_user_prompt=summary_result.prompt_audit.get("pass2_user_prompt"),
            attendees_snapshot=None,
            workflow_data_json=json.dumps(summary_result.workflow_data) if summary_result.workflow_data else None,
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
            transcript_version_id=str(transcript_version.id),
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
    frontmatter = dict(bp.get("frontmatter") or {})
    frontmatter["exported_at"] = local_exported_at.isoformat()
    markdown_content = build_obsidian_markdown(
        content=bp["summary_content"],
        template_label=bp["template_label"],
        recorded_at=bp["recorded_at"],
        exported_at=exported_at,
        duration_str=bp["duration_str"],
        processing_time_str=bp["processing_time_str"],
        transcript=bp["transcript"],
        meeting_display_id=bp.get("meeting_display_id"),
        summary_version_number=bp.get("summary_version_number"),
        transcript_version_number=bp.get("transcript_version_number"),
        frontmatter=frontmatter,
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
    request: StartTranscriptionJobRequest | None = None,
    repository: Repository = Depends(get_repository),
    transcription_manager: TranscriptionManager = Depends(get_transcription_manager),
):
    """Run authoritative transcription only (no summary/export)."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    active_job = _find_active_transcription_job(session_id)
    if active_job:
        job_id = str(active_job["job_id"])
        return TranscriptionJobCreateResponse(
            job_id=job_id,
            status=str(active_job.get("status", "queued")),
            poll_url=f"/api/transcription-jobs/{job_id}",
            transcript_version_id=active_job.get("transcript_version_id"),
            transcript_version_number=active_job.get("transcript_version_number"),
        )

    request = request or StartTranscriptionJobRequest()
    normalized_mode = str(request.mode or "initial").strip().lower()
    if normalized_mode not in {"initial", "retranscribe"}:
        raise HTTPException(status_code=422, detail="Unsupported transcription mode")

    if normalized_mode != "retranscribe":
        if request.expected_speaker_count is not None or request.late_join_offset_seconds is not None or request.repair_reason:
            raise HTTPException(status_code=422, detail="Repair options are only supported for retranscribe mode")
    else:
        if request.repair_reason and request.expected_speaker_count is None:
            raise HTTPException(status_code=422, detail="expected_speaker_count is required for repair runs")
        if request.expected_speaker_count is not None:
            if int(request.expected_speaker_count) < 2 or int(request.expected_speaker_count) > 10:
                raise HTTPException(status_code=422, detail="expected_speaker_count must be between 2 and 10")
        if request.repair_reason is not None:
            normalized_reason = str(request.repair_reason).strip().lower()
            if normalized_reason not in {"missing_speaker", "split_speaker"}:
                raise HTTPException(status_code=422, detail="Unsupported repair reason")
            request.repair_reason = normalized_reason
        if request.late_join_offset_seconds is not None and float(request.late_join_offset_seconds) < 0:
            raise HTTPException(status_code=422, detail="late_join_offset_seconds must be non-negative")
        if request.late_join_offset_seconds is not None:
            duration_seconds = None
            audio_path = get_session_audio_path(session_id)
            if not audio_path and session.ended_at:
                audio_path = ensure_session_audio_path(session_id)
            if audio_path:
                try:
                    duration_seconds = media_duration_seconds(audio_path)
                except Exception as exc:
                    logger.warning("Unable to read audio duration for speaker repair validation: %s", exc)
            if duration_seconds is None and session.ended_at:
                duration_seconds = max(0.0, float((session.ended_at - session.started_at).total_seconds()))
            if duration_seconds is not None and float(request.late_join_offset_seconds) > duration_seconds:
                raise HTTPException(status_code=422, detail="late_join_offset_seconds exceeds recording duration")

    job = _create_transcription_job(session_id)
    job_id = str(job["job_id"])
    _update_transcription_job(job_id, message="Starting transcription")

    task = asyncio.create_task(
        _run_transcription_job(
            job_id=job_id,
            session_id=session_id,
            repository=repository,
            transcription_manager=transcription_manager,
            mode=normalized_mode,
            source_transcript_version_id=request.source_transcript_version_id,
            expected_speaker_count=request.expected_speaker_count,
            late_join_offset_seconds=request.late_join_offset_seconds,
            repair_reason=request.repair_reason,
        )
    )
    _TRANSCRIPTION_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _TRANSCRIPTION_TASKS.pop(job_id, None))

    return TranscriptionJobCreateResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/api/transcription-jobs/{job_id}",
        transcript_version_id=job.get("transcript_version_id"),
        transcript_version_number=job.get("transcript_version_number"),
    )


@router.post("/recordings/{session_id}/summary-job", response_model=SummaryJobCreateResponse)
async def start_summary_job(
    session_id: str,
    request: StartSummaryJobRequest | None = None,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Generate a draft summary from the current recording workspace settings."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    request = request or StartSummaryJobRequest()
    job = _create_summary_job(
        session_id,
        transcript_version_id=request.transcript_version_id,
    )
    job_id = str(job["job_id"])
    _update_summary_job(job_id, message="Starting summary generation")

    task = asyncio.create_task(
        _run_summary_job(
            job_id=job_id,
            session_id=session_id,
            summarization_manager=summarization_manager,
            repository=repository,
            transcript_version_id=request.transcript_version_id,
        )
    )
    _SUMMARY_TASKS[job_id] = task
    task.add_done_callback(lambda _t: _SUMMARY_TASKS.pop(job_id, None))

    return SummaryJobCreateResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/api/summary-jobs/{job_id}",
        transcript_version_id=request.transcript_version_id,
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

    settings = get_settings()
    draft_id = bp.get("draft_summary_id")
    meeting = await repository.get_meeting(bp["meeting_id"])
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    session = await repository.get_session(meeting.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    pending_summary_id = str(uuid.uuid4()) if draft_id else None
    save_params = await _build_summary_save_params(
        repository,
        session,
        meeting,
        transcript_version_id=bp.get("transcript_version_id"),
        summary_id=pending_summary_id,
        summary_content=summary,
        template_label=bp["template_label"],
        processing_duration_seconds=bp.get("processing_duration_seconds"),
        pass1_system_prompt=bp.get("pass1_system_prompt"),
        pass1_user_prompt=bp.get("pass1_user_prompt"),
        pass2_system_prompt=bp.get("pass2_system_prompt"),
        pass2_user_prompt=bp.get("pass2_user_prompt"),
    )
    save_params["revision_history"] = bp.get("revision_history") or []
    frontmatter = dict(save_params.get("frontmatter") or {})
    frontmatter["exported_at"] = local_exported_at.isoformat()

    markdown_content = build_obsidian_markdown(
        content=summary,
        template_label=save_params["template_label"],
        recorded_at=save_params["recorded_at"],
        exported_at=exported_at,
        duration_str=save_params["duration_str"],
        processing_time_str=save_params["processing_time_str"],
        transcript=save_params["transcript"],
        meeting_display_id=save_params.get("meeting_display_id"),
        summary_version_number=save_params.get("summary_version_number"),
        transcript_version_number=save_params.get("transcript_version_number"),
        frontmatter=frontmatter,
        pass1_system_prompt=save_params.get("pass1_system_prompt"),
        pass1_user_prompt=save_params.get("pass1_user_prompt"),
        pass2_system_prompt=save_params.get("pass2_system_prompt"),
        pass2_user_prompt=save_params.get("pass2_user_prompt"),
        revision_history=save_params.get("revision_history"),
        revision_instruction=request.revision_instruction,
    )

    saved_at = datetime.utcnow()
    filepath = ""
    obsidian_uri = None
    if settings.obsidian_vault_path:
        with pipeline_step(logger, "vault_write", path=save_params["relative_path"]):
            await _archive_previous_export_if_needed(
                repository,
                summary_to_archive=save_params.get("latest_exported_summary"),
                archive_relative_path=save_params.get("previous_archive_relative_path"),
                obsidian_vault_path=settings.obsidian_vault_path,
            )
            filepath, obsidian_uri = await _write_obsidian_file(
                markdown_content,
                save_params["relative_path"],
                settings.obsidian_vault_path,
            )
    if draft_id:
        if request.edited_summary and request.edited_summary.strip():
            source_type = "ai_revised" if request.revision_instruction else "manual_edit"
            await repository.update_summary(
                draft_id,
                content=summary,
                source_type=source_type,
            )
        saved_summary = await repository.save_draft_summary(
            draft_id,
            summary_id=pending_summary_id,
            saved_to_obsidian_at=saved_at,
            obsidian_relative_path=save_params["relative_path"] if settings.obsidian_vault_path else None,
        )

    _update_export_job(job_id, status="completed", stage="completed", message="Saved to Obsidian")

    return {
        "success": True,
        "filename": save_params["filename"],
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
    bp = job.get("build_params") or {}

    try:
        revised_result = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=request.current_summary,
            template_key=bp.get("template_key") or DEFAULT_TEMPLATE_KEY,
            custom_prompt=bp.get("custom_prompt"),
            transcript=job.get("build_params", {}).get("transcript"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(e)}")
    revised = _normalize_refine_result(
        revised_result,
        current_summary=request.current_summary,
        route=classify_revision_route(request.instruction),
    )

    return {
        "revised_summary": revised.get("revised_summary", request.current_summary),
        "changed": bool(revised.get("changed")),
        "route": revised.get("route"),
        "used_transcript_context": bool(revised.get("used_transcript_context")),
        "evidence_window_count": int(revised.get("evidence_window_count") or 0),
    }


@router.post("/summaries/refine")
async def refine_summary(
    request: RefineRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Apply a single AI revision pass to any provided summary text."""
    try:
        revised_result = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=request.current_summary,
            template_key=DEFAULT_TEMPLATE_KEY,
            custom_prompt=None,
        )
        revised = _normalize_refine_result(
            revised_result,
            current_summary=request.current_summary,
            route=classify_revision_route(request.instruction),
        )
        return {
            "revised_summary": revised.get("revised_summary", request.current_summary),
            "changed": bool(revised.get("changed")),
            "route": revised.get("route"),
            "used_transcript_context": bool(revised.get("used_transcript_context")),
            "evidence_window_count": int(revised.get("evidence_window_count") or 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(e)}")


@router.post("/recordings/{session_id}/summary-draft")
async def create_summary_draft(
    session_id: str,
    request: CreateDraftRequest,
    repository: Repository = Depends(get_repository),
):
    """Create an editable draft from the selected summary when needed."""
    meeting = await repository.get_primary_meeting(session_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Recording not found")

    transcript_version_id = request.transcript_version_id
    if transcript_version_id:
        version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    else:
        version = await repository.get_latest_transcript_version(session_id)

    existing_draft = await repository.get_draft_summary(
        meeting.id,
        transcript_version_id=str(version.id) if version else None,
    )
    if existing_draft and (
        not request.source_summary_id or str(existing_draft.id) == str(request.source_summary_id)
    ):
        return {"draft_summary_id": existing_draft.id}

    source_summary_id = request.source_summary_id
    if not source_summary_id:
        latest_saved = await repository.get_latest_summary(
            meeting.id,
            status="saved",
            transcript_version_id=str(version.id) if version else None,
        )
        if not latest_saved:
            raise HTTPException(status_code=404, detail="No saved summary available to revise")
        source_summary_id = latest_saved.id

    source_summary = await repository.get_summary(source_summary_id)
    if not source_summary or str(source_summary.meeting_id) != str(meeting.id):
        raise HTTPException(status_code=404, detail="Selected summary version not found")
    if version and str(getattr(source_summary, "transcript_version_id", None) or "") != str(version.id):
        raise HTTPException(status_code=404, detail="Selected summary version does not match this transcript version")

    try:
        draft = await repository.branch_draft_from_summary(
            source_summary_id,
            source_type=request.source_type,
            preserve_existing_draft=bool(request.preserve_existing_draft),
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

    transcript = ""
    transcript_version_number: int | None = None
    evidence_segment_count = 0
    draft_transcript_version_id = getattr(draft, "transcript_version_id", None)
    if draft_transcript_version_id:
        version = await repository.get_transcript_version(draft_transcript_version_id)
        if version:
            transcript_version_number = int(getattr(version, "version_number", 0) or 0) or None
        segments = await repository.get_segments(
            session_id=None,
            transcript_version_id=draft_transcript_version_id,
        )
        transcript, _audio_duration_seconds = _segments_to_transcript(segments)
        evidence_segment_count = len(segments)

    route = classify_revision_route(request.instruction)
    draft_template_key = normalize_template_key(getattr(draft, "template_key", None) or DEFAULT_TEMPLATE_KEY)
    draft_custom_prompt = getattr(draft, "custom_prompt", None)
    transcript_windows = select_revision_evidence_windows(
        transcript=transcript,
        instruction=request.instruction,
        current_summary=draft.content,
        route=route,
    )
    if route == "evidence_needed" and not transcript_windows:
        raise HTTPException(
            status_code=400,
            detail="Revision needs transcript evidence, but I could not find enough relevant transcript context.",
        )

    try:
        revised_result = await summarization_manager.refine_summary(
            instruction=request.instruction,
            current_summary=draft.content,
            template_key=draft_template_key,
            custom_prompt=draft_custom_prompt,
            transcript=transcript,
            transcript_windows=transcript_windows,
            route=route,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Refinement failed: {str(exc)}")
    revised = _normalize_refine_result(
        revised_result,
        current_summary=draft.content,
        route=route,
    )
    if not bool(revised.get("structure_valid", True)):
        raise HTTPException(
            status_code=400,
            detail=str(
                revised.get("reason")
                or "Revision was not applied because it degraded the summary structure."
            ),
        )

    revision_entry = _build_revision_history_entry(
        instruction=request.instruction,
        route=str(revised.get("route") or route),
        used_transcript_context=bool(revised.get("used_transcript_context")),
        transcript_version_id=str(draft_transcript_version_id) if draft_transcript_version_id else None,
        transcript_version_number=transcript_version_number,
        evidence_window_count=int(revised.get("evidence_window_count") or len(transcript_windows)),
        evidence_segment_count=evidence_segment_count,
        changed=bool(revised.get("changed")),
        backend=str(revised.get("backend") or getattr(draft, "backend", "unknown")),
        model=str(revised.get("model") or getattr(draft, "model", "unknown")),
        template_key=draft_template_key,
        template_guidance_used=bool(revised.get("template_guidance_used", True)),
        structure_valid=bool(revised.get("structure_valid", True)),
        structure_repair_applied=bool(revised.get("structure_repair_applied", False)),
        structure_notes=str(revised.get("reason") or "") or None,
    )
    workflow_data = Repository.parse_summary_workflow_data(draft)
    revision_history = workflow_data.get("revision_history")
    if not isinstance(revision_history, list):
        revision_history = []
    revision_history.append(revision_entry)
    workflow_data["revision_history"] = revision_history

    updated = await repository.update_summary(
        summary_id,
        content=str(revised.get("revised_summary") or draft.content),
        source_type="ai_revised",
        workflow_data_json=json.dumps(workflow_data),
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Draft changed while AI revision was running. Reload the workspace and try again.",
        )
    return {
        "draft_summary_id": updated.id,
        "content": updated.content,
        "changed": bool(revised.get("changed")),
        "route": revised.get("route"),
        "used_transcript_context": bool(revised.get("used_transcript_context")),
        "evidence_window_count": int(revised.get("evidence_window_count") or 0),
        "structure_repair_applied": bool(revised.get("structure_repair_applied", False)),
    }


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
        transcript_version_id=draft.transcript_version_id,
        summary_id=str(uuid.uuid4()),
        summary_content=draft.content,
        template_label=draft.template or TEMPLATE_INFO.get(
            normalize_template_key(draft.template_key or meeting.template_key or DEFAULT_TEMPLATE_KEY),
            {},
        ).get("name", "Meeting"),
        processing_duration_seconds=draft.processing_duration_seconds,
        pass1_system_prompt=draft.pass1_system_prompt,
        pass1_user_prompt=draft.pass1_user_prompt,
        pass2_system_prompt=draft.pass2_system_prompt,
        pass2_user_prompt=draft.pass2_user_prompt,
        workflow_data_json=draft.workflow_data_json,
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
        frontmatter = dict(params.get("frontmatter") or {})
        frontmatter["exported_at"] = local_exported_at.isoformat()
        markdown_content = build_obsidian_markdown(
            content=params["summary_content"],
            template_label=params["template_label"],
            recorded_at=params["recorded_at"],
            exported_at=exported_at,
            duration_str=params["duration_str"],
            processing_time_str=params["processing_time_str"],
            transcript=params["transcript"],
            meeting_display_id=params.get("meeting_display_id"),
            summary_version_number=params.get("summary_version_number"),
            transcript_version_number=params.get("transcript_version_number"),
            frontmatter=frontmatter,
            pass1_system_prompt=params.get("pass1_system_prompt"),
            pass1_user_prompt=params.get("pass1_user_prompt"),
            pass2_system_prompt=params.get("pass2_system_prompt"),
            pass2_user_prompt=params.get("pass2_user_prompt"),
            revision_history=params.get("revision_history"),
        )
        await _archive_previous_export_if_needed(
            repository,
            summary_to_archive=params.get("latest_exported_summary"),
            archive_relative_path=params.get("previous_archive_relative_path"),
            obsidian_vault_path=settings.obsidian_vault_path,
        )
        _, obsidian_uri = await _write_obsidian_file(
            markdown_content,
            params["relative_path"],
            settings.obsidian_vault_path,
        )

    saved_summary = await repository.save_draft_summary(
        summary_id,
        summary_id=params.get("frontmatter", {}).get("sidekick_summary_id"),
        saved_to_obsidian_at=datetime.utcnow(),
        obsidian_relative_path=params["relative_path"] if settings.obsidian_vault_path else None,
    )
    chat_service = WorkspaceChatService(repository)
    await chat_service.append_system_event(
        session_id=session.id,
        meeting_id=meeting.id,
        transcript_version_id=draft.transcript_version_id,
        summary_id=saved_summary.id,
        template_key=draft.template_key,
        message_type="save_event",
        content="Saved the current draft to Obsidian.",
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
