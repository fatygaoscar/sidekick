"""Session and meeting REST endpoints."""

import asyncio
import json
import logging
import os
import re
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.settings import get_settings, SummarizationBackend as SumBackendEnum
from src.core.datetime_utils import localize_datetime, timezone_label, to_utc_iso
from src.core.markdown_utils import (
    build_obsidian_markdown,
    format_datetime_human,
    format_duration_human,
    format_processing_time,
)
from src.core.speaker_labels import (
    build_user_facing_speaker_map,
    infer_strict_segment_speakers,
    is_generic_speaker,
    resolve_user_facing_speaker_name,
)
from src.audio.storage import (
    assemble_chunks,
    cleanup_chunk_storage,
    clear_session_chunk_upload_state,
    ensure_session_audio_path,
    extension_from_content_type,
    get_audio_dir,
    get_available_chunks,
    get_chunk_upload_summary,
    get_missing_chunk_indices,
    get_session_audio_candidates,
    get_session_audio_path,
    read_session_chunk_meta,
    media_type_for_path,
    normalize_audio_extension,
    recover_session_audio_from_chunks,
    write_chunk,
    write_session_chunk_meta,
)
from src.audio.clips import cleanup_speaker_clip_cache, ensure_speaker_clip
from src.sessions.manager import SessionManager
from src.sessions.repository import Repository, UNSET
from src.summarization.manager import SummarizationManager
from src.summarization.prompts import DEFAULT_TEMPLATE_KEY, normalize_template_key
from src.transcription.diarize import assign_speaker, diarize
from src.transcription.speaker_attribution import (
    assign_speakers_to_segments,
    repair_quality_gate_passed,
    speaker_assignment_metrics,
)
from src.transcription.speaker_review_state import (
    speaker_identity,
    speaker_review_update_fields,
    transcript_requires_speaker_review,
)
from src.transcription.speaker_profiles import (
    apply_profile_matches_to_segments,
    build_profile_example,
    embedding_to_json,
    get_embedding_model_name,
    MAX_PROFILE_EXAMPLES,
    match_segments_to_profiles,
)
from src.workspace_chat.service import WorkspaceChatService


router = APIRouter()
logger = logging.getLogger(__name__)
_SPEAKER_DETECTION_JOBS: dict[str, dict] = {}
_SPEAKER_DETECTION_TASKS: dict[str, asyncio.Task] = {}
_SPEAKER_PROFILE_SAVE_SEMAPHORE = asyncio.Semaphore(1)


async def _workspace_chat_enabled(repository: Repository) -> bool:
    settings = await repository.get_app_settings(create_if_missing=True)
    return bool(settings.workspace_chat_enabled) if settings else False


async def _require_workspace_chat_enabled(repository: Repository) -> None:
    if not await _workspace_chat_enabled(repository):
        raise HTTPException(status_code=404, detail="Meeting Assistant is disabled")


async def _speaker_repair_enabled(repository: Repository) -> bool:
    settings = await repository.get_app_settings(create_if_missing=True)
    return bool(getattr(settings, "speaker_repair_enabled", False)) if settings else False


async def _require_speaker_repair_enabled(repository: Repository) -> None:
    if not await _speaker_repair_enabled(repository):
        raise HTTPException(status_code=404, detail="Speaker repair is disabled")


async def _list_speaker_profiles_safe(repository) -> list:
    list_profiles = getattr(repository, "list_speaker_profiles", None)
    if not callable(list_profiles):
        return []
    return await list_profiles()


async def _list_transcript_speaker_profile_overrides_safe(
    repository,
    transcript_version_id: str | None,
) -> list:
    if not transcript_version_id:
        return []
    list_overrides = getattr(repository, "list_transcript_speaker_profile_overrides", None)
    if not callable(list_overrides):
        return []
    return await list_overrides(transcript_version_id)


def _serialize_app_settings(settings) -> dict:
    return {
        "workspace_chat_enabled": bool(getattr(settings, "workspace_chat_enabled", False)),
        "speaker_repair_enabled": bool(getattr(settings, "speaker_repair_enabled", False)),
        "summarization_backend": str(
            getattr(settings, "summarization_backend", get_settings().summarization_backend.value)
        ),
        "recording_capture_mode": str(
            getattr(settings, "recording_capture_mode", "whole_room")
        ),
    }


def _serialize_settings_payload(
    settings,
    summarization_manager: SummarizationManager,
    diarization_runtime: dict[str, object] | None = None,
    diagnostics: dict[str, dict[str, object]] | None = None,
) -> dict:
    runtime = summarization_manager.runtime_state()
    if diagnostics is not None:
        runtime["diagnostics"] = diagnostics
    return {
        "settings": _serialize_app_settings(settings),
        "summarization": runtime,
        "diarization": diarization_runtime or {},
    }


def _utc_now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc)) or ""


def _create_speaker_detection_job(
    session_id: str,
    *,
    transcript_version_id: str | None = None,
    transcript_version_number: int | None = None,
) -> dict:
    job_id = str(uuid.uuid4())
    payload = {
        "job_id": job_id,
        "session_id": session_id,
        "status": "queued",
        "stage": "queued",
        "message": "Queued",
        "overall_progress": 0.0,
        "error": None,
        "transcript_version_id": transcript_version_id,
        "transcript_version_number": transcript_version_number,
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }
    _SPEAKER_DETECTION_JOBS[job_id] = payload
    return payload


def _update_speaker_detection_job(job_id: str, **fields) -> None:
    job = _SPEAKER_DETECTION_JOBS.get(job_id)
    if not job:
        return
    job.update(fields)
    stage = str(job.get("stage", "queued"))
    stage_progress = {
        "queued": 0.0,
        "diarizing": 0.3,
        "matching_profiles": 0.7,
        "writing": 0.95,
        "completed": 1.0,
    }
    job["overall_progress"] = stage_progress.get(stage, float(job.get("overall_progress", 0.0)))
    job["updated_at"] = _utc_now_iso()


def _normalize_profile_name(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


async def _run_speaker_detection_job(
    *,
    job_id: str,
    session_id: str,
    source_transcript_version_id: str,
    target_transcript_version_id: str,
    expected_speaker_count: int | None,
    repository: Repository,
) -> None:
    """Re-run diarization against stored transcript segment timing."""
    try:
        session = await repository.get_session(session_id)
        if not session:
            raise RuntimeError("Recording not found")

        source_version = await repository.get_transcript_version_for_session(
            session_id,
            source_transcript_version_id,
        )
        if source_version is None:
            raise RuntimeError("Transcript version not found")

        source_segments = await repository.get_segments(
            session_id=session_id,
            transcript_version_id=source_version.id,
        )
        if not source_segments:
            raise RuntimeError("No transcript segments are available for speaker detection.")

        audio_path = get_session_audio_path(session_id)
        if not audio_path and session.ended_at:
            audio_path = ensure_session_audio_path(session_id)
        if not audio_path:
            raise RuntimeError("Recording audio is not available.")

        _update_speaker_detection_job(
            job_id,
            status="running",
            stage="diarizing",
            message="Re-running speaker detection",
        )
        spans = await asyncio.to_thread(
            diarize,
            str(audio_path),
            get_settings().hf_token,
            None,
            int(expected_speaker_count) if expected_speaker_count is not None else None,
            int(expected_speaker_count) if expected_speaker_count is not None else None,
        )
        reassigned_segments = assign_speakers_to_segments(source_segments, spans, assign_speaker)

        _update_speaker_detection_job(
            job_id,
            stage="matching_profiles",
            message="Matching known speaker profiles",
        )
        profiles = await _list_speaker_profiles_safe(repository)
        profile_matches = {}
        if profiles:
            profile_matches = await asyncio.to_thread(
                match_segments_to_profiles,
                audio_path=str(audio_path),
                segments=reassigned_segments,
                hf_token=get_settings().hf_token,
                profiles=profiles,
            )
            if profile_matches:
                apply_profile_matches_to_segments(
                    segments=reassigned_segments,
                    matches=profile_matches,
                )

        _update_speaker_detection_job(
            job_id,
            stage="writing",
            message="Saving new transcript version",
        )
        await repository.delete_segments_for_transcript_version(target_transcript_version_id)
        for segment in reassigned_segments:
            await repository.add_segment(
                session_id=session_id,
                meeting_id=source_version.meeting_id,
                transcript_version_id=target_transcript_version_id,
                text=str(segment.text).strip(),
                start_time=float(segment.start),
                end_time=float(segment.end),
                speaker=segment.speaker,
                speaker_cluster=segment.speaker_cluster,
                confidence=None,
            )
        await repository.set_session_has_transcription(session_id, True)
        await repository.reindex_session_transcript_search(session_id)

        persisted_segments = await repository.get_segments(
            session_id=session_id,
            transcript_version_id=target_transcript_version_id,
        )
        metrics = speaker_assignment_metrics(persisted_segments)
        gate_passed = (
            repair_quality_gate_passed(
                metrics,
                expected_speaker_count=int(expected_speaker_count),
            )
            if expected_speaker_count is not None
            else None
        )
        if gate_passed is False:
            await repository.update_transcript_version(
                target_transcript_version_id,
                status="failed",
                diarization_actual_speaker_count=metrics["actual_speaker_count"],
                diarization_unassigned_segment_count=metrics["unassigned_segment_count"],
                diarization_unassigned_segment_ratio=metrics["unassigned_segment_ratio"],
                repair_quality_gate_passed=False,
            )
            raise RuntimeError(
                f"Speaker detection could not confidently separate {int(expected_speaker_count)} speakers."
            )

        updated_version = await repository.update_transcript_version(
            target_transcript_version_id,
            status="ready",
            **speaker_review_update_fields(persisted_segments),
            diarization_actual_speaker_count=metrics["actual_speaker_count"],
            diarization_unassigned_segment_count=metrics["unassigned_segment_count"],
            diarization_unassigned_segment_ratio=metrics["unassigned_segment_ratio"],
            repair_quality_gate_passed=gate_passed,
        )
        _update_speaker_detection_job(
            job_id,
            status="completed",
            stage="completed",
            message="Speaker detection complete",
            transcript_version_id=str(updated_version.id) if updated_version else target_transcript_version_id,
            transcript_version_number=int(updated_version.version_number) if updated_version else None,
        )
    except Exception as exc:
        logger.warning("Speaker detection job failed: %s", exc)
        await repository.update_transcript_version(target_transcript_version_id, status="failed")
        _update_speaker_detection_job(
            job_id,
            status="failed",
            stage="failed",
            message="Speaker detection failed",
            error=str(exc),
        )


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
    time_label = local_started_at.strftime("%I:%M%p").lstrip("0")
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


async def _sync_transcript_speaker_review_state(
    repository: Repository,
    transcript_version_id: str,
    segments: list,
):
    return await repository.update_transcript_version(
        transcript_version_id,
        **speaker_review_update_fields(segments),
    )


def _build_legacy_speaker_cluster_updates(segments: list) -> dict[str, dict[str, str | None]]:
    """Backfill stable speaker_cluster ids for legacy transcript versions."""
    existing_name_to_cluster: dict[str, str] = {}
    assigned_name_to_cluster: dict[str, str] = {}
    used_clusters: set[str] = set()
    updates: dict[str, dict[str, str | None]] = {}

    for segment in segments:
        speaker_cluster = str(getattr(segment, "speaker_cluster", "") or "").strip()
        speaker_name = str(getattr(segment, "speaker", "") or "").strip()
        if speaker_cluster:
            used_clusters.add(speaker_cluster)
            if speaker_name and not is_generic_speaker(speaker_name):
                existing_name_to_cluster.setdefault(
                    _normalize_profile_name(speaker_name),
                    speaker_cluster,
                )

    next_index = 0

    def next_legacy_cluster() -> str:
        nonlocal next_index
        while True:
            candidate = f"LEGACY_SPEAKER_{next_index:02d}"
            next_index += 1
            if candidate not in used_clusters:
                used_clusters.add(candidate)
                return candidate

    for segment in segments:
        speaker_cluster = str(getattr(segment, "speaker_cluster", "") or "").strip()
        if speaker_cluster:
            continue

        speaker_name = str(getattr(segment, "speaker", "") or "").strip()
        speaker_key = (
            _normalize_profile_name(speaker_name)
            if speaker_name and not is_generic_speaker(speaker_name)
            else "__legacy_unknown__"
        )
        assigned_cluster = (
            existing_name_to_cluster.get(speaker_key)
            or assigned_name_to_cluster.get(speaker_key)
        )
        if not assigned_cluster:
            assigned_cluster = next_legacy_cluster()
            assigned_name_to_cluster[speaker_key] = assigned_cluster
        updates[str(segment.id)] = {"speaker_cluster": assigned_cluster}

    return updates


async def _normalize_legacy_transcript_speaker_clusters(
    repository: Repository,
    *,
    session_id: str,
    transcript_version_id: str,
    segments: list,
) -> tuple[list, int]:
    updates = _build_legacy_speaker_cluster_updates(segments)
    if not updates:
        return segments, 0

    await repository.update_segments_speaker_metadata(updates)
    refreshed_segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
    )
    return refreshed_segments, len(updates)


