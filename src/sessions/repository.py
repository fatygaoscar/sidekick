"""Database operations for sessions, meetings, and transcripts."""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from .models import Base, ImportantMarker, Meeting, Session, StructuredItem, Summary, TranscriptSegment
from src.core.datetime_utils import to_utc_iso


UNSET = object()


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
            await self._ensure_meeting_workflow_columns(conn)
            await self._ensure_transcript_speaker_column(conn)
            await self._ensure_transcript_speaker_cluster_column(conn)
            await self._ensure_summary_duration_column(conn)
            await self._ensure_summary_template_column(conn)
            await self._ensure_summary_workflow_columns(conn)

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
                .options(
                    selectinload(Session.meetings).selectinload(Meeting.summaries),
                    selectinload(Session.segments),
                )
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
            meeting = Meeting(session_id=session_id, title=title, template_key="meeting")
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

    async def get_primary_meeting(
        self,
        session_id: str,
        create_if_missing: bool = False,
        title: str | None = None,
    ) -> Meeting | None:
        """Get the earliest meeting for a session, optionally creating one."""
        session = await self.get_session(session_id)
        if not session:
            return None
        meetings = sorted(session.meetings, key=lambda m: m.key_start) if session.meetings else []
        if meetings:
            return meetings[0]
        if not create_if_missing:
            return None
        return await self.create_meeting(session_id=session_id, title=title)

    async def update_meeting_settings(
        self,
        meeting_id: str,
        *,
        title: str | object = UNSET,
        template_key: str | None | object = UNSET,
        custom_prompt: str | None | object = UNSET,
        attendees: str | None | object = UNSET,
        speaker_review_required: bool | object = UNSET,
        speaker_review_completed_at: datetime | None | object = UNSET,
    ) -> Meeting | None:
        """Update workflow-related settings for a meeting."""
        values: dict[str, Any] = {}
        if title is not UNSET:
            values["title"] = title
        if template_key is not UNSET:
            values["template_key"] = template_key
        if custom_prompt is not UNSET:
            values["custom_prompt"] = custom_prompt
        if attendees is not UNSET:
            values["attendees"] = attendees
        if speaker_review_required is not UNSET:
            values["speaker_review_required"] = bool(speaker_review_required)
        if speaker_review_completed_at is not UNSET:
            values["speaker_review_completed_at"] = speaker_review_completed_at
        if not values:
            return await self.get_meeting(meeting_id)

        async with self._session_factory() as db:
            await db.execute(update(Meeting).where(Meeting.id == meeting_id).values(**values))
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
        speaker_cluster: str | None = None,
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
                speaker_cluster=speaker_cluster,
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
        async with self._session_factory() as db:
            for segment_id, speaker in updates.items():
                await db.execute(
                    update(TranscriptSegment)
                    .where(TranscriptSegment.id == segment_id)
                    .values(speaker=speaker)
                )
            await db.commit()

    async def update_segments_speaker_metadata(
        self,
        updates: dict[str, dict[str, str | None]],
    ) -> None:
        """Bulk update speaker display labels and/or immutable speaker clusters."""
        async with self._session_factory() as db:
            for segment_id, payload in updates.items():
                values: dict[str, str | None] = {}
                if "speaker" in payload:
                    values["speaker"] = payload["speaker"]
                if "speaker_cluster" in payload:
                    values["speaker_cluster"] = payload["speaker_cluster"]
                if not values:
                    continue
                await db.execute(
                    update(TranscriptSegment)
                    .where(TranscriptSegment.id == segment_id)
                    .values(**values)
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
        processing_duration_seconds: float | None = None,
        template: str | None = None,
        status: str = "saved",
        source_type: str = "generated",
        parent_summary_id: str | None = None,
        template_key: str | None = None,
        custom_prompt: str | None = None,
        pass1_system_prompt: str | None = None,
        pass1_user_prompt: str | None = None,
        pass2_system_prompt: str | None = None,
        pass2_user_prompt: str | None = None,
        attendees_snapshot: str | None = None,
        saved_to_obsidian_at: datetime | None = None,
        obsidian_relative_path: str | None = None,
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
                processing_duration_seconds=processing_duration_seconds,
                template=template,
                status=status,
                source_type=source_type,
                parent_summary_id=parent_summary_id,
                template_key=template_key,
                custom_prompt=custom_prompt,
                pass1_system_prompt=pass1_system_prompt,
                pass1_user_prompt=pass1_user_prompt,
                pass2_system_prompt=pass2_system_prompt,
                pass2_user_prompt=pass2_user_prompt,
                attendees_snapshot=attendees_snapshot,
                saved_to_obsidian_at=saved_to_obsidian_at,
                obsidian_relative_path=obsidian_relative_path,
            )
            db.add(summary)
            await db.commit()
            await db.refresh(summary)
            return summary

    async def get_summaries(self, meeting_id: str, status: str | None = None) -> list[Summary]:
        """Get summaries for a meeting."""
        async with self._session_factory() as db:
            query = select(Summary).where(Summary.meeting_id == meeting_id)
            if status:
                query = query.where(Summary.status == status)
            result = await db.execute(query.order_by(Summary.created_at.desc()))
            return list(result.scalars().all())

    async def get_summary(self, summary_id: str) -> Summary | None:
        """Get a summary by ID."""
        async with self._session_factory() as db:
            result = await db.execute(select(Summary).where(Summary.id == summary_id))
            return result.scalar_one_or_none()

    async def get_latest_summary(
        self,
        meeting_id: str,
        status: str | None = None,
    ) -> Summary | None:
        """Get the newest summary for a meeting, optionally filtered by status."""
        summaries = await self.get_summaries(meeting_id, status=status)
        return summaries[0] if summaries else None

    async def get_draft_summary(self, meeting_id: str) -> Summary | None:
        """Get the active draft summary for a meeting."""
        return await self.get_latest_summary(meeting_id, status="draft")

    async def delete_summary(self, summary_id: str) -> None:
        """Delete a summary by ID."""
        async with self._session_factory() as db:
            await db.execute(delete(Summary).where(Summary.id == summary_id))
            await db.commit()

    async def delete_draft_summaries(self, meeting_id: str) -> int:
        """Delete all draft summaries for a meeting."""
        async with self._session_factory() as db:
            result = await db.execute(
                delete(Summary).where(Summary.meeting_id == meeting_id, Summary.status == "draft")
            )
            await db.commit()
            return result.rowcount or 0

    async def replace_draft_summary(
        self,
        meeting_id: str,
        *,
        content: str,
        backend: str,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        processing_duration_seconds: float | None = None,
        template: str | None = None,
        source_type: str = "generated",
        parent_summary_id: str | None = None,
        template_key: str | None = None,
        custom_prompt: str | None = None,
        pass1_system_prompt: str | None = None,
        pass1_user_prompt: str | None = None,
        pass2_system_prompt: str | None = None,
        pass2_user_prompt: str | None = None,
        attendees_snapshot: str | None = None,
    ) -> Summary:
        """Replace the active draft summary for a meeting."""
        await self.delete_draft_summaries(meeting_id)
        return await self.add_summary(
            meeting_id=meeting_id,
            content=content,
            backend=backend,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            processing_duration_seconds=processing_duration_seconds,
            template=template,
            status="draft",
            source_type=source_type,
            parent_summary_id=parent_summary_id,
            template_key=template_key,
            custom_prompt=custom_prompt,
            pass1_system_prompt=pass1_system_prompt,
            pass1_user_prompt=pass1_user_prompt,
            pass2_system_prompt=pass2_system_prompt,
            pass2_user_prompt=pass2_user_prompt,
            attendees_snapshot=attendees_snapshot,
        )

    async def update_summary(
        self,
        summary_id: str,
        *,
        content: str | object = UNSET,
        source_type: str | object = UNSET,
        parent_summary_id: str | None | object = UNSET,
        saved_to_obsidian_at: datetime | None | object = UNSET,
        obsidian_relative_path: str | None | object = UNSET,
    ) -> Summary | None:
        """Update mutable summary fields."""
        values: dict[str, Any] = {}
        if content is not UNSET:
            values["content"] = content
        if source_type is not UNSET:
            values["source_type"] = source_type
        if parent_summary_id is not UNSET:
            values["parent_summary_id"] = parent_summary_id
        if saved_to_obsidian_at is not UNSET:
            values["saved_to_obsidian_at"] = saved_to_obsidian_at
        if obsidian_relative_path is not UNSET:
            values["obsidian_relative_path"] = obsidian_relative_path
        if not values:
            return await self.get_summary(summary_id)

        async with self._session_factory() as db:
            await db.execute(update(Summary).where(Summary.id == summary_id).values(**values))
            await db.commit()
        return await self.get_summary(summary_id)

    async def create_draft_from_summary(
        self,
        source_summary_id: str,
        *,
        source_type: str,
    ) -> Summary:
        """Clone any summary into the meeting's active draft slot."""
        source = await self.get_summary(source_summary_id)
        if not source:
            raise ValueError("Source summary not found")
        return await self.replace_draft_summary(
            meeting_id=source.meeting_id,
            content=source.content,
            backend=source.backend,
            model=source.model,
            prompt_tokens=source.prompt_tokens,
            completion_tokens=source.completion_tokens,
            processing_duration_seconds=source.processing_duration_seconds,
            template=source.template,
            source_type=source_type,
            parent_summary_id=source.id,
            template_key=source.template_key,
            custom_prompt=source.custom_prompt,
            pass1_system_prompt=source.pass1_system_prompt,
            pass1_user_prompt=source.pass1_user_prompt,
            pass2_system_prompt=source.pass2_system_prompt,
            pass2_user_prompt=source.pass2_user_prompt,
            attendees_snapshot=source.attendees_snapshot,
        )

    async def save_draft_summary(
        self,
        draft_id: str,
        *,
        saved_to_obsidian_at: datetime | None = None,
        obsidian_relative_path: str | None = None,
    ) -> Summary:
        """Persist a draft as an immutable saved summary and clear active drafts."""
        draft = await self.get_summary(draft_id)
        if not draft:
            raise ValueError("Draft summary not found")
        if draft.status != "draft":
            return draft

        saved = await self.add_summary(
            meeting_id=draft.meeting_id,
            content=draft.content,
            backend=draft.backend,
            model=draft.model,
            prompt_tokens=draft.prompt_tokens,
            completion_tokens=draft.completion_tokens,
            processing_duration_seconds=draft.processing_duration_seconds,
            template=draft.template,
            status="saved",
            source_type=draft.source_type,
            parent_summary_id=draft.parent_summary_id,
            template_key=draft.template_key,
            custom_prompt=draft.custom_prompt,
            pass1_system_prompt=draft.pass1_system_prompt,
            pass1_user_prompt=draft.pass1_user_prompt,
            pass2_system_prompt=draft.pass2_system_prompt,
            pass2_user_prompt=draft.pass2_user_prompt,
            attendees_snapshot=draft.attendees_snapshot,
            saved_to_obsidian_at=saved_to_obsidian_at,
            obsidian_relative_path=obsidian_relative_path,
        )
        await self.delete_draft_summaries(draft.meeting_id)
        return saved

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
                has_draft = False
                needs_speaker_review = False
                title = None
                meetings = sorted(session.meetings, key=lambda m: m.key_start)
                for meeting in meetings:
                    if not title and meeting.title:
                        title = meeting.title
                    if meeting.speaker_review_required and meeting.speaker_review_completed_at is None:
                        needs_speaker_review = True
                    meeting_summaries = await db.execute(
                        select(Summary).where(Summary.meeting_id == meeting.id)
                    )
                    summaries = list(meeting_summaries.scalars().all())
                    if any(summary.status == "saved" for summary in summaries):
                        has_summary = True
                    if any(summary.status == "draft" for summary in summaries):
                        has_draft = True

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
                    "has_draft": has_draft,
                    "needs_speaker_review": needs_speaker_review,
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

    async def _ensure_transcript_speaker_cluster_column(self, conn) -> None:
        """Backfill schema for immutable diarization speaker clusters."""
        result = await conn.execute(text("PRAGMA table_info(transcript_segments)"))
        column_names = {row[1] for row in result.fetchall()}
        if "speaker_cluster" not in column_names:
            await conn.execute(
                text("ALTER TABLE transcript_segments ADD COLUMN speaker_cluster VARCHAR(64)")
            )
        await conn.execute(
            text(
                """
                UPDATE transcript_segments
                SET speaker_cluster = speaker
                WHERE speaker_cluster IS NULL
                  AND speaker LIKE 'SPEAKER_%'
                """
            )
        )

    async def _ensure_summary_duration_column(self, conn) -> None:
        """Backfill schema for processing duration on existing SQLite DBs."""
        result = await conn.execute(text("PRAGMA table_info(summaries)"))
        column_names = {row[1] for row in result.fetchall()}
        if "processing_duration_seconds" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN processing_duration_seconds FLOAT"))

    async def _ensure_summary_template_column(self, conn) -> None:
        """Backfill schema for template name on existing SQLite DBs."""
        result = await conn.execute(text("PRAGMA table_info(summaries)"))
        column_names = {row[1] for row in result.fetchall()}
        if "template" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN template VARCHAR(100)"))

    async def _ensure_meeting_workflow_columns(self, conn) -> None:
        """Backfill schema for recording-level workflow settings."""
        result = await conn.execute(text("PRAGMA table_info(meetings)"))
        column_names = {row[1] for row in result.fetchall()}
        if "template_key" not in column_names:
            await conn.execute(text("ALTER TABLE meetings ADD COLUMN template_key VARCHAR(100)"))
        if "custom_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE meetings ADD COLUMN custom_prompt TEXT"))
        if "attendees" not in column_names:
            await conn.execute(text("ALTER TABLE meetings ADD COLUMN attendees TEXT"))
        if "speaker_review_required" not in column_names:
            await conn.execute(
                text("ALTER TABLE meetings ADD COLUMN speaker_review_required BOOLEAN DEFAULT 0")
            )
        if "speaker_review_completed_at" not in column_names:
            await conn.execute(text("ALTER TABLE meetings ADD COLUMN speaker_review_completed_at DATETIME"))
        await conn.execute(
            text(
                """
                UPDATE meetings
                SET template_key = 'meeting'
                WHERE template_key IS NULL OR template_key = ''
                """
            )
        )

    async def _ensure_summary_workflow_columns(self, conn) -> None:
        """Backfill schema for draft/saved summary workflow metadata."""
        result = await conn.execute(text("PRAGMA table_info(summaries)"))
        column_names = {row[1] for row in result.fetchall()}
        if "status" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN status VARCHAR(20) DEFAULT 'saved'"))
        if "source_type" not in column_names:
            await conn.execute(
                text("ALTER TABLE summaries ADD COLUMN source_type VARCHAR(32) DEFAULT 'generated'")
            )
        if "parent_summary_id" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN parent_summary_id VARCHAR(36)"))
        if "template_key" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN template_key VARCHAR(100)"))
        if "custom_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN custom_prompt TEXT"))
        if "pass1_system_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN pass1_system_prompt TEXT"))
        if "pass1_user_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN pass1_user_prompt TEXT"))
        if "pass2_system_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN pass2_system_prompt TEXT"))
        if "pass2_user_prompt" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN pass2_user_prompt TEXT"))
        if "attendees_snapshot" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN attendees_snapshot TEXT"))
        if "saved_to_obsidian_at" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN saved_to_obsidian_at DATETIME"))
        if "obsidian_relative_path" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN obsidian_relative_path TEXT"))

        await conn.execute(
            text(
                """
                UPDATE summaries
                SET status = 'saved'
                WHERE status IS NULL OR status = ''
                """
            )
        )
        await conn.execute(
            text(
                """
                UPDATE summaries
                SET source_type = CASE
                    WHEN backend = 'manual' THEN 'manual_edit'
                    ELSE 'generated'
                END
                WHERE source_type IS NULL OR source_type = ''
                """
            )
        )

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
