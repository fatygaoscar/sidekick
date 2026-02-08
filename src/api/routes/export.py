"""Export endpoints for Obsidian integration."""

import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

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


@router.get("/templates")
async def get_templates():
    """Get available summary templates."""
    return {"templates": TEMPLATE_INFO}


@router.post("/recordings/{session_id}/export-obsidian", response_model=ExportResponse)
async def export_to_obsidian(
    session_id: str,
    request: ExportRequest,
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
    repository: Repository = Depends(get_repository),
    transcription_manager: TranscriptionManager = Depends(get_transcription_manager),
):
    """Export a recording to Obsidian vault with AI summary.

    Authoritative export pipeline:
      audio file -> transcription -> transcript segments -> summary -> markdown
    """
    settings = get_settings()

    # Get session and its transcript
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    audio_path = get_session_audio_path(session_id)
    if not audio_path:
        raise HTTPException(status_code=400, detail="No recording audio available for this session")

    # Persist the user-provided title for history/recordings pages.
    # If there is no meeting yet, create one and attach the title.
    meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
    primary_meeting_id = None
    if meetings:
        primary_meeting = meetings[0]
        primary_meeting_id = primary_meeting.id
        if primary_meeting.title != request.title:
            await repository.update_meeting_title(primary_meeting.id, request.title)
    else:
        meeting = await repository.create_meeting(session_id=session.id, title=request.title)
        primary_meeting_id = meeting.id

    # Export transcription pipeline is source of truth:
    # transcribe full recording audio, then replace transcript segments for session.
    try:
        transcription_result, audio_duration_seconds = await transcription_manager.transcribe_file(
            audio_path
        )
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
    template = request.template
    custom_instructions = request.custom_prompt if template == "custom" else None

    try:
        summary_result = await summarization_manager.summarize(
            transcript=full_transcript,
            prompt_type=template,
            custom_instructions=custom_instructions,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {str(e)}")

    # Calculate duration
    duration_seconds = int(audio_duration_seconds)
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    # Build filename: YYYY-MM-DD-HHMM - [title] [Template].md
    timestamp = session.started_at.strftime("%Y-%m-%d-%H%M")
    template_label = TEMPLATE_INFO.get(template, {}).get("name", template.title())
    safe_title = re.sub(r'[<>:"/\\|?*]', '', request.title)  # Remove invalid filename chars
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
    vault_path = Path(settings.obsidian_vault_path)
    if not vault_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Obsidian vault path does not exist: {settings.obsidian_vault_path}"
        )

    filepath = vault_path / filename
    try:
        filepath.write_text(markdown_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")

    # Build Obsidian URI
    # Format: obsidian://open?vault=VaultName&file=filename
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
