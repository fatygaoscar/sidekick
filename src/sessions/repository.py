"""Database operations for sessions, meetings, and transcripts."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from .models import Base, ImportantMarker, Meeting, Session, StructuredItem, Summary, TranscriptSegment
from src.core.datetime_utils import to_utc_iso


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
            await self._ensure_session_timezone_columns(conn)
            await self._ensure_session_transcription_column(conn)
            await self._ensure_transcript_speaker_column(conn)

    async def close(self) -> None:
        """Close database connection."""
        await self._engine.dispose()

    # Session operations
    async def create_session(
        self,
        mode: str = "work",
        submode: str | None = None,
        timezone_name: str | None = None,
        timezone_offset_minutes: int | None = None,
    ) -> Session:
        """Create a new session."""
        async with self._session_factory() as db:
            session = Session(
                mode=mode,
                submode=submode,
                timezone_name=timezone_name,
                timezone_offset_minutes=timezone_offset_minutes,
            )
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

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and all associated data."""
        from sqlalchemy import delete

        async with self._session_factory() as db:
            # Delete summaries for meetings in this session
            meetings = await db.execute(
                select(Meeting).where(Meeting.session_id == session_id)
            )
            for meeting in meetings.scalars().all():
                await db.execute(
                    delete(Summary).where(Summary.meeting_id == meeting.id)
                )

            # Delete related data
            await db.execute(
                delete(ImportantMarker).where(ImportantMarker.session_id == session_id)
            )
            await db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)
            )
            await db.execute(
                delete(Meeting).where(Meeting.session_id == session_id)
            )
            await db.execute(
                delete(Session).where(Session.id == session_id)
            )
            await db.commit()

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

    async def set_session_has_transcription(
        self, session_id: str, has_transcription: bool = True
    ) -> Session | None:
        """Mark whether authoritative transcription has been run for a session."""
        async with self._session_factory() as db:
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(has_transcription=has_transcription)
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
        speaker: str | None = None,
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
                speaker=speaker,
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

    async def delete_segments_for_session(self, session_id: str) -> int:
        """Delete all transcript segments for a session."""
        from sqlalchemy import delete

        async with self._session_factory() as db:
            result = await db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)
            )
            await db.commit()
            return result.rowcount or 0

    async def update_segments_speakers(self, updates: dict[str, str | None]) -> None:
        """Bulk update speaker labels. updates maps segment_id → speaker label."""
        from sqlalchemy import update

        async with self._session_factory() as db:
            for segment_id, speaker in updates.items():
                await db.execute(
                    update(TranscriptSegment)
                    .where(TranscriptSegment.id == segment_id)
                    .values(speaker=speaker)
                )
            await db.commit()

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

    async def get_sessions_list(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Get list of sessions with summary info for recordings page."""
        async with self._session_factory() as db:
            # Get sessions ordered by start time (newest first)
            result = await db.execute(
                select(Session)
                .options(selectinload(Session.meetings))
                .where(Session.is_active == False)  # Only completed sessions
                .order_by(Session.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            sessions = list(result.scalars().all())

            recordings = []
            for session in sessions:
                # Get segment count and duration
                seg_result = await db.execute(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.session_id == session.id)
                    .order_by(TranscriptSegment.end_time.desc())
                )
                segments = list(seg_result.scalars().all())

                duration_seconds = 0
                if segments:
                    duration_seconds = int(max(s.end_time for s in segments))
                elif session.ended_at:
                    elapsed = (session.ended_at - session.started_at).total_seconds()
                    duration_seconds = max(0, int(elapsed))

                # Check if any meeting has summaries
                has_summary = False
                title = None
                meetings = sorted(session.meetings, key=lambda m: m.key_start)
                for meeting in meetings:
                    if not title and meeting.title:
                        title = meeting.title
                    sum_result = await db.execute(
                        select(Summary).where(Summary.meeting_id == meeting.id).limit(1)
                    )
                    if sum_result.scalar_one_or_none():
                        has_summary = True
                        break

                recordings.append({
                    "id": session.id,
                    "title": title,
                    "timezone_name": session.timezone_name,
                    "timezone_offset_minutes": session.timezone_offset_minutes,
                    "has_transcription": bool(session.has_transcription),
                    "started_at": to_utc_iso(session.started_at),
                    "ended_at": to_utc_iso(session.ended_at),
                    "duration_seconds": duration_seconds,
                    "segment_count": len(segments),
                    "has_summary": has_summary,
                })

            return recordings

    async def _ensure_session_timezone_columns(self, conn) -> None:
        """Backfill schema for timezone metadata on existing SQLite DBs."""
        result = await conn.execute(text("PRAGMA table_info(sessions)"))
        column_names = {row[1] for row in result.fetchall()}

        if "timezone_name" not in column_names:
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN timezone_name VARCHAR(128)"))

        if "timezone_offset_minutes" not in column_names:
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN timezone_offset_minutes INTEGER"))

    async def _ensure_session_transcription_column(self, conn) -> None:
        """Backfill schema for transcription state on existing SQLite DBs."""
        result = await conn.execute(text("PRAGMA table_info(sessions)"))
        column_names = {row[1] for row in result.fetchall()}
        if "has_transcription" not in column_names:
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN has_transcription BOOLEAN DEFAULT 0"))

    async def _ensure_transcript_speaker_column(self, conn) -> None:
        """Backfill schema for speaker label on existing SQLite DBs."""
        result = await conn.execute(text("PRAGMA table_info(transcript_segments)"))
        column_names = {row[1] for row in result.fetchall()}
        if "speaker" not in column_names:
            await conn.execute(text("ALTER TABLE transcript_segments ADD COLUMN speaker VARCHAR(64)"))

    # Structured item operations
    async def add_structured_item(
        self,
        meeting_id: str,
        item_id: str,
        item_type: str,
        text: str,
        owner: str | None = None,
        due_date: str | None = None,
        blocking: str | None = None,
        source_timestamp: str | None = None,
        confidence: float = 1.0,
        rationale: str | None = None,
        impact: str | None = None,
        mitigation: str | None = None,
        context: str | None = None,
        who_decides: str | None = None,
        timeline: str | None = None,
        status: str = "open",
    ) -> StructuredItem:
        """Add a structured item for a meeting."""
        async with self._session_factory() as db:
            item = StructuredItem(
                meeting_id=meeting_id,
                item_id=item_id,
                item_type=item_type,
                text=text,
                owner=owner,
                due_date=due_date,
                blocking=blocking,
                source_timestamp=source_timestamp,
                confidence=confidence,
                rationale=rationale,
                impact=impact,
                mitigation=mitigation,
                context=context,
                who_decides=who_decides,
                timeline=timeline,
                status=status,
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            return item

    async def add_structured_items_bulk(
        self,
        meeting_id: str,
        items: list[dict],
    ) -> list[StructuredItem]:
        """Add multiple structured items for a meeting in a single transaction."""
        async with self._session_factory() as db:
            created = []
            for item_data in items:
                item = StructuredItem(
                    meeting_id=meeting_id,
                    item_id=item_data.get("item_id", ""),
                    item_type=item_data.get("item_type", "action"),
                    text=item_data.get("text", ""),
                    owner=item_data.get("owner"),
                    due_date=item_data.get("due_date"),
                    blocking=item_data.get("blocking"),
                    source_timestamp=item_data.get("source_timestamp"),
                    confidence=item_data.get("confidence", 1.0),
                    rationale=item_data.get("rationale"),
                    impact=item_data.get("impact"),
                    mitigation=item_data.get("mitigation"),
                    context=item_data.get("context"),
                    who_decides=item_data.get("who_decides"),
                    timeline=item_data.get("timeline"),
                    status=item_data.get("status", "open"),
                )
                db.add(item)
                created.append(item)
            await db.commit()
            for item in created:
                await db.refresh(item)
            return created

    async def get_structured_items(
        self,
        meeting_id: str,
        item_type: str | None = None,
    ) -> list[StructuredItem]:
        """Get structured items for a meeting."""
        async with self._session_factory() as db:
            query = select(StructuredItem).where(StructuredItem.meeting_id == meeting_id)
            if item_type:
                query = query.where(StructuredItem.item_type == item_type)
            query = query.order_by(StructuredItem.item_id)
            result = await db.execute(query)
            return list(result.scalars().all())

    async def delete_structured_items(self, meeting_id: str) -> int:
        """Delete all structured items for a meeting."""
        from sqlalchemy import delete

        async with self._session_factory() as db:
            result = await db.execute(
                delete(StructuredItem).where(StructuredItem.meeting_id == meeting_id)
            )
            await db.commit()
            return result.rowcount or 0
