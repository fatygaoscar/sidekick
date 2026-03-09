"""Type definitions for the adaptive multi-stage summarization pipeline."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TranscriptChunk:
    """Backward-compatible chunk representation used by older tests/helpers."""

    index: int
    text: str
    start_time: float
    end_time: float
    start_timestamp: str
    end_timestamp: str


@dataclass
class TranscriptTurn:
    """A normalized transcript turn."""

    turn_id: str
    speaker_raw: Optional[str]
    speaker: Optional[str]
    speaker_cluster: Optional[str]
    start_time: float
    end_time: float
    raw_text: str
    normalized_text: str


@dataclass
class NormalizedEntity:
    """A conservatively normalized entity."""

    entity_id: str
    entity_type: str
    surface_forms: list[str]
    canonical_name: str
    confidence: float
    normalization_source: str = "surface_form"


@dataclass
class EvidenceSpan:
    """Evidence span tied back to transcript turns."""

    turn_ids: list[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    quote: str = ""


@dataclass
class MeetingContextProfile:
    """Internal context inference for adaptive weighting and rendering."""

    ui_summary_mode: str
    primary_mode: str
    secondary_modes: list[str] = field(default_factory=list)
    priority_weights: dict[str, float] = field(default_factory=dict)
    render_profile: str = "default_business_v1"
    mode_confidence: float = 0.0
    reasoning_summary: str = ""


@dataclass
class TopicSegment:
    """A coherent topic segment with thread/re-entry support."""

    topic_id: str
    thread_id: str
    label: str
    topic_type: str
    start_time: float
    end_time: float
    turn_ids: list[str] = field(default_factory=list)
    reentry_index: int = 0
    priority_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ExtractedItem:
    """A candidate or validated extracted item."""

    id: str
    type: str  # decision|proposal|action|constraint|issue|question|observation|direction
    text: str
    speaker: Optional[str] = None
    actors: list[str] = field(default_factory=list)
    owner: Optional[str] = None
    due_date: Optional[str] = None
    blocking: Optional[str] = None
    source_timestamp: Optional[str] = None
    confidence: float = 1.0
    importance: float = 0.5
    outcome_relevance: float = 0.5
    render_score: float = 0.0
    rationale: Optional[str] = None
    impact: Optional[str] = None
    mitigation: Optional[str] = None
    context: Optional[str] = None
    who_decides: Optional[str] = None
    timeline: Optional[str] = None
    status: str = "open"
    topic_id: Optional[str] = None
    thread_id: Optional[str] = None
    topic_key: Optional[str] = None
    evidence: EvidenceSpan = field(default_factory=EvidenceSpan)
    validation_flags: list[str] = field(default_factory=list)
    validated: bool = False
    dropped: bool = False


@dataclass
class StructuredItems:
    """Collection of validated structured items."""

    actions: list[ExtractedItem] = field(default_factory=list)
    decisions: list[ExtractedItem] = field(default_factory=list)
    proposals: list[ExtractedItem] = field(default_factory=list)
    constraints: list[ExtractedItem] = field(default_factory=list)
    issues: list[ExtractedItem] = field(default_factory=list)
    questions: list[ExtractedItem] = field(default_factory=list)
    observations: list[ExtractedItem] = field(default_factory=list)
    directions: list[ExtractedItem] = field(default_factory=list)
    discussion_points: list[ExtractedItem] = field(default_factory=list)
    risks: list[ExtractedItem] = field(default_factory=list)
    followups: list[ExtractedItem] = field(default_factory=list)

    def all_items(self) -> list[ExtractedItem]:
        return (
            self.actions
            + self.decisions
            + self.proposals
            + self.constraints
            + self.issues
            + self.questions
            + self.observations
            + self.directions
            + self.discussion_points
            + self.risks
            + self.followups
        )

    def all_ids(self) -> set[str]:
        return {item.id for item in self.all_items()}


@dataclass
class PipelineResult:
    """Result from the adaptive pipeline."""

    narrative: str
    items: StructuredItems
    participants: list[str]
    template: str
    context_profile: MeetingContextProfile
    topics: list[TopicSegment] = field(default_factory=list)
    turns: list[TranscriptTurn] = field(default_factory=list)
    entities: list[NormalizedEntity] = field(default_factory=list)
    candidate_items: list[ExtractedItem] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    chunk_count: int = 0
    total_items_extracted: int = 0
    items_after_dedup: int = 0
    coverage_score: float = 1.0
    backend: str = ""
    model: str = ""
    narrative_context_mode: str = "adaptive_v1"
    narrative_strategy: str = "stable_render"
    narrative_passes: int = 1
    style_profile: str = "adaptive_constrained_v1"
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    routing_metadata: dict[str, Any] = field(default_factory=dict)

    def workflow_data(self) -> dict[str, Any]:
        def serialize_item(item: ExtractedItem) -> dict[str, Any]:
            return {
                "item_id": item.id,
                "item_type": item.type,
                "text": item.text,
                "speaker": item.speaker,
                "actors": list(item.actors),
                "owner": item.owner,
                "due_date": item.due_date,
                "source_timestamp": item.source_timestamp,
                "confidence": item.confidence,
                "importance": item.importance,
                "outcome_relevance": item.outcome_relevance,
                "render_score": item.render_score,
                "status": item.status,
                "topic_id": item.topic_id,
                "thread_id": item.thread_id,
                "topic_key": item.topic_key,
                "validated": item.validated,
                "dropped": item.dropped,
                "validation_flags": list(item.validation_flags),
                "evidence": {
                    "turn_ids": list(item.evidence.turn_ids),
                    "start_time": item.evidence.start_time,
                    "end_time": item.evidence.end_time,
                    "quote": item.evidence.quote,
                },
            }

        return {
            "context_profile": {
                "ui_summary_mode": self.context_profile.ui_summary_mode,
                "primary_mode": self.context_profile.primary_mode,
                "secondary_modes": list(self.context_profile.secondary_modes),
                "priority_weights": dict(self.context_profile.priority_weights),
                "render_profile": self.context_profile.render_profile,
                "mode_confidence": self.context_profile.mode_confidence,
                "reasoning_summary": self.context_profile.reasoning_summary,
            },
            "topics": [
                {
                    "topic_id": topic.topic_id,
                    "thread_id": topic.thread_id,
                    "label": topic.label,
                    "topic_type": topic.topic_type,
                    "start_time": topic.start_time,
                    "end_time": topic.end_time,
                    "turn_ids": list(topic.turn_ids),
                    "reentry_index": topic.reentry_index,
                    "priority_tags": list(topic.priority_tags),
                    "confidence": topic.confidence,
                }
                for topic in self.topics
            ],
            "validation_warnings": list(self.validation_warnings),
            "timings_ms": dict(self.timings_ms),
            "routing_metadata": dict(self.routing_metadata),
            "candidate_items": [
                serialize_item(item)
                for item in self.candidate_items
            ],
            "validated_items": [
                serialize_item(item)
                for item in self.items.all_items()
            ],
        }