async def _normalize_legacy_session_lifecycle(
    repository: Repository,
    *,
    session,
    latest_ready_version,
    audio_path: Path | None,
):
    if (
        audio_path is None
        or latest_ready_version is None
        or getattr(session, "ended_at", None) is None
    ):
        return session, False

    current_recording_status = str(getattr(session, "recording_status", "") or "").strip().lower()
    current_audio_status = str(getattr(session, "audio_status", "") or "").strip().lower()
    if current_recording_status == "ready" and current_audio_status == "finalized":
        return session, False

    updated_session = await repository.update_session_recording_state(
        str(session.id),
        recording_status="ready",
        audio_status="finalized",
        audio_error=None,
        finalized_at=getattr(session, "finalized_at", None) or getattr(session, "ended_at", None),
    )
    return updated_session or session, True


def _serialize_speaker_profile(profile) -> dict:
    examples = getattr(profile, "examples", []) or []
    return {
        "id": str(profile.id),
        "display_name": str(profile.display_name),
        "example_count": len(examples),
        "created_at": to_utc_iso(getattr(profile, "created_at", None)),
    }


def _serialize_speaker_profile_example(
    example,
    *,
    recording_title: str | None = None,
) -> dict:
    session_id = str(getattr(example, "session_id", ""))
    return {
        "id": str(example.id),
        "session_id": session_id,
        "transcript_version_id": (
            str(getattr(example, "transcript_version_id", None))
            if getattr(example, "transcript_version_id", None)
            else None
        ),
        "speaker_cluster": getattr(example, "speaker_cluster", None),
        "clip_start_seconds": float(getattr(example, "clip_start_seconds", 0.0) or 0.0),
        "clip_end_seconds": float(getattr(example, "clip_end_seconds", 0.0) or 0.0),
        "duration_seconds": float(getattr(example, "duration_seconds", 0.0) or 0.0),
        "source_type": str(getattr(example, "source_type", "") or ""),
        "created_at": to_utc_iso(getattr(example, "created_at", None)),
        "recording_title": recording_title,
        "clip_url": f"/api/speaker-profile-examples/{urllib.parse.quote(str(example.id), safe='')}/audio",
    }


def _speaker_profiles_by_name(profiles: list) -> dict[str, object]:
    return {
        _normalize_profile_name(getattr(profile, "display_name", None)): profile
        for profile in profiles
        if _normalize_profile_name(getattr(profile, "display_name", None))
    }


def _speaker_profile_overrides_by_cluster(overrides: list | None) -> dict[str, object]:
    return {
        str(getattr(override, "speaker_cluster", "")): override
        for override in (overrides or [])
        if str(getattr(override, "speaker_cluster", "")).strip()
    }


def _transcript_version_label(version) -> str:
    base = f"v{int(getattr(version, 'version_number', 1) or 1)}"
    source_type = str(getattr(version, "source_type", "") or "").strip().lower()
    if source_type == "speaker_cluster_merge":
        return f"{base} (Merged Speakers)"
    if source_type in {"retranscription_repair", "retranscription", "speaker_detection_rerun"} and getattr(version, "repair_reason", None):
        return f"{base} (Repaired)"
    return base


def _serialize_transcript_version(version, latest_version_id: str | None) -> dict:
    return {
        "id": str(version.id),
        "version_number": int(version.version_number),
        "label": _transcript_version_label(version),
        "is_latest": str(version.id) == str(latest_version_id) if latest_version_id else False,
        "status": version.status,
        "source_type": version.source_type,
        "repair_reason": getattr(version, "repair_reason", None),
        "is_repaired_version": bool(getattr(version, "repair_reason", None)),
        "derived_from_transcript_version_id": (
            str(getattr(version, "diarization_repair_source_version_id", None))
            if getattr(version, "diarization_repair_source_version_id", None)
            else (
                str(getattr(version, "parent_version_id", None))
                if getattr(version, "parent_version_id", None)
                else None
            )
        ),
        "diarization_expected_speaker_count": getattr(version, "diarization_expected_speaker_count", None),
        "diarization_late_join_offset_seconds": getattr(version, "diarization_late_join_offset_seconds", None),
        "repair_strategy": getattr(version, "repair_strategy", None),
        "diarization_actual_speaker_count": getattr(version, "diarization_actual_speaker_count", None),
        "diarization_unassigned_segment_count": getattr(version, "diarization_unassigned_segment_count", None),
        "diarization_unassigned_segment_ratio": getattr(version, "diarization_unassigned_segment_ratio", None),
        "repair_quality_gate_passed": getattr(version, "repair_quality_gate_passed", None),
        "created_at": to_utc_iso(version.created_at),
    }


def _serialize_summary(summary, meeting=None) -> dict:
    out_of_date_reason = _summary_out_of_date_reason(meeting, summary) if meeting else None
    workflow_data = _safe_json_loads(getattr(summary, "workflow_data_json", None), {})
    revision_history = workflow_data.get("revision_history", []) if isinstance(workflow_data, dict) else []
    if not isinstance(revision_history, list):
        revision_history = []
    latest_revision = revision_history[-1] if revision_history else None
    save_kind = (
        "exported"
        if (getattr(summary, "saved_to_obsidian_at", None) or getattr(summary, "obsidian_relative_path", None))
        else "saved_copy"
    )
    return {
        "id": str(summary.id),
        "meeting_id": str(summary.meeting_id),
        "transcript_version_id": (
            str(getattr(summary, "transcript_version_id", None))
            if getattr(summary, "transcript_version_id", None)
            else None
        ),
        "content": summary.content,
        "backend": summary.backend,
        "model": summary.model,
        "created_at": to_utc_iso(summary.created_at),
        "processing_duration_seconds": summary.processing_duration_seconds,
        "template": summary.template,
        "template_key": summary.template_key,
        "custom_prompt": summary.custom_prompt,
        "status": summary.status,
        "source_type": summary.source_type,
        "saved_to_obsidian_at": to_utc_iso(summary.saved_to_obsidian_at),
        "obsidian_relative_path": summary.obsidian_relative_path,
        "save_kind": save_kind if getattr(summary, "status", None) == "saved" else None,
        "summary_out_of_date": out_of_date_reason is not None,
        "summary_out_of_date_reason": out_of_date_reason,
        "revision_history": revision_history,
        "latest_revision": latest_revision if isinstance(latest_revision, dict) else None,
    }


def _safe_json_loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _summary_version_label(summary, saved_summaries: list) -> str:
    if not summary:
        return "No summary"
    if getattr(summary, "status", None) == "draft":
        return f"v{len(saved_summaries) + 1} (Draft)"
    ordered = list(saved_summaries or [])
    for index, item in enumerate(ordered):
        if str(getattr(item, "id", "")) != str(getattr(summary, "id", "")):
            continue
        total_saved = len(ordered)
        version_number = total_saved - index
        save_kind = (
            "exported"
            if (getattr(item, "saved_to_obsidian_at", None) or getattr(item, "obsidian_relative_path", None))
            else "saved_copy"
        )
        if save_kind == "exported":
            latest_exported = next(
                (
                    candidate
                    for candidate in ordered
                    if (getattr(candidate, "saved_to_obsidian_at", None) or getattr(candidate, "obsidian_relative_path", None))
                ),
                None,
            )
            if latest_exported and str(getattr(latest_exported, "id", "")) == str(getattr(item, "id", "")):
                return f"v{version_number} (Latest Exported)"
            return f"v{version_number} (Exported)"
        return f"v{version_number} (Saved Copy)"
    return "Saved summary"


def _workspace_chat_context_text(transcript_version, summary, saved_summaries: list) -> str:
    transcript_label = "Transcript"
    if transcript_version:
        transcript_label = f"Transcript v{getattr(transcript_version, 'version_number', 1)}"
    summary_label = _summary_version_label(summary, saved_summaries)
    return f"Context updated to {transcript_label} and {summary_label}."


def _serialize_chat_message(message, saved_summaries: list) -> dict:
    metadata = _safe_json_loads(getattr(message, "metadata_json", None), {})
    citations = _safe_json_loads(getattr(message, "citations_json", None), [])
    retrieval_windows = _safe_json_loads(getattr(message, "retrieval_windows_json", None), [])
    return {
        "id": str(message.id),
        "thread_id": str(message.thread_id),
        "role": message.role,
        "message_type": message.message_type,
        "content": message.content,
        "transcript_version_id": (
            str(message.transcript_version_id) if getattr(message, "transcript_version_id", None) else None
        ),
        "summary_id": str(message.summary_id) if getattr(message, "summary_id", None) else None,
        "citations": citations if isinstance(citations, list) else [],
        "retrieval_windows": retrieval_windows if isinstance(retrieval_windows, list) else [],
        "intent_label": message.intent_label,
        "intent_confidence": message.intent_confidence,
        "suggests_summary_change": bool(getattr(message, "suggests_summary_change", False)),
        "suggested_change_kind": message.suggested_change_kind,
        "apply_ready": bool(getattr(message, "apply_ready", False)),
        "applied": getattr(message, "applied_at", None) is not None,
        "applied_summary_id": (
            str(message.applied_summary_id) if getattr(message, "applied_summary_id", None) else None
        ),
        "applied_draft_summary_id": (
            str(message.applied_draft_summary_id) if getattr(message, "applied_draft_summary_id", None) else None
        ),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": to_utc_iso(message.created_at),
    }


