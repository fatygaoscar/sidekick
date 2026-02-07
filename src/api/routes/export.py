"""Export endpoints for Obsidian integration."""

import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from config.settings import get_settings
from src.sessions.manager import SessionManager
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager
from src.summarization.prompts import TEMPLATE_INFO


router = APIRouter()


def get_session_manager(request: Request) -> SessionManager:
    """Dependency to get session manager."""
    return request.app.state.session_manager


def get_summarization_manager(request: Request) -> SummarizationManager:
    """Dependency to get summarization manager."""
    return request.app.state.summarization_manager


def get_repository(request: Request) -> Repository:
    """Dependency to get repository."""
    return request.app.state.repository


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
    session_manager: SessionManager = Depends(get_session_manager),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
    repository: Repository = Depends(get_repository),
):
    """Export a recording to Obsidian vault with AI summary."""
    settings = get_settings()

    # Get session and its transcript
    session = await repository.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get all segments for the session
    segments = await repository.get_segments(session_id=session_id)
    if not segments:
        raise HTTPException(status_code=400, detail="No transcript available for this session")

    # Build transcript with timestamps
    transcript_lines = []
    for segment in segments:
        mins = int(segment.start_time // 60)
        secs = int(segment.start_time % 60)
        timestamp = f"[{mins:02d}:{secs:02d}]"
        marker = " [IMPORTANT]" if segment.is_important else ""
        transcript_lines.append(f"{timestamp}{marker} {segment.text}")

    full_transcript = "\n".join(transcript_lines)

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
    if segments:
        duration_seconds = int(max(s.end_time for s in segments))
        hours = duration_seconds // 3600
        minutes = (duration_seconds % 3600) // 60
        seconds = duration_seconds % 60
        duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        duration_str = "00:00:00"

    # Build filename: YYYY-MM-DD-HHMM - [title] [Template].md
    timestamp = session.started_at.strftime("%Y-%m-%d-%H%M")
    template_label = TEMPLATE_INFO.get(template, {}).get("name", template.title())
    safe_title = re.sub(r'[<>:"/\\|?*]', '', request.title)  # Remove invalid filename chars
    filename = f"{timestamp} - {safe_title} [{template_label}].md"

    # Build markdown content
    recorded_at = session.started_at.strftime("%Y-%m-%d %H:%M")
    markdown_content = f"""# {timestamp} - {request.title}

**Template**: {template_label}
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
