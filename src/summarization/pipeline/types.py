"""Type definitions for the multi-stage pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class TranscriptChunk:
    """A time-bounded chunk of transcript for processing."""

    index: int
    text: str
    start_time: float  # seconds
    end_time: float  # seconds
    start_timestamp: str  # formatted [MM:SS]
    end_timestamp: str  # formatted [MM:SS]


@dataclass
class ExtractedItem:
    """An item extracted from the transcript (action, decision, risk, etc.)."""

    id: str  # e.g., "A-001", "D-001", "R-001", "Q-001", "F-001"
    type: str  # action|decision|followup|risk|question
    text: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    blocking: Optional[str] = None
    source_timestamp: Optional[str] = None
    confidence: float = 1.0
    # Additional fields for specific types
    rationale: Optional[str] = None  # For decisions
    impact: Optional[str] = None  # For risks
    mitigation: Optional[str] = None  # For risks
    context: Optional[str] = None  # For questions
    who_decides: Optional[str] = None  # For questions
    timeline: Optional[str] = None  # For follow-ups
    status: str = "open"  # For actions


@dataclass
class StructuredItems:
    """Collection of all extracted and structured items."""

    actions: list[ExtractedItem] = field(default_factory=list)
    decisions: list[ExtractedItem] = field(default_factory=list)
    risks: list[ExtractedItem] = field(default_factory=list)
    questions: list[ExtractedItem] = field(default_factory=list)
    followups: list[ExtractedItem] = field(default_factory=list)

    def all_items(self) -> list[ExtractedItem]:
        """Get all items as a flat list."""
        return self.actions + self.decisions + self.risks + self.questions + self.followups

    def all_ids(self) -> set[str]:
        """Get all item IDs."""
        return {item.id for item in self.all_items()}


@dataclass
class PipelineResult:
    """Result from the full pipeline processing."""

    # Narrative summary text
    narrative: str

    # Structured items
    items: StructuredItems

    # Extracted participant names
    participants: list[str]

    # Template used
    template: str

    # Processing metadata
    chunk_count: int
    total_items_extracted: int
    items_after_dedup: int
    coverage_score: float  # 0-1, how many items are referenced in narrative

    # Backend info
    backend: str
    model: str

    # Token usage (aggregate)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