def _serialize_transcript_segments(segments: list) -> list[dict]:
    inferred_speakers = infer_strict_segment_speakers(segments)
    fallback_map = build_user_facing_speaker_map(
        (
            (inferred or {}).get("speaker_cluster")
            or getattr(segment, "speaker_cluster", None)
            or getattr(segment, "speaker", None)
            or (inferred or {}).get("speaker")
        )
        for segment, inferred in zip(segments, inferred_speakers)
    )
    transcript_lines = []
    for segment, inferred in zip(segments, inferred_speakers):
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        effective_speaker = getattr(segment, "speaker", None) or (inferred or {}).get("speaker")
        raw_label = (
            getattr(segment, "speaker_cluster", None)
            or getattr(segment, "speaker", None)
            or (inferred or {}).get("speaker_cluster")
            or (inferred or {}).get("speaker")
        )
        speaker = resolve_user_facing_speaker_name(
            effective_speaker,
            raw_label,
            fallback_map,
        ) or "?"
        transcript_lines.append(
            {
                "id": str(segment.id),
                "timestamp": f"[{mins:02d}:{secs:02d}]",
                "text": segment.text,
                "speaker": speaker,
                "speaker_cluster": getattr(segment, "speaker_cluster", None) or (inferred or {}).get("speaker_cluster"),
                "is_important": segment.is_important,
                "start_time": segment.start_time,
                "end_time": segment.end_time,
            }
        )
    return transcript_lines


def _build_speaker_cards(
    segments: list,
    session_id: str,
    transcript_version_id: str | None = None,
    speaker_profiles: list | None = None,
    speaker_profile_overrides: list | None = None,
) -> list[dict]:
    def segment_duration(segment) -> float:
        return max(0.0, float(segment.end_time) - float(segment.start_time))

    def choose_representative_segment(speaker_segments: list):
        preferred = [
            segment for segment in speaker_segments
            if 3.0 <= segment_duration(segment) <= 8.0
        ]
        if preferred:
            return preferred[0]

        bounded = [
            segment for segment in speaker_segments
            if segment_duration(segment) <= 10.0
        ]
        if bounded:
            return max(
                bounded,
                key=lambda segment: (segment_duration(segment), -float(segment.start_time)),
            )

        long_enough = [
            segment for segment in speaker_segments
            if segment_duration(segment) >= 1.5
        ]
        if long_enough:
            return long_enough[0]

        return speaker_segments[0]

    grouped: dict[str, list] = {}
    profiles_by_name = _speaker_profiles_by_name(speaker_profiles or [])
    overrides_by_cluster = _speaker_profile_overrides_by_cluster(speaker_profile_overrides)
    for segment in segments:
        speaker_cluster = speaker_identity(segment)
        if not speaker_cluster:
            continue
        grouped.setdefault(speaker_cluster, []).append(segment)

    cards: list[dict] = []
    for speaker_cluster, speaker_segments in sorted(grouped.items(), key=lambda item: item[0]):
        speaker_segments.sort(key=lambda segment: segment.start_time)
        representative = choose_representative_segment(speaker_segments)
        display_name = getattr(representative, "speaker", None)
        clip_start = max(0.0, float(representative.start_time) - 0.25)
        clip_end = min(float(representative.end_time), clip_start + 5.0)
        if clip_end <= clip_start:
            clip_end = max(clip_start + 0.5, float(representative.end_time))
        override = overrides_by_cluster.get(str(speaker_cluster))
        matched_profile = getattr(override, "speaker_profile", None) if override is not None else None
        match_source = "override" if matched_profile is not None else "none"
        if matched_profile is None:
            matched_profile = profiles_by_name.get(_normalize_profile_name(display_name))
            if matched_profile is not None:
                match_source = "name_inferred"
        cards.append(
            {
                "speaker_cluster": speaker_cluster,
                "display_name": None if display_name == speaker_cluster else display_name,
                "matched_profile_id": str(matched_profile.id) if matched_profile else None,
                "matched_profile_name": getattr(matched_profile, "display_name", None) if matched_profile else None,
                "matched_profile_example_count": len(getattr(matched_profile, "examples", []) or []) if matched_profile else 0,
                "match_confidence": None,
                "profile_suggestion_state": "matched" if matched_profile else "none",
                "match_source": match_source,
                "can_change_match": bool(matched_profile),
                "raw_label": speaker_cluster,
                "preview_text": representative.text[:160],
                "clip_start": clip_start,
                "clip_end": clip_end,
                "clip_url": (
                    f"/api/recordings/{session_id}/speaker-clips/"
                    f"{urllib.parse.quote(str(speaker_cluster), safe='')}/audio"
                    + (
                        f"?transcript_version_id={urllib.parse.quote(str(transcript_version_id), safe='')}"
                        if transcript_version_id
                        else ""
                    )
                ),
                "needs_name": is_generic_speaker(speaker_cluster)
                and (not display_name or display_name == speaker_cluster),
            }
        )
    return cards


def _summary_out_of_date_reason(meeting, summary) -> str | None:
    if not summary:
        return None
    meeting_speaker_review_required = bool(getattr(meeting, "speaker_review_required", False))
    meeting_speaker_review_completed_at = getattr(meeting, "speaker_review_completed_at", None)
    if (
        meeting
        and meeting_speaker_review_required
        and meeting_speaker_review_completed_at
        and summary.created_at < meeting_speaker_review_completed_at
    ):
        return "Speaker assignments changed after this summary was generated."
    meeting_template_key = normalize_template_key(
        (getattr(meeting, "template_key", None) or DEFAULT_TEMPLATE_KEY) if meeting else DEFAULT_TEMPLATE_KEY
    )
    if summary.template_key:
        if normalize_template_key(summary.template_key) != meeting_template_key:
            return "Summary settings changed to a different template."
    elif meeting_template_key != DEFAULT_TEMPLATE_KEY:
        return "Summary settings changed to a different template."
    summary_prompt = _normalize_optional_text(summary.custom_prompt)
    meeting_prompt = _normalize_optional_text(getattr(meeting, "custom_prompt", None) if meeting else None)
    if (summary_prompt or meeting_prompt) and summary_prompt != meeting_prompt:
        return "Summary prompt settings changed after this summary was generated."
    return None


def _summary_is_out_of_date(meeting, summary) -> bool:
    return _summary_out_of_date_reason(meeting, summary) is not None


def _build_recording_workspace_state(
    session,
    meeting,
    segments: list,
    saved_summaries: list,
    draft_summary,
    latest_saved_summary,
) -> dict:
    current_summary = draft_summary or latest_saved_summary
    summary_out_of_date_reason = (
        _summary_out_of_date_reason(meeting, current_summary)
        if meeting
        else None
    )
    return {
        "has_transcription": bool(meeting and session.has_transcription),
        "requires_speaker_review": bool(
            meeting
            and meeting.speaker_review_required
            and meeting.speaker_review_completed_at is None
        ),
        "can_generate_summary": bool(meeting and session.has_transcription),
        "has_unsaved_draft": draft_summary is not None,
        "summary_out_of_date": summary_out_of_date_reason is not None,
        "summary_out_of_date_reason": summary_out_of_date_reason,
        "transcript_segment_count": len(segments),
        "saved_summary_count": len(saved_summaries),
    }


def _serialize_recording_lifecycle(session, *, audio_path=None) -> dict:
    recording_status = (getattr(session, "recording_status", None) or "starting").strip() or "starting"
    audio_status = (getattr(session, "audio_status", None) or "none").strip() or "none"
    workspace_ready = bool(
        recording_status == "ready"
        and audio_status == "finalized"
        and audio_path is not None
    )
    return {
        "recording_status": recording_status,
        "audio_status": audio_status,
        "audio_error": getattr(session, "audio_error", None),
        "finalized_at": to_utc_iso(getattr(session, "finalized_at", None)),
        "workspace_ready": workspace_ready,
    }


async def _finalize_chunked_recording_audio(
    *,
    session_id: str,
    client_id: str,
    mime_type: str | None,
    expected_chunks: int,
):
    """Assemble chunked recording audio when the expected chunk set is complete."""
    missing = get_missing_chunk_indices(session_id, client_id, expected_chunks)
    if missing:
        return None, missing

    extension = extension_from_content_type(mime_type or "")
    final_path = assemble_chunks(session_id, client_id, expected_chunks, extension)
    if not final_path:
        raise HTTPException(status_code=400, detail="Failed to assemble chunks")
    cleanup_speaker_clip_cache(session_id)
    return final_path, []


def _serialize_chunk_summary(summary: dict | None) -> dict:
    summary = summary or {}
    return {
        "client_id": summary.get("client_id"),
        "available_count": int(summary.get("available_count") or 0),
        "highest_index": summary.get("highest_index"),
        "expected_count": summary.get("expected_count"),
        "missing_indices": list(summary.get("missing_indices") or []),
        "is_contiguous": bool(summary.get("is_contiguous")),
        "is_complete": bool(summary.get("is_complete")),
    }


def _merge_chunk_meta(
    session_id: str,
    *,
    client_id: str | None = None,
    extension: str | None = None,
    expected_chunks: int | None = None,
) -> dict:
    meta = read_session_chunk_meta(session_id) or {}
    if client_id:
        meta["client_id"] = client_id
    if extension:
        meta["extension"] = extension
    if expected_chunks and expected_chunks > 0:
        meta["expected_chunks"] = int(expected_chunks)
    write_session_chunk_meta(session_id, meta)
    return meta


async def _recover_recording_audio(
    *,
    session_id: str,
    client_id: str | None,
    mime_type: str | None,
    expected_chunks: int | None,
    settle_timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> tuple[Path | None, dict | None]:
    preferred_extension = extension_from_content_type(mime_type or "")
    stripped_client_id = (client_id or "").strip() or None
    normalized_expected = int(expected_chunks) if expected_chunks and int(expected_chunks) > 0 else None
    last_summary: dict | None = None

    if stripped_client_id or preferred_extension or normalized_expected:
        _merge_chunk_meta(
            session_id,
            client_id=stripped_client_id,
            extension=preferred_extension,
            expected_chunks=normalized_expected,
        )

    existing_audio = get_session_audio_path(session_id)
    if existing_audio:
        return existing_audio, last_summary

    if stripped_client_id and normalized_expected:
        attempts = max(1, int(settle_timeout_seconds / poll_interval_seconds))
        for attempt in range(attempts):
            summary = get_chunk_upload_summary(
                session_id,
                stripped_client_id,
                normalized_expected,
            )
            last_summary = summary
            logger.info(
                "recording_complete:chunk_summary session_id=%s client_id=%s attempt=%d available=%d expected=%d missing=%d complete=%s",
                session_id,
                stripped_client_id,
                attempt + 1,
                summary["available_count"],
                normalized_expected,
                len(summary["missing_indices"]),
                summary["is_complete"],
            )
            if summary["is_complete"]:
                recovered = recover_session_audio_from_chunks(
                    session_id,
                    client_id=stripped_client_id,
                    expected_count=normalized_expected,
                    preferred_extension=preferred_extension,
                )
                if recovered:
                    cleanup_speaker_clip_cache(session_id)
                    return recovered, summary
            if attempt < attempts - 1:
                await asyncio.sleep(poll_interval_seconds)

    recovered = recover_session_audio_from_chunks(
        session_id,
        client_id=stripped_client_id,
        expected_count=normalized_expected,
        preferred_extension=preferred_extension,
    )
    if recovered:
        cleanup_speaker_clip_cache(session_id)
        return recovered, last_summary

    if stripped_client_id:
        last_summary = last_summary or get_chunk_upload_summary(
            session_id,
            stripped_client_id,
            normalized_expected,
        )

    return None, last_summary


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


class CompleteRecordingRequest(BaseModel):
    client_id: Optional[str] = None
    mime_type: Optional[str] = None
    expected_chunks: Optional[int] = None
    allow_fallback_blob: bool = False


class SessionResponse(BaseModel):
    id: str
    mode: str
    submode: Optional[str]
    timezone_name: Optional[str]
    timezone_offset_minutes: Optional[int]
    is_active: bool
    recording_status: str
    audio_status: str
    audio_error: Optional[str]
    started_at: str
    ended_at: Optional[str]
    finalized_at: Optional[str]

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
        recording_status=session.recording_status,
        audio_status=session.audio_status,
        audio_error=session.audio_error,
        started_at=to_utc_iso(session.started_at),
        ended_at=to_utc_iso(session.ended_at),
        finalized_at=to_utc_iso(session.finalized_at),
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
        recording_status=session.recording_status,
        audio_status=session.audio_status,
        audio_error=session.audio_error,
        started_at=to_utc_iso(session.started_at),
        ended_at=to_utc_iso(session.ended_at),
        finalized_at=to_utc_iso(session.finalized_at),
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


@router.delete("/sessions/{session_id}")
async def end_session_by_id(
    session_id: str,
    repository: Repository = Depends(get_repository),
    session_manager: SessionManager = Depends(get_session_manager),
):
    """End a session by ID."""
    existing_session = await repository.get_session(session_id)
    if not existing_session:
        raise HTTPException(status_code=404, detail="Session not found")

    was_active = bool(existing_session.is_active)
    session = await session_manager.end_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "status": "ended" if was_active else "already_ended",
        "session_id": session.id,
    }


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
    transcript_version_id: Optional[str] = None


