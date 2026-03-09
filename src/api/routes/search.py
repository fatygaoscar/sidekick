"""Cross-recording transcript search endpoints."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.search.service import RecordingSearchService
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager


router = APIRouter()


def get_repository(request: Request) -> Repository:
    return request.app.state.repository


def get_summarization_manager(request: Request) -> SummarizationManager:
    return request.app.state.summarization_manager


class RecordingSearchRequest(BaseModel):
    """Search request payload."""

    query: str = Field(..., min_length=1, max_length=500)
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    speaker: Optional[str] = Field(default=None, max_length=100)
    limit: int = Field(default=8, ge=1, le=12)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = " ".join((value or "").strip().split())
        if not normalized:
            raise ValueError("Query cannot be empty")
        return normalized

    @field_validator("speaker")
    @classmethod
    def validate_speaker(cls, value: str | None) -> str | None:
        normalized = " ".join((value or "").strip().split())
        return normalized or None


class RecordingSearchResult(BaseModel):
    citation_id: str
    session_id: str
    meeting_id: Optional[str] = None
    recording_title: str
    recorded_at: str
    recorded_date_label: str
    recorded_time_label: str
    speaker: Optional[str] = None
    speaker_cluster: Optional[str] = None
    start_time: float
    end_time: float
    timestamp: str
    snippet: str
    transcript_segment_ids: list[str]
    score: float
    is_cited: bool = False


class RecordingSearchResponse(BaseModel):
    query: str
    answer: Optional[str] = None
    confidence: str = "low"
    results: list[RecordingSearchResult]
    retrieval_count: int


@router.post("/search/recordings", response_model=RecordingSearchResponse)
async def search_recordings(
    payload: RecordingSearchRequest,
    repository: Repository = Depends(get_repository),
    summarization_manager: SummarizationManager = Depends(get_summarization_manager),
):
    """Search across completed recordings using transcript-grounded retrieval."""
    if payload.date_from and payload.date_to and payload.date_from > payload.date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    service = RecordingSearchService(
        repository=repository,
        summarization_manager=summarization_manager,
    )
    result = await service.search(
        query=payload.query,
        date_from=payload.date_from,
        date_to=payload.date_to,
        speaker=payload.speaker,
        limit=payload.limit,
    )
    return RecordingSearchResponse(**result)
