"""SQLAlchemy models for sessions, meetings, and transcripts."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncAttrs, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all models."""

    pass


class Session(Base):
    """Main session tracking."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    mode: Mapped[str] = mapped_column(String(50), nullable=False, default="work")
    submode: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    timezone_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    timezone_offset_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    has_transcription: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_status: Mapped[str] = mapped_column(String(20), nullable=False, default="starting")
    audio_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    audio_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    meetings: Mapped[list["Meeting"]] = relationship(
        "Meeting", back_populates="session", cascade="all, delete-orphan"
    )
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="session", cascade="all, delete-orphan"
    )
    transcript_versions: Mapped[list["TranscriptVersion"]] = relationship(
        "TranscriptVersion", back_populates="session", cascade="all, delete-orphan"
    )
    important_markers: Mapped[list["ImportantMarker"]] = relationship(
        "ImportantMarker", back_populates="session", cascade="all, delete-orphan"
    )
    speaker_profile_overrides: Mapped[list["TranscriptSpeakerProfileOverride"]] = relationship(
        "TranscriptSpeakerProfileOverride", back_populates="session", cascade="all, delete-orphan"
    )
    speaker_profile_examples: Mapped[list["SpeakerProfileExample"]] = relationship(
        "SpeakerProfileExample", back_populates="session", cascade="all, delete-orphan"
    )


class Meeting(Base):
    """Meeting sub-sessions with key start/stop times."""

    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    template_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attendees: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    speaker_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    speaker_review_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    key_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    key_stop: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="meetings")
    summaries: Mapped[list["Summary"]] = relationship(
        "Summary", back_populates="meeting", cascade="all, delete-orphan"
    )
    transcript_versions: Mapped[list["TranscriptVersion"]] = relationship(
        "TranscriptVersion", back_populates="meeting", cascade="all, delete-orphan"
    )


class TranscriptVersion(Base):
    """Versioned transcript workspace state for a recording."""

    __tablename__ = "transcript_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="initial_transcription")
    transcription_backend: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    transcription_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    diarization_backend: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    diarization_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    diarization_expected_speaker_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    diarization_late_join_offset_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    diarization_repair_source_version_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    repair_strategy: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    diarization_actual_speaker_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    diarization_unassigned_segment_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    diarization_unassigned_segment_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    repair_quality_gate_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    repair_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    template_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    speaker_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    speaker_review_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    session: Mapped["Session"] = relationship("Session", back_populates="transcript_versions")
    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="transcript_versions")
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="transcript_version"
    )
    summaries: Mapped[list["Summary"]] = relationship(
        "Summary", back_populates="transcript_version"
    )
    speaker_profile_overrides: Mapped[list["TranscriptSpeakerProfileOverride"]] = relationship(
        "TranscriptSpeakerProfileOverride", back_populates="transcript_version", cascade="all, delete-orphan"
    )
    speaker_profile_examples: Mapped[list["SpeakerProfileExample"]] = relationship(
        "SpeakerProfileExample", back_populates="transcript_version"
    )


class TranscriptSegment(Base):
    """Individual transcription chunks with timestamps."""

    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("meetings.id"), nullable=True
    )
    transcript_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_important: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    speaker: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    speaker_cluster: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="segments")
    transcript_version: Mapped[Optional["TranscriptVersion"]] = relationship(
        "TranscriptVersion", back_populates="segments"
    )


class SpeakerProfile(Base):
    """Local speaker identity profile backed by one or more embedding examples."""

    __tablename__ = "speaker_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    examples: Mapped[list["SpeakerProfileExample"]] = relationship(
        "SpeakerProfileExample", back_populates="speaker_profile", cascade="all, delete-orphan"
    )
    transcript_overrides: Mapped[list["TranscriptSpeakerProfileOverride"]] = relationship(
        "TranscriptSpeakerProfileOverride", back_populates="speaker_profile", cascade="all, delete-orphan"
    )


class TranscriptSpeakerProfileOverride(Base):
    """Explicit profile correction for one speaker cluster in one transcript version."""

    __tablename__ = "transcript_speaker_profile_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    transcript_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=False
    )
    speaker_cluster: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    speaker_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("speaker_profiles.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    session: Mapped["Session"] = relationship("Session", back_populates="speaker_profile_overrides")
    transcript_version: Mapped["TranscriptVersion"] = relationship(
        "TranscriptVersion", back_populates="speaker_profile_overrides"
    )
    speaker_profile: Mapped["SpeakerProfile"] = relationship(
        "SpeakerProfile", back_populates="transcript_overrides"
    )


class SpeakerProfileExample(Base):
    """Confirmed speaker-example clip and embedding for a local speaker profile."""

    __tablename__ = "speaker_profile_examples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    speaker_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("speaker_profiles.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    transcript_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=True
    )
    speaker_cluster: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    clip_start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    clip_end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_assignment")
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_vector_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    speaker_profile: Mapped["SpeakerProfile"] = relationship(
        "SpeakerProfile", back_populates="examples"
    )
    session: Mapped["Session"] = relationship("Session", back_populates="speaker_profile_examples")
    transcript_version: Mapped[Optional["TranscriptVersion"]] = relationship(
        "TranscriptVersion", back_populates="speaker_profile_examples"
    )


class ImportantMarker(Base):
    """Flagged moments with context windows."""

    __tablename__ = "important_markers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("meetings.id"), nullable=True
    )
    marked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=60)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="important_markers")


class Summary(Base):
    """Generated summaries with metadata."""

    __tablename__ = "summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    transcript_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    processing_duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    template: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="saved")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    parent_summary_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("summaries.id"), nullable=True
    )
    template_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pass1_system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pass1_user_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pass2_system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pass2_user_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attendees_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    saved_to_obsidian_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    obsidian_relative_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    workflow_data_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="summaries")
    transcript_version: Mapped[Optional["TranscriptVersion"]] = relationship(
        "TranscriptVersion", back_populates="summaries"
    )


class WorkspaceChatThread(Base):
    """Persistent per-recording chat thread for the workspace assistant."""

    __tablename__ = "workspace_chat_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class WorkspaceChatMessage(Base):
    """Audit trail for workspace assistant turns and system events."""

    __tablename__ = "workspace_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspace_chat_threads.id"), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    transcript_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("transcript_versions.id"), nullable=True
    )
    summary_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("summaries.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retrieval_windows_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    intent_label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    intent_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    suggests_summary_change: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested_change_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    apply_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_summary_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("summaries.id"), nullable=True
    )
    applied_draft_summary_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("summaries.id"), nullable=True
    )
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    template_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppSettings(Base):
    """Singleton global app settings persisted in the database."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    workspace_chat_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speaker_repair_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summarization_backend: Mapped[str] = mapped_column(String(32), default="ollama", nullable=False)
    recording_capture_mode: Mapped[str] = mapped_column(
        String(32), default="whole_room", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class StructuredItem(Base):
    """Structured items extracted from meetings (actions, decisions, risks, etc.)."""

    __tablename__ = "structured_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    meeting_id: Mapped[str] = mapped_column(String(36), ForeignKey("meetings.id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String(10), nullable=False)  # e.g., "A-001"
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)  # action|decision|risk|question|followup
    text: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    due_date: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    blocking: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_timestamp: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mitigation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    who_decides: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    timeline: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db(database_url: str) -> None:
    """Initialize the database and create tables."""
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