class UpdateSpeakerAssignmentsRequest(BaseModel):
    assignments: dict[str, str]
    transcript_version_id: Optional[str] = None


class MergeSpeakerClustersRequest(BaseModel):
    source_speaker_cluster: str
    target_speaker_cluster: str


class SpeakerProfileCreateRequest(BaseModel):
    display_name: str


class PromoteSpeakerProfileRequest(BaseModel):
    speaker_cluster: str
    display_name: Optional[str] = None
    speaker_profile_id: Optional[str] = None


class CorrectSpeakerProfileMatchRequest(BaseModel):
    speaker_cluster: str
    accepted_profile_id: Optional[str] = None
    accepted_display_name: Optional[str] = None


class StartSpeakerDetectionJobRequest(BaseModel):
    source_transcript_version_id: Optional[str] = None
    expected_speaker_count: Optional[int] = None


class SpeakerDetectionJobCreateResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str
    transcript_version_id: Optional[str] = None
    transcript_version_number: Optional[int] = None


class SpeakerDetectionJobStatus(BaseModel):
    job_id: str
    session_id: str
    status: str
    stage: str
    message: str
    overall_progress: float
    created_at: str
    updated_at: str
    error: Optional[str] = None
    transcript_version_id: Optional[str] = None
    transcript_version_number: Optional[int] = None


class WorkspaceChatMessageRequest(BaseModel):
    content: str
    transcript_version_id: Optional[str] = None
    summary_id: Optional[str] = None


class WorkspaceChatApplyRequest(BaseModel):
    transcript_version_id: Optional[str] = None
    summary_id: Optional[str] = None


class UpdateAppSettingsRequest(BaseModel):
    workspace_chat_enabled: Optional[bool] = None
    speaker_repair_enabled: Optional[bool] = None
    summarization_backend: Optional[str] = None
    recording_capture_mode: Optional[str] = None


async def _resolve_workspace_chat_context(
    session_id: str,
    *,
    transcript_version_id: str | None,
    summary_id: str | None,
    repository: Repository,
):
    """Resolve the workspace entities needed for one chat/apply request."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    meeting = await repository.get_primary_meeting(session_id, create_if_missing=True)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    await repository.ensure_transcript_versions(session_id)
    if transcript_version_id:
        transcript_version = await repository.get_transcript_version_for_session(
            session_id,
            transcript_version_id,
        )
        if transcript_version is None:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    else:
        transcript_version = await repository.get_latest_transcript_version(session_id)

    if transcript_version is None or not session.has_transcription:
        raise HTTPException(status_code=400, detail="Chat requires a completed transcript")

    saved_summaries = await repository.get_summaries(
        meeting.id,
        status="saved",
        transcript_version_id=transcript_version.id,
    )
    draft_summary = await repository.get_draft_summary(
        meeting.id,
        transcript_version_id=transcript_version.id,
    )
    available_summaries = [draft_summary, *saved_summaries]
    selected_summary = draft_summary or (saved_summaries[0] if saved_summaries else None)
    if summary_id:
        selected_summary = next(
            (summary for summary in available_summaries if summary and str(summary.id) == str(summary_id)),
            None,
        )
        if selected_summary is None:
            raise HTTPException(status_code=404, detail="Summary not found for this transcript version")

    return session, meeting, transcript_version, selected_summary, saved_summaries, draft_summary


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
    transcript_version_id: Optional[str] = None,
    repository: Repository = Depends(get_repository),
):
    """Return the unified recording workspace state for new and past recordings."""
    started_at = perf_counter()

    def log_step(step: str, step_started_at: float, **extra) -> None:
        logger.info(
            "workspace_load: %s | session=%s elapsed_ms=%.1f%s",
            step,
            session_id,
            (perf_counter() - step_started_at) * 1000.0,
            f" extra={extra}" if extra else "",
        )

    step_started_at = perf_counter()
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    log_step("get_session", step_started_at, has_transcription=bool(session.has_transcription))

    step_started_at = perf_counter()
    meeting = await repository.get_primary_meeting(session_id, create_if_missing=True)
    if not meeting:
        raise HTTPException(status_code=400, detail="No meeting found for this recording")
    log_step("get_primary_meeting", step_started_at, meeting_id=str(meeting.id))

    step_started_at = perf_counter()
    versions = await repository.ensure_transcript_versions(session_id)
    active_version = None
    if transcript_version_id:
        active_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
    if active_version is None:
        active_version = await repository.get_latest_transcript_version(session_id)
    latest_version = await repository.get_latest_transcript_version(session_id, include_processing=True)
    log_step(
        "load_transcript_versions",
        step_started_at,
        version_count=len(versions),
        active_version_id=str(active_version.id) if active_version else None,
        latest_version_id=str(latest_version.id) if latest_version else None,
    )
    latest_ready_version = next(
        (version for version in versions if str(getattr(version, "status", "") or "").lower() == "ready"),
        None,
    )

    audio_path = get_session_audio_path(session.id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session.id)

    step_started_at = perf_counter()
    session, lifecycle_normalized = await _normalize_legacy_session_lifecycle(
        repository,
        session=session,
        latest_ready_version=latest_ready_version,
        audio_path=audio_path,
    )
    log_step(
        "normalize_legacy_lifecycle",
        step_started_at,
        normalized=lifecycle_normalized,
        recording_status=getattr(session, "recording_status", None),
        audio_status=getattr(session, "audio_status", None),
    )

    step_started_at = perf_counter()
    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id if active_version else None,
    )
    legacy_segment_updates = 0
    if active_version and segments:
        segments, legacy_segment_updates = await _normalize_legacy_transcript_speaker_clusters(
            repository,
            session_id=session_id,
            transcript_version_id=active_version.id,
            segments=segments,
        )
    log_step(
        "normalize_legacy_speaker_clusters",
        step_started_at,
        updated_segments=legacy_segment_updates,
    )

    step_started_at = perf_counter()
    if active_version and session.has_transcription and segments:
        inferred_requires_review = transcript_requires_speaker_review(segments)
        is_completed = getattr(active_version, "speaker_review_completed_at", None) is not None
        should_be_completed = not inferred_requires_review
        if (
            inferred_requires_review != bool(active_version.speaker_review_required)
            or is_completed != should_be_completed
        ):
            active_version = await _sync_transcript_speaker_review_state(
                repository,
                str(active_version.id),
                segments,
            )
    log_step("get_segments", step_started_at, segment_count=len(segments))

    step_started_at = perf_counter()
    saved_summaries = await repository.get_summaries(
        meeting.id,
        status="saved",
        transcript_version_id=active_version.id if active_version else None,
    )
    draft_summary = await repository.get_draft_summary(
        meeting.id,
        transcript_version_id=active_version.id if active_version else None,
    )
    latest_saved_summary = saved_summaries[0] if saved_summaries else None
    latest_exported_summary = next(
        (
            summary
            for summary in saved_summaries
            if (getattr(summary, "saved_to_obsidian_at", None) or getattr(summary, "obsidian_relative_path", None))
        ),
        None,
    )
    log_step(
        "load_summaries",
        step_started_at,
        saved_summary_count=len(saved_summaries),
        has_draft=bool(draft_summary),
    )

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

    lifecycle = _serialize_recording_lifecycle(session, audio_path=audio_path)

    settings = get_settings()
    vault_name = (
        os.path.basename(settings.obsidian_vault_path.rstrip("/"))
        if settings.obsidian_vault_path
        else ""
    )
    open_in_obsidian_uri = None
    if latest_exported_summary and latest_exported_summary.obsidian_relative_path and vault_name:
        open_in_obsidian_uri = (
            f"obsidian://open?"
            f"vault={urllib.parse.quote(vault_name)}&"
            f"file={urllib.parse.quote(latest_exported_summary.obsidian_relative_path)}"
        )
    elif latest_exported_summary:
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

    step_started_at = perf_counter()
    speaker_profiles = await _list_speaker_profiles_safe(repository)
    speaker_profile_overrides = await _list_transcript_speaker_profile_overrides_safe(
        repository,
        active_version.id if active_version else None,
    )
    speaker_cards = _build_speaker_cards(
        segments,
        session_id,
        active_version.id if active_version else None,
        speaker_profiles=speaker_profiles,
        speaker_profile_overrides=speaker_profile_overrides,
    ) if session.has_transcription else []
    log_step(
        "build_speaker_review",
        step_started_at,
        profile_count=len(speaker_profiles),
        override_count=len(speaker_profile_overrides),
        speaker_card_count=len(speaker_cards),
    )
    chat_service = WorkspaceChatService(repository)
    chat_thread = None
    chat_messages = []
    app_settings = await repository.get_app_settings(create_if_missing=True)
    chat_enabled = bool(getattr(app_settings, "workspace_chat_enabled", False)) if app_settings else False
    speaker_repair_enabled = bool(getattr(app_settings, "speaker_repair_enabled", False)) if app_settings else False
    step_started_at = perf_counter()
    if chat_enabled and active_version and session.has_transcription:
        chat_thread, raw_chat_messages = await chat_service.list_messages(session.id, meeting.id)
        chat_messages = [
            _serialize_chat_message(message, saved_summaries)
            for message in raw_chat_messages
        ]
    log_step(
        "load_chat",
        step_started_at,
        enabled=chat_enabled,
        has_thread=bool(chat_thread),
        message_count=len(chat_messages),
    )

    logger.info(
        "workspace_load: completed | session=%s elapsed_ms=%.1f transcript_versions=%d segments=%d summaries=%d",
        session_id,
        (perf_counter() - started_at) * 1000.0,
        len(versions),
        len(segments),
        len(saved_summaries),
    )

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
            "recorded_datetime_label": " · ".join(
                part for part in (date_label, time_label) if part
            ),
            "recorded_date_label": date_label,
            "recorded_time_label": time_label,
            "recorded_timezone_label": tz_label,
            "started_at": to_utc_iso(session.started_at),
            "ended_at": to_utc_iso(session.ended_at),
            "duration_seconds": duration_seconds,
            "segment_count": len(segments),
            "has_transcription": bool(active_version and session.has_transcription),
            "has_audio": audio_path is not None,
            "audio_url": f"/api/recordings/{session.id}/audio" if audio_path else None,
            "audio_download_url": (
                f"/api/recordings/{session.id}/audio?download=true" if audio_path else None
            ),
            **lifecycle,
        },
        "debug_retranscribe_enabled": bool(get_settings().enable_debug_retranscribe),
        "speaker_repair_enabled": speaker_repair_enabled,
        "transcript_versions": [
            _serialize_transcript_version(version, latest_version.id if latest_version else None)
            for version in versions
            if version.status != "failed"
        ],
        "active_transcript_version": (
            _serialize_transcript_version(active_version, latest_version.id if latest_version else None)
            if active_version
            else None
        ),
        "settings": {
            "title": meeting.title,
            "template_key": (
                normalize_template_key(
                    (active_version.template_key if active_version else None)
                    or meeting.template_key
                    or DEFAULT_TEMPLATE_KEY
                )
            ),
            "custom_prompt": active_version.custom_prompt if active_version else meeting.custom_prompt,
        },
        "speaker_review": {
            "required": bool(active_version.speaker_review_required) if active_version else False,
            "repair_enabled": speaker_repair_enabled,
            "completed": (
                True
                if not active_version
                else (not active_version.speaker_review_required)
                or active_version.speaker_review_completed_at is not None
            ),
            "completed_at": to_utc_iso(active_version.speaker_review_completed_at) if active_version else None,
            "speakers": speaker_cards if active_version and session.has_transcription else [],
            "known_profiles": [_serialize_speaker_profile(profile) for profile in speaker_profiles],
        },
        "transcript": _serialize_transcript_segments(segments) if active_version and session.has_transcription else [],
        "draft_summary": _serialize_summary(draft_summary, active_version) if draft_summary else None,
        "saved_summaries": [_serialize_summary(summary, active_version) for summary in saved_summaries],
        "active_summary": _serialize_summary(draft_summary or latest_saved_summary, active_version)
        if (draft_summary or latest_saved_summary)
        else None,
        "obsidian": {
            "open_uri": open_in_obsidian_uri,
            "latest_relative_path": latest_exported_summary.obsidian_relative_path
            if latest_exported_summary
            else None,
        },
        "chat": {
            "enabled": chat_enabled,
            "thread_id": str(chat_thread.id) if chat_thread else None,
            "messages": chat_messages,
            "active_context": {
                "transcript_version_id": str(active_version.id) if active_version else None,
                "summary_id": (
                    str((draft_summary or latest_saved_summary).id)
                    if (draft_summary or latest_saved_summary)
                    else None
                ),
                "draft_summary_id": str(draft_summary.id) if draft_summary else None,
            },
        },
        "state": _build_recording_workspace_state(
            session,
            active_version,
            segments,
            saved_summaries,
            draft_summary,
            latest_saved_summary,
        ),
    }


@router.get("/settings")
async def get_app_settings(
    request: Request,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Return global app settings."""
    settings = await repository.get_app_settings(create_if_missing=True)
    diarization_runtime = getattr(request.app.state, "diarization_runtime", None) or {}
    return _serialize_settings_payload(settings, summarization_manager, diarization_runtime)


