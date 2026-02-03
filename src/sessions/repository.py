"""Database operations for sessions, meetings, and transcripts."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from .models import Base, ImportantMarker, Meeting, Session, Summary, TranscriptSegment


class Repository:
    """Repository for database operations."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def init_db(self) -> None:
        """Initialize database and create tables."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        """Close database connection."""
        await self._engine.dispose()

    # Session operations
    async def create_session(self, mode: str = "work", submode: str | None = None) -> Session:
        """Create a new session."""
        async with self._session_factory() as db:
            session = Session(mode=mode, submode=submode)
            db.add(session)
            await db.commit()
            await db.refresh(session)
            return session

    async def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(Session)
                .options(selectinload(Session.meetings), selectinload(Session.segments))
                .where(Session.id == session_id)
            )
            return result.scalar_one_or_none()

    async def get_active_session(self) -> Session | None:
        """Get the currently active session."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(Session)
                .options(selectinload(Session.meetings))
                .where(Session.is_active == True)
                .order_by(Session.started_at.desc())
            )
            return result.scalar_one_or_none()

    async def end_session(self, session_id: str) -> Session | None:
        """End a session."""
        async with self._session_factory() as db:
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(is_active=False, ended_at=datetime.utcnow())
            )
            await db.commit()
            return await self.get_session(session_id)

    async def update_session_mode(
        self, session_id: str, mode: str, submode: str | None = None
    ) -> Session | None:
        """Update session mode."""
        async with self._session_factory() as db:
            await db.execute(
                update(Session).where(Session.id == session_id).values(mode=mode, submode=submode)
            )
            await db.commit()
            return await self.get_session(session_id)

    # Meeting operations
    async def create_meeting(
        self, session_id: str, title: str | None = None
    ) -> Meeting:
        """Create a new meeting (Key Start)."""
        async with self._session_factory() as db:
            meeting = Meeting(session_id=session_id, title=title)
            db.add(meeting)
            await db.commit()
            await db.refresh(meeting)
            return meeting

    async def get_meeting(self, meeting_id: str) -> Meeting | None:
        """Get a meeting by ID."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(Meeting)
                .options(selectinload(Meeting.summaries))
                .where(Meeting.id == meeting_id)
            )
            return result.scalar_one_or_none()

    async def get_active_meeting(self, session_id: str) -> Meeting | None:
        """Get the currently active meeting for a session."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(Meeting)
                .where(Meeting.session_id == session_id, Meeting.is_active == True)
                .order_by(Meeting.key_start.desc())
            )
            return result.scalar_one_or_none()

    async def end_meeting(self, meeting_id: str) -> Meeting | None:
        """End a meeting (Key Stop)."""
        async with self._session_factory() as db:
            await db.execute(
                update(Meeting)
                .where(Meeting.id == meeting_id)
                .values(is_active=False, key_stop=datetime.utcnow())
            )
            await db.commit()
            return await self.get_meeting(meeting_id)

    async def update_meeting_title(self, meeting_id: str, title: str) -> Meeting | None:
        """Update meeting title."""
        async with self._session_factory() as db:
            await db.execute(
                update(Meeting).where(Meeting.id == meeting_id).values(title=title)
            )
            await db.commit()
            return await self.get_meeting(meeting_id)

    # Transcript segment operations
    async def add_segment(
        self,
        session_id: str,
        text: str,
        start_time: float,
        end_time: float,
        meeting_id: str | None = None,
        is_important: bool = False,
        confidence: float | None = None,
    ) -> TranscriptSegment:
        """Add a transcript segment."""
        async with self._session_factory() as db:
            segment = TranscriptSegment(
                session_id=session_id,
                meeting_id=meeting_id,
                text=text,
                start_time=start_time,
                end_time=end_time,
                is_important=is_important,
                confidence=confidence,
            )
            db.add(segment)
            await db.commit()
            await db.refresh(segment)
            return segment

    async def get_segments(
        self,
        session_id: str | None = None,
        meeting_id: str | None = None,
        important_only: bool = False,
    ) -> list[TranscriptSegment]:
        """Get transcript segments with optional filters."""
        async with self._session_factory() as db:
            query = select(TranscriptSegment)

            if session_id:
                query = query.where(TranscriptSegment.session_id == session_id)
            if meeting_id:
                query = query.where(TranscriptSegment.meeting_id == meeting_id)
            if important_only:
                query = query.where(TranscriptSegment.is_important == True)

            query = query.order_by(TranscriptSegment.start_time)
            result = await db.execute(query)
            return list(result.scalars().all())

    async def mark_segments_important(
        self, session_id: str, start_time: float, end_time: float
    ) -> int:
        """Mark segments within a time range as important."""
        async with self._session_factory() as db:
            result = await db.execute(
                update(TranscriptSegment)
                .where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.start_time >= start_time,
                    TranscriptSegment.start_time <= end_time,
                )
                .values(is_important=True)
            )
            await db.commit()
            return result.rowcount

    # Important marker operations
    async def add_important_marker(
        self,
        session_id: str,
        meeting_id: str | None = None,
        duration_seconds: int = 60,
        note: str | None = None,
    ) -> ImportantMarker:
        """Add an important marker."""
        async with self._session_factory() as db:
            marker = ImportantMarker(
                session_id=session_id,
                meeting_id=meeting_id,
                duration_seconds=duration_seconds,
                note=note,
            )
            db.add(marker)
            await db.commit()
            await db.refresh(marker)
            return marker

    async def get_important_markers(
        self, session_id: str | None = None, meeting_id: str | None = None
    ) -> list[ImportantMarker]:
        """Get important markers."""
        async with self._session_factory() as db:
            query = select(ImportantMarker)

            if session_id:
                query = query.where(ImportantMarker.session_id == session_id)
            if meeting_id:
                query = query.where(ImportantMarker.meeting_id == meeting_id)

            query = query.order_by(ImportantMarker.marked_at)
            result = await db.execute(query)
            return list(result.scalars().all())

    # Summary operations
    async def add_summary(
        self,
        meeting_id: str,
        content: str,
        backend: str,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> Summary:
        """Add a summary for a meeting."""
        async with self._session_factory() as db:
            summary = Summary(
                meeting_id=meeting_id,
                content=content,
                backend=backend,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            db.add(summary)
            await db.commit()
            await db.refresh(summary)
            return summary

    async def get_summaries(self, meeting_id: str) -> list[Summary]:
        """Get summaries for a meeting."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(Summary)
                .where(Summary.meeting_id == meeting_id)
                .order_by(Summary.created_at.desc())
            )
            return list(result.scalars().all())
