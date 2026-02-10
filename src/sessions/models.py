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
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    meetings: Mapped[list["Meeting"]] = relationship(
        "Meeting", back_populates="session", cascade="all, delete-orphan"
    )
    segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="session", cascade="all, delete-orphan"
    )
    important_markers: Mapped[list["ImportantMarker"]] = relationship(
        "ImportantMarker", back_populates="session", cascade="all, delete-orphan"
    )


class Meeting(Base):
    """Meeting sub-sessions with key start/stop times."""

    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    key_start: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    key_stop: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="meetings")
    summaries: Mapped[list["Summary"]] = relationship(
        "Summary", back_populates="meeting", cascade="all, delete-orphan"
    )


class TranscriptSegment(Base):
    """Individual transcription chunks with timestamps."""

    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False)
    meeting_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("meetings.id"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_important: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="segments")


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
    content: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Relationships
    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="summaries")


async def init_db(database_url: str) -> None:
    """Initialize the database and create tables."""
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