@router.patch("/settings")
async def update_app_settings(
    request: UpdateAppSettingsRequest,
    request_http: Request,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Persist global app settings."""
    provided_fields = getattr(request, "model_fields_set", set())
    if not provided_fields:
        raise HTTPException(status_code=422, detail="No settings were provided")

    current_backend = summarization_manager.active_backend_type or get_settings().summarization_backend
    target_backend = current_backend
    switched_backend = False

    if "summarization_backend" in provided_fields:
        if request.summarization_backend is None:
            raise HTTPException(status_code=422, detail="summarization_backend cannot be null")
        try:
            target_backend = SumBackendEnum(str(request.summarization_backend).strip().lower())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Unsupported summarization backend") from exc

        if target_backend not in {SumBackendEnum.OLLAMA, SumBackendEnum.OPENAI}:
            raise HTTPException(status_code=422, detail="Only OpenAI and Ollama are supported in settings")

        probe = await summarization_manager.probe_backend(target_backend)
        if not probe.ready:
            raise HTTPException(status_code=503, detail=probe.message)

        if target_backend != current_backend:
            await summarization_manager.switch_backend(target_backend)
            switched_backend = True

    normalized_capture_mode = UNSET
    if "recording_capture_mode" in provided_fields:
        if request.recording_capture_mode is None:
            raise HTTPException(status_code=422, detail="recording_capture_mode cannot be null")
        normalized_capture_mode = str(request.recording_capture_mode).strip().lower()
        if normalized_capture_mode not in {"single_speaker", "whole_room"}:
            raise HTTPException(status_code=422, detail="Unsupported recording capture mode")

    try:
        settings = await repository.update_app_settings(
            workspace_chat_enabled=(
                request.workspace_chat_enabled
                if "workspace_chat_enabled" in provided_fields
                else UNSET
            ),
            speaker_repair_enabled=(
                request.speaker_repair_enabled
                if "speaker_repair_enabled" in provided_fields
                else UNSET
            ),
            summarization_backend=(
                target_backend.value
                if "summarization_backend" in provided_fields
                else UNSET
            ),
            recording_capture_mode=normalized_capture_mode,
        )
    except Exception:
        if switched_backend:
            await summarization_manager.switch_backend(current_backend)
        raise

    diarization_runtime = getattr(request_http.app.state, "diarization_runtime", None) or {}
    return _serialize_settings_payload(settings, summarization_manager, diarization_runtime)


@router.post("/settings/summarization/diagnostics")
async def run_summarization_diagnostics(
    request: Request,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Run live readiness checks for the supported summarization providers."""
    settings = await repository.get_app_settings(create_if_missing=True)
    diagnostics = await summarization_manager.diagnose_backends(
        [SumBackendEnum.OLLAMA, SumBackendEnum.OPENAI]
    )
    diarization_runtime = getattr(request.app.state, "diarization_runtime", None) or {}
    return _serialize_settings_payload(
        settings,
        summarization_manager,
        diarization_runtime,
        diagnostics=diagnostics,
    )


@router.get("/speaker-profiles")
async def get_speaker_profiles(
    repository: Repository = Depends(get_repository),
):
    """Return locally stored speaker profiles."""
    profiles = await _list_speaker_profiles_safe(repository)
    return {
        "profiles": [_serialize_speaker_profile(profile) for profile in profiles],
    }


@router.post("/speaker-profiles")
async def create_speaker_profile(
    request: SpeakerProfileCreateRequest,
    repository: Repository = Depends(get_repository),
):
    """Create or reuse a local speaker profile."""
    display_name = " ".join(str(request.display_name or "").strip().split())
    if not display_name:
        raise HTTPException(status_code=422, detail="display_name is required")
    profile = await repository.create_speaker_profile(display_name=display_name)
    return {"profile": _serialize_speaker_profile(profile)}


@router.delete("/speaker-profiles/{profile_id}")
async def delete_speaker_profile(
    profile_id: str,
    repository: Repository = Depends(get_repository),
):
    """Delete an empty local speaker profile."""
    profile = await repository.get_speaker_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Speaker profile not found")
    if getattr(profile, "examples", None):
        raise HTTPException(status_code=409, detail="Speaker profile has saved voice examples")
    deleted = await repository.delete_empty_speaker_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Speaker profile not found")
    return {"success": True}


@router.post("/recordings/{session_id}/transcript-versions/{transcript_version_id}/speaker-profiles/promote")
async def promote_speaker_profile_example(
    session_id: str,
    transcript_version_id: str,
    request: PromoteSpeakerProfileRequest,
    repository: Repository = Depends(get_repository),
):
    """Promote a confirmed speaker cluster into a local speaker profile example."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    transcript_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
    if transcript_version is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")

    cluster = str(request.speaker_cluster or "").strip()
    if not cluster:
        raise HTTPException(status_code=422, detail="speaker_cluster is required")

    requested_profile_id = str(request.speaker_profile_id or "").strip() or None
    requested_display_name = " ".join(str(request.display_name or "").strip().split()) or None
    if requested_profile_id:
        profile = await repository.get_speaker_profile(requested_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Speaker profile not found")
    elif not requested_display_name:
        raise HTTPException(status_code=422, detail="display_name or speaker_profile_id is required")
    else:
        profile = await repository.get_speaker_profile_by_name(requested_display_name)

    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
    )
    cluster_segments = [
        segment
        for segment in segments
        if (getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)) == cluster
    ]
    if not cluster_segments:
        raise HTTPException(status_code=404, detail="Speaker cluster not found")

    existing_examples = []
    if profile is not None:
        existing_examples = await repository.list_speaker_profile_examples(profile.id)
        if len(existing_examples) >= MAX_PROFILE_EXAMPLES:
            raise HTTPException(
                status_code=409,
                detail=f"This profile already has the maximum of {MAX_PROFILE_EXAMPLES} saved examples.",
            )

    try:
        async with _SPEAKER_PROFILE_SAVE_SEMAPHORE:
            payload = await asyncio.to_thread(
                build_profile_example,
                audio_path=str(audio_path),
                segments=cluster_segments,
                hf_token=get_settings().hf_token,
                existing_examples=existing_examples,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to build speaker profile example: {exc}") from exc

    if not payload or not payload.get("embedding") or payload.get("segment") is None:
        raise HTTPException(status_code=409, detail="No new usable voice example was found for this speaker.")

    if profile is None:
        profile = await repository.create_speaker_profile(display_name=requested_display_name or "")
        existing_examples = []

    if len(existing_examples) >= MAX_PROFILE_EXAMPLES:
        raise HTTPException(
            status_code=409,
            detail=f"This profile already has the maximum of {MAX_PROFILE_EXAMPLES} saved examples.",
        )

    chosen_segment = payload["segment"]
    start_time = float(getattr(chosen_segment, "start_time", 0.0))
    end_time = float(getattr(chosen_segment, "end_time", 0.0))
    example = await repository.add_speaker_profile_example(
        speaker_profile_id=profile.id,
        session_id=session_id,
        transcript_version_id=transcript_version_id,
        speaker_cluster=cluster,
        clip_start_seconds=start_time,
        clip_end_seconds=end_time,
        duration_seconds=max(0.0, end_time - start_time),
        source_type="manual_promoted_example",
        embedding_model=get_embedding_model_name(),
        embedding_vector_json=embedding_to_json(payload["embedding"]),
    )
    return {
        "success": True,
        "profile": _serialize_speaker_profile(await repository.get_speaker_profile(profile.id)),
        "example_id": str(example.id),
        "saved_segment": {
            "start_time": start_time,
            "end_time": end_time,
        },
    }


@router.post("/recordings/{session_id}/transcript-versions/{transcript_version_id}/speaker-profiles/correct-match")
async def correct_speaker_profile_match(
    session_id: str,
    transcript_version_id: str,
    request: CorrectSpeakerProfileMatchRequest,
    repository: Repository = Depends(get_repository),
):
    """Correct a matched speaker profile for the current transcript version."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    transcript_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
    if transcript_version is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")

    speaker_cluster = str(request.speaker_cluster or "").strip()
    if not speaker_cluster:
        raise HTTPException(status_code=422, detail="speaker_cluster is required")

    accepted_profile_id = str(request.accepted_profile_id or "").strip() or None
    accepted_display_name = " ".join(str(request.accepted_display_name or "").strip().split()) or None
    if bool(accepted_profile_id) == bool(accepted_display_name):
        raise HTTPException(status_code=422, detail="Provide exactly one accepted profile target")

    if accepted_profile_id:
        profile = await repository.get_speaker_profile(accepted_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Speaker profile not found")
    else:
        profile = await repository.get_speaker_profile_by_name(accepted_display_name or "")
        if profile is None:
            profile = await repository.create_speaker_profile(display_name=accepted_display_name or "")

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
    )
    cluster_segments = [
        segment
        for segment in segments
        if (getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)) == speaker_cluster
    ]
    if not cluster_segments:
        raise HTTPException(status_code=404, detail="Speaker cluster not found")

    await repository.set_transcript_speaker_profile_override(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
        speaker_cluster=speaker_cluster,
        speaker_profile_id=str(profile.id),
    )
    await repository.update_transcript_version_cluster_speaker_name(
        transcript_version_id=transcript_version_id,
        speaker_cluster=speaker_cluster,
        speaker_name=str(profile.display_name),
    )

    warning = None
    example_saved = False
    example = None
    saved_segment = None

    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)

    if not audio_path:
        warning = "Changed match, but recording audio was unavailable so no new voice example was saved."
    else:
        existing_examples = await repository.list_speaker_profile_examples(str(profile.id))
        if len(existing_examples) >= MAX_PROFILE_EXAMPLES:
            warning = f"Changed match, but {profile.display_name} already has the maximum of {MAX_PROFILE_EXAMPLES} saved examples."
        else:
            try:
                async with _SPEAKER_PROFILE_SAVE_SEMAPHORE:
                    payload = await asyncio.to_thread(
                        build_profile_example,
                        audio_path=str(audio_path),
                        segments=cluster_segments,
                        hf_token=get_settings().hf_token,
                        existing_examples=existing_examples,
                    )
            except Exception as exc:
                payload = None
                warning = f"Changed match, but Sidekick could not save a new voice example: {exc}"
            if payload and payload.get("embedding") and payload.get("segment") is not None:
                chosen_segment = payload["segment"]
                start_time = float(getattr(chosen_segment, "start_time", 0.0))
                end_time = float(getattr(chosen_segment, "end_time", 0.0))
                example = await repository.add_speaker_profile_example(
                    speaker_profile_id=str(profile.id),
                    session_id=session_id,
                    transcript_version_id=transcript_version_id,
                    speaker_cluster=speaker_cluster,
                    clip_start_seconds=start_time,
                    clip_end_seconds=end_time,
                    duration_seconds=max(0.0, end_time - start_time),
                    source_type="manual_match_correction",
                    embedding_model=get_embedding_model_name(),
                    embedding_vector_json=embedding_to_json(payload["embedding"]),
                )
                example_saved = True
                saved_segment = {
                    "start_time": start_time,
                    "end_time": end_time,
                }
            elif warning is None:
                warning = "Changed match, but no new usable voice example was available to improve future matching."

    updated_profile = await repository.get_speaker_profile(str(profile.id))
    speaker_profiles = await _list_speaker_profiles_safe(repository)
    speaker_profile_overrides = await _list_transcript_speaker_profile_overrides_safe(
        repository,
        transcript_version_id,
    )
    refreshed_segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=transcript_version_id,
    )
    await _sync_transcript_speaker_review_state(
        repository,
        transcript_version_id,
        refreshed_segments,
    )
    speaker_card = next(
        (
            card
            for card in _build_speaker_cards(
                refreshed_segments,
                session_id,
                transcript_version_id,
                speaker_profiles=speaker_profiles,
                speaker_profile_overrides=speaker_profile_overrides,
            )
            if card["speaker_cluster"] == speaker_cluster
        ),
        None,
    )
    return {
        "success": True,
        "profile": _serialize_speaker_profile(updated_profile),
        "override": {
            "speaker_cluster": speaker_cluster,
            "speaker_profile_id": str(updated_profile.id),
            "speaker_profile_name": str(updated_profile.display_name),
        },
        "example_saved": example_saved,
        "example_id": str(example.id) if example else None,
        "saved_segment": saved_segment,
        "warning": warning,
        "speaker_card": speaker_card,
    }


@router.get("/speaker-profiles/{profile_id}/examples")
async def get_speaker_profile_examples(
    profile_id: str,
    repository: Repository = Depends(get_repository),
):
    """List saved speaker examples for one profile."""
    profile = await repository.get_speaker_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Speaker profile not found")

    examples = await repository.list_speaker_profile_examples(profile_id)
    serialized_examples = []
    for example in examples:
        meeting = await repository.get_primary_meeting(str(example.session_id), create_if_missing=False)
        serialized_examples.append(
            _serialize_speaker_profile_example(
                example,
                recording_title=getattr(meeting, "title", None),
            )
        )

    return {
        "profile": _serialize_speaker_profile(profile),
        "examples": serialized_examples,
    }


@router.delete("/speaker-profile-examples/{example_id}")
async def delete_speaker_profile_example(
    example_id: str,
    repository: Repository = Depends(get_repository),
):
    """Delete one saved speaker profile example."""
    example = await repository.get_speaker_profile_example(example_id)
    if example is None:
        raise HTTPException(status_code=404, detail="Speaker profile example not found")
    deleted = await repository.delete_speaker_profile_example(example_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Speaker profile example not found")
    return {"success": True}


@router.get("/speaker-profile-examples/{example_id}/audio")
async def get_speaker_profile_example_audio(
    example_id: str,
    repository: Repository = Depends(get_repository),
):
    """Play one saved speaker profile example clip."""
    example = await repository.get_speaker_profile_example(example_id)
    if example is None:
        raise HTTPException(status_code=404, detail="Speaker profile example not found")

    session = await repository.get_session(str(example.session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    audio_path = get_session_audio_path(str(example.session_id))
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(str(example.session_id))
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    try:
        clip_path = ensure_speaker_clip(
            audio_path=audio_path,
            session_id=str(example.session_id),
            transcript_version_id=str(getattr(example, "transcript_version_id", None) or "profile-examples"),
            speaker_key=f"profile-example-{example.id}",
            start_time=float(getattr(example, "clip_start_seconds", 0.0) or 0.0),
            end_time=float(getattr(example, "clip_end_seconds", 0.0) or 0.0),
        )
    except Exception as exc:
        logger.warning("speaker_profile_example_clip: generation failed | example=%s error=%s", example_id, exc)
        raise HTTPException(status_code=500, detail="Failed to generate voice example clip")

    return FileResponse(path=clip_path, media_type="audio/wav")


@router.post("/recordings/{session_id}/speaker-detection-job", response_model=SpeakerDetectionJobCreateResponse)
async def start_speaker_detection_job(
    session_id: str,
    request: StartSpeakerDetectionJobRequest,
    repository: Repository = Depends(get_repository),
):
    """Start a diarization-only speaker-detection rerun using stored transcript timing."""
    await _require_speaker_repair_enabled(repository)
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    source_version = None
    if request.source_transcript_version_id:
        source_version = await repository.get_transcript_version_for_session(session_id, request.source_transcript_version_id)
    if source_version is None:
        source_version = await repository.get_latest_transcript_version(session_id)
    if source_version is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")

    expected_speaker_count = request.expected_speaker_count
    if expected_speaker_count is not None and (expected_speaker_count < 2 or expected_speaker_count > 10):
        raise HTTPException(status_code=422, detail="expected_speaker_count must be between 2 and 10")

    versions = await repository.ensure_transcript_versions(session_id)
    next_version_number = max((int(version.version_number) for version in versions), default=0) + 1
    target_version = await repository.create_transcript_version(
        session_id=session_id,
        meeting_id=source_version.meeting_id,
        version_number=next_version_number,
        parent_version_id=source_version.id,
        status="processing",
        source_type="speaker_detection_rerun",
        transcription_backend=source_version.transcription_backend,
        transcription_model=source_version.transcription_model,
        diarization_backend="pyannote",
        diarization_model=get_settings().hf_token and "pyannote/speaker-diarization-community-1" or None,
        diarization_expected_speaker_count=expected_speaker_count,
        diarization_repair_source_version_id=source_version.id,
        repair_strategy="full_file_exact_count" if expected_speaker_count is not None else "full_file_unconstrained",
        repair_reason="missing_speaker" if expected_speaker_count is not None else None,
        template_key=source_version.template_key,
        custom_prompt=source_version.custom_prompt,
        speaker_review_required=True,
        speaker_review_completed_at=None,
    )
    job = _create_speaker_detection_job(
        session_id,
        transcript_version_id=str(target_version.id),
        transcript_version_number=next_version_number,
    )
    _SPEAKER_DETECTION_TASKS[job["job_id"]] = asyncio.create_task(
        _run_speaker_detection_job(
            job_id=job["job_id"],
            session_id=session_id,
            source_transcript_version_id=str(source_version.id),
            target_transcript_version_id=str(target_version.id),
            expected_speaker_count=expected_speaker_count,
            repository=repository,
        )
    )
    return SpeakerDetectionJobCreateResponse(
        job_id=job["job_id"],
        status=job["status"],
        poll_url=f"/api/speaker-detection-jobs/{job['job_id']}",
        transcript_version_id=str(target_version.id),
        transcript_version_number=next_version_number,
    )


@router.get("/speaker-detection-jobs/{job_id}", response_model=SpeakerDetectionJobStatus)
async def get_speaker_detection_job(job_id: str):
    """Return speaker-detection job status."""
    job = _SPEAKER_DETECTION_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Speaker detection job not found")
    return SpeakerDetectionJobStatus(**job)


@router.post("/recordings/{session_id}/chat/messages")
async def post_workspace_chat_message(
    session_id: str,
    request: WorkspaceChatMessageRequest,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Append a grounded chat turn for the current recording workspace."""
    await _require_workspace_chat_enabled(repository)
    content = (request.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message cannot be empty")

    (
        session,
        meeting,
        transcript_version,
        selected_summary,
        saved_summaries,
        _draft_summary,
    ) = await _resolve_workspace_chat_context(
        session_id,
        transcript_version_id=request.transcript_version_id,
        summary_id=request.summary_id,
        repository=repository,
    )
    chat_service = WorkspaceChatService(repository, summarization_manager)
    user_message, assistant_message = await chat_service.send_message(
        session=session,
        meeting=meeting,
        transcript_version=transcript_version,
        current_summary=selected_summary,
        content=content,
        context_event_text=_workspace_chat_context_text(
            transcript_version,
            selected_summary,
            saved_summaries,
        ),
    )
    return {
        "user_message": _serialize_chat_message(user_message, saved_summaries),
        "assistant_message": _serialize_chat_message(assistant_message, saved_summaries),
    }


@router.post("/recordings/{session_id}/chat/messages/{message_id}/apply")
async def apply_workspace_chat_message(
    session_id: str,
    message_id: str,
    request: WorkspaceChatApplyRequest,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Apply an assistant chat turn to the current summary draft context."""
    await _require_workspace_chat_enabled(repository)
    (
        session,
        meeting,
        transcript_version,
        selected_summary,
        saved_summaries,
        _draft_summary,
    ) = await _resolve_workspace_chat_context(
        session_id,
        transcript_version_id=request.transcript_version_id,
        summary_id=request.summary_id,
        repository=repository,
    )
    if selected_summary is None:
        raise HTTPException(status_code=404, detail="No summary available to apply chat changes")

    assistant_message = await repository.get_workspace_chat_message(message_id)
    if assistant_message is None or assistant_message.role != "assistant":
        raise HTTPException(status_code=404, detail="Assistant chat message not found")
    if str(assistant_message.session_id) != str(session_id):
        raise HTTPException(status_code=404, detail="Assistant chat message not found for this recording")
    if assistant_message.message_type != "assistant_answer":
        raise HTTPException(status_code=400, detail="Only assistant answer turns can be applied")
    if not assistant_message.apply_ready:
        raise HTTPException(status_code=400, detail="This assistant turn is not ready to apply")

    chat_service = WorkspaceChatService(repository, summarization_manager)
    result = await chat_service.apply_message_to_summary(
        session=session,
        meeting=meeting,
        transcript_version=transcript_version,
        assistant_message=assistant_message,
        base_summary=selected_summary,
        context_event_text=_workspace_chat_context_text(
            transcript_version,
            selected_summary,
            saved_summaries,
        ),
    )
    return {
        "changed": result["changed"],
        "reason": result["reason"],
        "draft_summary": (
            _serialize_summary(result["draft_summary"], transcript_version)
            if result["draft_summary"] is not None
            else None
        ),
        "system_message": (
            _serialize_chat_message(result["system_message"], saved_summaries)
            if result.get("system_message") is not None
            else None
        ),
    }


@router.get("/ai-feedback/summary-patterns")
async def get_workspace_chat_feedback_patterns(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    template_key: Optional[str] = None,
    applied_only: bool = False,
    intent_label: Optional[str] = None,
    repository: Repository = Depends(get_repository),
):
    """Return aggregate prompt-improvement counts from workspace assistant history."""
    await _require_workspace_chat_enabled(repository)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    return await repository.aggregate_workspace_chat_feedback(
        date_from=date_from,
        date_to=date_to,
        template_key=normalize_template_key(template_key) if template_key else None,
        applied_only=applied_only,
        intent_label=intent_label,
    )


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

    provided_fields = getattr(request, "model_fields_set", set())

    normalized_title = UNSET
    if "title" in provided_fields:
        normalized_title = request.title.strip() or None if request.title is not None else None

    normalized_template_key = UNSET
    if "template_key" in provided_fields:
        normalized_template_key = request.template_key

    normalized_custom_prompt = UNSET
    if "custom_prompt" in provided_fields:
        normalized_custom_prompt = (
            request.custom_prompt.strip() or None
            if request.custom_prompt is not None
            else None
        )

    if request.transcript_version_id:
        version = await repository.get_transcript_version_for_session(session_id, request.transcript_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    else:
        version = await repository.get_latest_transcript_version(session_id)

    if version:
        updated = await repository.update_meeting_settings(
            meeting.id,
            title=normalized_title,
        )
        version = await repository.update_transcript_version(
            version.id,
            template_key=normalized_template_key if "template_key" in provided_fields else UNSET,
            custom_prompt=normalized_custom_prompt if "custom_prompt" in provided_fields else UNSET,
        )
    else:
        updated = await repository.update_meeting_settings(
            meeting.id,
            title=normalized_title,
            template_key=normalized_template_key,
            custom_prompt=normalized_custom_prompt,
        )
    return {
        "success": True,
        "meeting_id": updated.id,
        "title": updated.title,
        "template_key": (version.template_key if version else updated.template_key),
        "custom_prompt": (version.custom_prompt if version else updated.custom_prompt),
    }


@router.get("/recordings/{session_id}/speakers")
async def get_recording_speakers(
    session_id: str,
    transcript_version_id: Optional[str] = None,
    repository: Repository = Depends(get_repository),
):
    """Return speaker cards for the workspace speaker-review step."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")
    versions = await repository.ensure_transcript_versions(session_id)
    active_version = None
    if transcript_version_id:
        active_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
        if active_version is None:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    if active_version is None and versions:
        active_version = await repository.get_latest_transcript_version(session_id)
    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id if active_version else None,
    )
    speaker_profiles = await _list_speaker_profiles_safe(repository)
    speaker_profile_overrides = await _list_transcript_speaker_profile_overrides_safe(
        repository,
        active_version.id if active_version else None,
    )
    return {
        "audio_url": f"/api/recordings/{session_id}/audio",
        "speakers": _build_speaker_cards(
            segments,
            session_id,
            active_version.id if active_version else None,
            speaker_profiles=speaker_profiles,
            speaker_profile_overrides=speaker_profile_overrides,
        ),
        "known_profiles": [_serialize_speaker_profile(profile) for profile in speaker_profiles],
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

    versions = await repository.ensure_transcript_versions(session_id)
    active_version = None
    if request.transcript_version_id:
        active_version = await repository.get_transcript_version_for_session(session_id, request.transcript_version_id)
    if active_version is None and versions:
        active_version = await repository.get_latest_transcript_version(session_id)
    if not active_version:
        raise HTTPException(status_code=404, detail="Transcript version not found")

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id,
    )
    speaker_profiles = await _list_speaker_profiles_safe(repository)
    profiles_by_name = _speaker_profiles_by_name(speaker_profiles)
    updates: dict[str, str | None] = {}
    cluster_assignments: dict[str, str] = {}
    for segment in segments:
        speaker_cluster = speaker_identity(segment)
        if not speaker_cluster:
            continue
        mapped_name = request.assignments.get(speaker_cluster)
        if mapped_name and mapped_name.strip():
            cleaned_name = mapped_name.strip()
            updates[segment.id] = cleaned_name
            cluster_assignments[speaker_cluster] = cleaned_name

    if updates:
        await repository.update_segments_speakers(updates)
        for speaker_cluster, speaker_name in cluster_assignments.items():
            matched_profile = profiles_by_name.get(_normalize_profile_name(speaker_name))
            if matched_profile is not None:
                await repository.set_transcript_speaker_profile_override(
                    session_id=session_id,
                    transcript_version_id=active_version.id,
                    speaker_cluster=speaker_cluster,
                    speaker_profile_id=str(matched_profile.id),
                )
            else:
                await repository.delete_transcript_speaker_profile_override(
                    active_version.id,
                    speaker_cluster,
                )

    refreshed_segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id,
    )
    await _sync_transcript_speaker_review_state(
        repository,
        active_version.id,
        refreshed_segments,
    )
    return {"success": True, "updated": len(updates)}


@router.post("/recordings/{session_id}/transcript-versions/{transcript_version_id}/speaker-clusters/merge")
async def merge_speaker_clusters(
    session_id: str,
    transcript_version_id: str,
    request: MergeSpeakerClustersRequest,
    repository: Repository = Depends(get_repository),
):
    """Create a new transcript version that merges one speaker cluster into another."""
    await _require_speaker_repair_enabled(repository)

    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    active_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
    if active_version is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")

    source_cluster = str(request.source_speaker_cluster or "").strip()
    target_cluster = str(request.target_speaker_cluster or "").strip()
    if not source_cluster or not target_cluster:
        raise HTTPException(status_code=422, detail="Both source and target speaker clusters are required")
    if source_cluster == target_cluster:
        raise HTTPException(status_code=422, detail="Source and target speaker clusters must be different")

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id,
    )
    available_clusters = {
        speaker_identity(segment)
        for segment in segments
        if speaker_identity(segment)
    }
    if source_cluster not in available_clusters or target_cluster not in available_clusters:
        raise HTTPException(status_code=404, detail="Speaker cluster not found")

    versions = await repository.ensure_transcript_versions(session_id)
    next_version_number = (max((int(version.version_number) for version in versions), default=0) + 1)
    merged_version = await repository.create_transcript_version(
        session_id=session_id,
        meeting_id=active_version.meeting_id,
        version_number=next_version_number,
        parent_version_id=active_version.id,
        status="processing",
        source_type="speaker_cluster_merge",
        transcription_backend=active_version.transcription_backend,
        transcription_model=active_version.transcription_model,
        diarization_backend=active_version.diarization_backend,
        diarization_model=active_version.diarization_model,
        diarization_expected_speaker_count=active_version.diarization_expected_speaker_count,
        diarization_late_join_offset_seconds=active_version.diarization_late_join_offset_seconds,
        diarization_repair_source_version_id=active_version.id,
        repair_strategy="manual_cluster_merge",
        repair_reason="split_speaker",
        template_key=active_version.template_key,
        custom_prompt=active_version.custom_prompt,
        speaker_review_required=True,
        speaker_review_completed_at=None,
    )

    cloned_count = await repository.clone_transcript_version_segments(
        source_transcript_version_id=active_version.id,
        target_transcript_version_id=merged_version.id,
        speaker_cluster_rewrites={source_cluster: target_cluster},
    )
    if cloned_count == 0:
        await repository.update_transcript_version(merged_version.id, status="failed")
        raise HTTPException(status_code=400, detail="No transcript segments found to merge")

    merged_segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=merged_version.id,
    )
    merged_metrics = speaker_assignment_metrics(merged_segments)
    merged_version = await repository.update_transcript_version(
        merged_version.id,
        status="ready",
        **speaker_review_update_fields(merged_segments),
        repair_strategy="manual_cluster_merge",
        diarization_actual_speaker_count=merged_metrics["actual_speaker_count"],
        diarization_unassigned_segment_count=merged_metrics["unassigned_segment_count"],
        diarization_unassigned_segment_ratio=merged_metrics["unassigned_segment_ratio"],
        repair_quality_gate_passed=True,
    )
    return {
        "success": True,
        "transcript_version": _serialize_transcript_version(
            merged_version,
            latest_version_id=str(merged_version.id),
        ),
    }


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
        attendees_snapshot=None,
    )

    # Best-effort vault write
    settings = get_settings()
    if settings.obsidian_vault_path:
        transcript_version_id = (
            getattr(original_summary, "transcript_version_id", None)
            if original_summary
            else None
        )
        if not transcript_version_id:
            transcript_version = await repository.get_latest_transcript_version(session_id)
            transcript_version_id = str(transcript_version.id) if transcript_version else None
        from src.api.routes.export import (
            _archive_previous_export_if_needed,
            _build_summary_save_params,
            _write_obsidian_file,
        )
        params = await _build_summary_save_params(
            repository,
            session,
            primary_meeting,
            transcript_version_id=transcript_version_id,
            summary_id=summary.id,
            summary_content=request.content,
            template_label="Refined",
            processing_duration_seconds=(
                original_summary.processing_duration_seconds if original_summary else None
            ),
            pass1_system_prompt=original_summary.pass1_system_prompt if original_summary else None,
            pass1_user_prompt=original_summary.pass1_user_prompt if original_summary else None,
            pass2_system_prompt=original_summary.pass2_system_prompt if original_summary else None,
            pass2_user_prompt=original_summary.pass2_user_prompt if original_summary else None,
            workflow_data_json=getattr(original_summary, "workflow_data_json", None) if original_summary else None,
        )
        local_exported_at = localize_datetime(
            datetime.now(timezone.utc),
            session.timezone_name,
            session.timezone_offset_minutes,
        )
        exported_at = format_datetime_human(local_exported_at, params["tz_label"])
        frontmatter = dict(params.get("frontmatter") or {})
        frontmatter["exported_at"] = local_exported_at.isoformat()
        markdown_content = build_obsidian_markdown(
            content=request.content,
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
            revision_instruction=request.revision_instruction,
        )

        obsidian_uri = None
        try:
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
            await repository.update_summary(
                summary.id,
                saved_to_obsidian_at=datetime.utcnow(),
                obsidian_relative_path=params["relative_path"],
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
    cleanup_speaker_clip_cache(session_id)
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

    latest_version = await repository.get_latest_transcript_version(session_id)

    # Get segments for this session
    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=latest_version.id if latest_version else None,
    )

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
    lifecycle = _serialize_recording_lifecycle(session, audio_path=audio_path)

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
        **lifecycle,
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

    This is the authoritative fallback upload path. It overwrites any existing
    finalized audio file, but retained chunk storage is preserved so the
    original chunk set remains recoverable.
    """
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Audio payload is empty")

    requested_extension = normalize_audio_extension(
        request.headers.get("x-upload-extension"),
        default="",
    )
    extension = requested_extension or extension_from_content_type(
        request.headers.get("content-type", "")
    )

    audio_dir = get_audio_dir()
    cleanup_speaker_clip_cache(session_id)
    # Remove any existing finalized audio
    for existing in get_session_audio_candidates(session_id):
        existing.unlink()

    audio_path = audio_dir / f"{session_id}.{extension}"
    audio_path.write_bytes(body)
    await repository.update_session_recording_state(
        session_id,
        audio_status="uploaded",
        audio_error=None,
    )

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

    extension = extension_from_content_type(request.headers.get("content-type", ""))

    # Store chunk (idempotent - skips if same size already exists)
    chunk_path = write_chunk(session_id, client_id, chunk_index, body)
    _merge_chunk_meta(
        session_id,
        client_id=client_id,
        extension=extension,
    )
    await repository.update_session_recording_state(
        session_id,
        recording_status="recording" if session.is_active else session.recording_status,
        audio_status="chunking",
        audio_error=None,
    )

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
        await repository.update_session_recording_state(
            session_id,
            audio_status="finalized",
            audio_error=None,
        )
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

    final_path, missing = await _finalize_chunked_recording_audio(
        session_id=session_id,
        client_id=client_id,
        mime_type=body.mime_type,
        expected_chunks=expected_count,
    )
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Incomplete chunks: missing {len(missing)} of {expected_count}",
            headers={"X-Missing-Chunks": ",".join(str(i) for i in missing[:20])},
        )
    await repository.update_session_recording_state(
        session_id,
        audio_status="finalized",
        audio_error=None,
    )

    return {
        "status": "finalized",
        "session_id": session_id,
        "chunks": expected_count,
        "bytes": final_path.stat().st_size,
        "audio_url": f"/api/recordings/{session_id}/audio",
    }


@router.post("/recordings/{session_id}/complete")
async def complete_recording(
    session_id: str,
    body: CompleteRecordingRequest,
    repository: Repository = Depends(get_repository),
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Finalize a recording and mark it ready for workspace review."""
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    client_id = (body.client_id or "").strip() or None
    expected_chunks = int(body.expected_chunks or 0) or None
    logger.info(
        "recording_complete:start session_id=%s client_id=%s expected_chunks=%s allow_fallback_blob=%s",
        session_id,
        client_id,
        expected_chunks,
        body.allow_fallback_blob,
    )

    audio_path = get_session_audio_path(session_id)
    lifecycle = _serialize_recording_lifecycle(session, audio_path=audio_path)
    if lifecycle["workspace_ready"]:
        return {
            "session_id": session_id,
            **lifecycle,
            "has_audio": True,
            "audio_url": f"/api/recordings/{session_id}/audio",
            "missing_chunks": [],
            "reason": None,
            "recoverable_from_chunks": True,
            "chunk_summary": _serialize_chunk_summary(None),
        }

    await repository.get_primary_meeting(session_id, create_if_missing=True)
    await repository.update_session_recording_state(
        session_id,
        recording_status="finalizing",
        audio_status="uploaded" if audio_path else "chunking",
        audio_error=None,
    )

    try:
        if not audio_path:
            audio_path, chunk_summary = await _recover_recording_audio(
                session_id=session_id,
                client_id=client_id,
                mime_type=body.mime_type,
                expected_chunks=expected_chunks,
            )
            missing_chunks = list((chunk_summary or {}).get("missing_indices") or [])

            if not audio_path:
                session = await repository.update_session_recording_state(
                    session_id,
                    recording_status="finalizing",
                    audio_status="chunking",
                    audio_error=None,
                )
                lifecycle = _serialize_recording_lifecycle(session, audio_path=None)
                return {
                    "session_id": session_id,
                    **lifecycle,
                    "has_audio": False,
                    "audio_url": None,
                    "missing_chunks": missing_chunks,
                    "reason": (
                        "missing_chunks" if missing_chunks else "audio_not_uploaded"
                    ),
                    "recoverable_from_chunks": bool(
                        chunk_summary and chunk_summary.get("available_count")
                    ),
                    "chunk_summary": _serialize_chunk_summary(chunk_summary),
                }

        await repository.update_session_recording_state(
            session_id,
            audio_status="finalized",
            audio_error=None,
        )
        await session_manager.end_session_by_id(session_id)
        session = await repository.mark_session_recording_ready(session_id)
        audio_path = get_session_audio_path(session_id)
        lifecycle = _serialize_recording_lifecycle(session, audio_path=audio_path)
        return {
            "session_id": session_id,
            **lifecycle,
            "has_audio": audio_path is not None,
            "audio_url": f"/api/recordings/{session_id}/audio" if audio_path else None,
            "missing_chunks": [],
            "reason": None,
            "recoverable_from_chunks": True,
            "chunk_summary": _serialize_chunk_summary(
                get_chunk_upload_summary(session_id, client_id, expected_chunks)
                if client_id
                else None
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("recording completion failed", extra={"session_id": session_id})
        session = await repository.mark_session_recording_failed(
            session_id,
            str(exc) or "Failed to finalize recording",
        )
        lifecycle = _serialize_recording_lifecycle(session, audio_path=get_session_audio_path(session_id))
        raise HTTPException(
            status_code=500,
            detail=lifecycle["audio_error"] or "Failed to finalize recording",
        ) from exc


@router.post("/recordings/{session_id}/recover-audio")
async def recover_recording_audio(
    session_id: str,
    body: CompleteRecordingRequest,
    repository: Repository = Depends(get_repository),
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Attempt to recover/finalize recording audio from retained chunks."""
    payload = await complete_recording(
        session_id,
        body,
        repository=repository,
        session_manager=session_manager,
    )
    payload["recovered"] = bool(payload.get("has_audio"))
    return payload


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
    clip_url: str


class SpeakerClipsResponse(BaseModel):
    clips: list[SpeakerClip]
    audio_url: str


@router.get("/recordings/{session_id}/speaker-clips", response_model=SpeakerClipsResponse)
async def get_speaker_clips(
    session_id: str,
    transcript_version_id: Optional[str] = None,
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

    active_version = None
    if transcript_version_id:
        active_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
        if active_version is None:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    if active_version is None:
        active_version = await repository.get_latest_transcript_version(session_id)

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id if active_version else None,
    )
    if not segments:
        raise HTTPException(status_code=404, detail="No transcript segments found")

    speaker_profiles = await _list_speaker_profiles_safe(repository)
    speaker_profile_overrides = await _list_transcript_speaker_profile_overrides_safe(
        repository,
        active_version.id if active_version else None,
    )
    speaker_cards = _build_speaker_cards(
        segments,
        session_id,
        active_version.id if active_version else None,
        speaker_profiles=speaker_profiles,
        speaker_profile_overrides=speaker_profile_overrides,
    )
    if not speaker_cards:
        raise HTTPException(status_code=400, detail="No speakers found in transcript. Run diarization first.")

    clips = [
        SpeakerClip(
            speaker=card["display_name"] or card["speaker_cluster"],
            start_time=float(card["clip_start"]),
            end_time=float(card["clip_end"]),
            text=str(card["preview_text"])[:100],
            clip_url=card["clip_url"],
        )
        for card in speaker_cards
    ]

    return SpeakerClipsResponse(
        clips=clips,
        audio_url=f"/api/recordings/{session_id}/audio",
    )


@router.get("/recordings/{session_id}/speaker-clips/{speaker_key}/audio")
async def get_speaker_clip_audio(
    session_id: str,
    speaker_key: str,
    transcript_version_id: Optional[str] = None,
    repository: Repository = Depends(get_repository),
):
    """Return a cached standalone clip for one detected speaker."""
    started_at = perf_counter()
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Recording not found")

    audio_path = get_session_audio_path(session_id)
    if not audio_path and session.ended_at:
        audio_path = ensure_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=404, detail="Recording audio not found")

    active_version = None
    if transcript_version_id:
        active_version = await repository.get_transcript_version_for_session(session_id, transcript_version_id)
        if active_version is None:
            raise HTTPException(status_code=404, detail="Transcript version not found")
    if active_version is None:
        active_version = await repository.get_latest_transcript_version(session_id)

    segments = await repository.get_segments(
        session_id=session_id,
        transcript_version_id=active_version.id if active_version else None,
    )
    if not segments:
        raise HTTPException(status_code=404, detail="No transcript segments found")

    speaker_profiles = await _list_speaker_profiles_safe(repository)
    speaker_profile_overrides = await _list_transcript_speaker_profile_overrides_safe(
        repository,
        active_version.id if active_version else None,
    )
    speaker_card = next(
        (
            card
            for card in _build_speaker_cards(
                segments,
                session_id,
                active_version.id if active_version else None,
                speaker_profiles=speaker_profiles,
                speaker_profile_overrides=speaker_profile_overrides,
            )
            if card["speaker_cluster"] == speaker_key
        ),
        None,
    )
    if not speaker_card:
        raise HTTPException(status_code=404, detail="Speaker clip not found")

    try:
        clip_path = ensure_speaker_clip(
            audio_path=audio_path,
            session_id=session_id,
            transcript_version_id=active_version.id if active_version else None,
            speaker_key=speaker_key,
            start_time=float(speaker_card["clip_start"]),
            end_time=float(speaker_card["clip_end"]),
        )
    except Exception as exc:
        logger.warning(
            "speaker_clip: generation failed | session=%s speaker=%s error=%s",
            session_id,
            speaker_key,
            exc,
        )
        raise HTTPException(status_code=500, detail="Failed to generate speaker clip")

    logger.info(
        "speaker_clip: ready | session=%s speaker=%s transcript_version=%s start=%.2f end=%.2f elapsed_ms=%.1f",
        session_id,
        speaker_key,
        str(active_version.id) if active_version else None,
        float(speaker_card["clip_start"]),
        float(speaker_card["clip_end"]),
        (perf_counter() - started_at) * 1000.0,
    )
    return FileResponse(path=clip_path, media_type="audio/wav")


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
