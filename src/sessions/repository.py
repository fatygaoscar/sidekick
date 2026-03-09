"""Database operations for sessions, meetings, and transcripts."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.summarization.prompts import DEFAULT_TEMPLATE_KEY, normalize_template_key

from .models import (
    Base,
    ImportantMarker,
    Meeting,
    Session,
    StructuredItem,
    Summary,
    TranscriptSegment,
    TranscriptVersion,
)
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
            await self._ensure_transcript_version_table(conn)
            await self._ensure_transcript_segment_version_column(conn)
            await self._ensure_meeting_workflow_columns(conn)
            await self._ensure_transcript_speaker_column(conn)
            await self._ensure_transcript_speaker_cluster_column(conn)
            await self._ensure_summary_duration_column(conn)
            await self._ensure_summary_template_column(conn)
            await self._ensure_summary_workflow_columns(conn)
            await self._ensure_summary_transcript_version_column(conn)
            await self._ensure_transcript_search_table(conn)
            if await self._transcript_search_index_is_empty(conn):
                await self._backfill_transcript_search_index(conn)

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
                    selectinload(Session.transcript_versions),
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
                text("DELETE FROM transcript_segments_fts WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
            await db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)
            )
            await db.execute(
                delete(TranscriptVersion).where(TranscriptVersion.session_id == session_id)
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
            meeting = Meeting(session_id=session_id, title=title, template_key=DEFAULT_TEMPLATE_KEY)
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
        meeting = await self.get_meeting(meeting_id)
        async with self._session_factory() as db:
            await db.execute(
                update(Meeting).where(Meeting.id == meeting_id).values(title=title)
            )
            if meeting:
                await self._refresh_transcript_search_index_for_session(db, meeting.session_id)
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
            if "title" in values:
                meeting = await self.get_meeting(meeting_id)
                if meeting:
                    await self._refresh_transcript_search_index_for_session(db, meeting.session_id)
            await db.commit()
        return await self.get_meeting(meeting_id)

    async def list_transcript_versions(self, session_id: str) -> list[TranscriptVersion]:
        """List transcript versions for a session, newest first."""
        await self.ensure_transcript_versions(session_id)
        async with self._session_factory() as db:
            result = await db.execute(
                select(TranscriptVersion)
                .where(TranscriptVersion.session_id == session_id)
                .order_by(TranscriptVersion.version_number.desc(), TranscriptVersion.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_transcript_version(self, version_id: str) -> TranscriptVersion | None:
        """Get a transcript version by ID."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(TranscriptVersion).where(TranscriptVersion.id == version_id)
            )
            return result.scalar_one_or_none()

    async def get_transcript_version_for_session(
        self,
        session_id: str,
        version_id: str,
    ) -> TranscriptVersion | None:
        """Get a transcript version if it belongs to the session."""
        async with self._session_factory() as db:
            result = await db.execute(
                select(TranscriptVersion).where(
                    TranscriptVersion.id == version_id,
                    TranscriptVersion.session_id == session_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_latest_transcript_version(
        self,
        session_id: str,
        *,
        include_processing: bool = False,
    ) -> TranscriptVersion | None:
        """Get the latest transcript version for a session."""
        versions = await self.list_transcript_versions(session_id)
        if include_processing:
            return versions[0] if versions else None
        for version in versions:
            if version.status == "ready":
                return version
        return versions[0] if versions else None

    async def create_transcript_version(
        self,
        *,
        session_id: str,
        meeting_id: str,
        version_number: int,
        parent_version_id: str | None = None,
        status: str = "ready",
        source_type: str = "initial_transcription",
        transcription_backend: str | None = None,
        transcription_model: str | None = None,
        diarization_backend: str | None = None,
        diarization_model: str | None = None,
        template_key: str | None = None,
        custom_prompt: str | None = None,
        speaker_review_required: bool = False,
        speaker_review_completed_at: datetime | None = None,
    ) -> TranscriptVersion:
        """Create a transcript version."""
        async with self._session_factory() as db:
            version = TranscriptVersion(
                session_id=session_id,
                meeting_id=meeting_id,
                version_number=version_number,
                parent_version_id=parent_version_id,
                status=status,
                source_type=source_type,
                transcription_backend=transcription_backend,
                transcription_model=transcription_model,
                diarization_backend=diarization_backend,
                diarization_model=diarization_model,
                template_key=template_key,
                custom_prompt=custom_prompt,
                speaker_review_required=speaker_review_required,
                speaker_review_completed_at=speaker_review_completed_at,
            )
            db.add(version)
            await db.commit()
            await db.refresh(version)
            return version

    async def update_transcript_version(
        self,
        version_id: str,
        *,
        status: str | object = UNSET,
        template_key: str | None | object = UNSET,
        custom_prompt: str | None | object = UNSET,
        speaker_review_required: bool | object = UNSET,
        speaker_review_completed_at: datetime | None | object = UNSET,
        transcription_backend: str | None | object = UNSET,
        transcription_model: str | None | object = UNSET,
        diarization_backend: str | None | object = UNSET,
        diarization_model: str | None | object = UNSET,
    ) -> TranscriptVersion | None:
        """Update mutable transcript version fields."""
        values: dict[str, Any] = {"updated_at": datetime.utcnow()}
        if status is not UNSET:
            values["status"] = status
        if template_key is not UNSET:
            values["template_key"] = template_key
        if custom_prompt is not UNSET:
            values["custom_prompt"] = custom_prompt
        if speaker_review_required is not UNSET:
            values["speaker_review_required"] = bool(speaker_review_required)
        if speaker_review_completed_at is not UNSET:
            values["speaker_review_completed_at"] = speaker_review_completed_at
        if transcription_backend is not UNSET:
            values["transcription_backend"] = transcription_backend
        if transcription_model is not UNSET:
            values["transcription_model"] = transcription_model
        if diarization_backend is not UNSET:
            values["diarization_backend"] = diarization_backend
        if diarization_model is not UNSET:
            values["diarization_model"] = diarization_model

        async with self._session_factory() as db:
            await db.execute(
                update(TranscriptVersion)
                .where(TranscriptVersion.id == version_id)
                .values(**values)
            )
            await db.commit()
        return await self.get_transcript_version(version_id)

    async def ensure_transcript_versions(self, session_id: str) -> list[TranscriptVersion]:
        """Lazy-backfill transcript version v1 for legacy recordings."""
        async with self._session_factory() as db:
            existing_result = await db.execute(
                select(TranscriptVersion)
                .where(TranscriptVersion.session_id == session_id)
                .order_by(TranscriptVersion.version_number.desc(), TranscriptVersion.created_at.desc())
            )
            existing_versions = list(existing_result.scalars().all())
            if existing_versions:
                return existing_versions

            session = await db.get(Session, session_id)
            if not session:
                return []

            meetings_result = await db.execute(
                select(Meeting)
                .where(Meeting.session_id == session_id)
                .order_by(Meeting.key_start.asc())
            )
            meetings = list(meetings_result.scalars().all())
            meeting = meetings[0] if meetings else None
            if not meeting:
                return []

            has_segments_result = await db.execute(
                select(TranscriptSegment.id)
                .where(TranscriptSegment.session_id == session_id)
                .limit(1)
            )
            has_segments = has_segments_result.scalar_one_or_none() is not None

            has_summaries_result = await db.execute(
                select(Summary.id)
                .where(Summary.meeting_id == meeting.id)
                .limit(1)
            )
            has_summaries = has_summaries_result.scalar_one_or_none() is not None

            if not session.has_transcription and not has_segments and not has_summaries:
                return []

            version = TranscriptVersion(
                session_id=session_id,
                meeting_id=meeting.id,
                version_number=1,
                status="ready",
                source_type="initial_transcription",
                template_key=normalize_template_key(meeting.template_key or DEFAULT_TEMPLATE_KEY),
                custom_prompt=meeting.custom_prompt,
                speaker_review_required=bool(meeting.speaker_review_required),
                speaker_review_completed_at=meeting.speaker_review_completed_at,
            )
            db.add(version)
            await db.flush()

            await db.execute(
                update(TranscriptSegment)
                .where(
                    TranscriptSegment.session_id == session_id,
                    TranscriptSegment.transcript_version_id.is_(None),
                )
                .values(transcript_version_id=version.id)
            )
            await db.execute(
                update(Summary)
                .where(
                    Summary.meeting_id == meeting.id,
                    Summary.transcript_version_id.is_(None),
                )
                .values(transcript_version_id=version.id)
            )
            await db.commit()

        return await self.list_transcript_versions(session_id)

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
        transcript_version_id: str | None = None,
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
                transcript_version_id=transcript_version_id,
            )
            db.add(segment)
            await db.commit()
            await db.refresh(segment)
            return segment

    async def get_segments(
        self,
        session_id: str | None = None,
        meeting_id: str | None = None,
        transcript_version_id: str | None = None,
        important_only: bool = False,
    ) -> list[TranscriptSegment]:
        """Get transcript segments with optional filters."""
        async with self._session_factory() as db:
            query = select(TranscriptSegment)

            if session_id:
                query = query.where(TranscriptSegment.session_id == session_id)
            if meeting_id:
                query = query.where(TranscriptSegment.meeting_id == meeting_id)
            if transcript_version_id:
                query = query.where(TranscriptSegment.transcript_version_id == transcript_version_id)
            if important_only:
                query = query.where(TranscriptSegment.is_important == True)

            query = query.order_by(TranscriptSegment.start_time)
            result = await db.execute(query)
            return list(result.scalars().all())

    async def delete_segments_for_session(self, session_id: str) -> int:
        """Delete all transcript segments for a session."""
        from sqlalchemy import delete

        async with self._session_factory() as db:
            await db.execute(
                text("DELETE FROM transcript_segments_fts WHERE session_id = :session_id"),
                {"session_id": session_id},
            )
            result = await db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.session_id == session_id)
            )
            await db.commit()
            return result.rowcount or 0

    async def delete_segments_for_transcript_version(self, transcript_version_id: str) -> int:
        """Delete transcript segments for one transcript version."""
        async with self._session_factory() as db:
            session_result = await db.execute(
                select(TranscriptVersion.session_id).where(TranscriptVersion.id == transcript_version_id)
            )
            session_id = session_result.scalar_one_or_none()
            result = await db.execute(
                delete(TranscriptSegment).where(
                    TranscriptSegment.transcript_version_id == transcript_version_id
                )
            )
            if session_id:
                await self._refresh_transcript_search_index_for_session(db, session_id)
            await db.commit()
            return result.rowcount or 0

    async def update_segments_speakers(self, updates: dict[str, str | None]) -> None:
        """Bulk update speaker labels. updates maps segment_id → speaker label."""
        async with self._session_factory() as db:
            session_ids: set[str] = set()
            for segment_id, speaker in updates.items():
                existing = await db.execute(
                    select(TranscriptSegment.session_id).where(TranscriptSegment.id == segment_id)
                )
                session_id = existing.scalar_one_or_none()
                if session_id:
                    session_ids.add(session_id)
                await db.execute(
                    update(TranscriptSegment)
                    .where(TranscriptSegment.id == segment_id)
                    .values(speaker=speaker)
                )
            for session_id in session_ids:
                await self._refresh_transcript_search_index_for_session(db, session_id)
            await db.commit()

    async def update_segments_speaker_metadata(
        self,
        updates: dict[str, dict[str, str | None]],
    ) -> None:
        """Bulk update speaker display labels and/or immutable speaker clusters."""
        async with self._session_factory() as db:
            session_ids: set[str] = set()
            for segment_id, payload in updates.items():
                values: dict[str, str | None] = {}
                existing = await db.execute(
                    select(TranscriptSegment.session_id).where(TranscriptSegment.id == segment_id)
                )
                session_id = existing.scalar_one_or_none()
                if session_id:
                    session_ids.add(session_id)
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
            for session_id in session_ids:
                await self._refresh_transcript_search_index_for_session(db, session_id)
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
        transcript_version_id: str | None = None,
        workflow_data_json: str | None = None,
    ) -> Summary:
        """Add a summary for a meeting."""
        async with self._session_factory() as db:
            summary = Summary(
                meeting_id=meeting_id,
                transcript_version_id=transcript_version_id,
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
                workflow_data_json=workflow_data_json,
            )
            db.add(summary)
            await db.commit()
            await db.refresh(summary)
            return summary

    async def get_summaries(
        self,
        meeting_id: str,
        status: str | None = None,
        transcript_version_id: str | None = None,
    ) -> list[Summary]:
        """Get summaries for a meeting."""
        async with self._session_factory() as db:
            query = select(Summary).where(Summary.meeting_id == meeting_id)
            if status:
                query = query.where(Summary.status == status)
            if transcript_version_id:
                query = query.where(Summary.transcript_version_id == transcript_version_id)
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
        transcript_version_id: str | None = None,
    ) -> Summary | None:
        """Get the newest summary for a meeting, optionally filtered by status."""
        summaries = await self.get_summaries(
            meeting_id,
            status=status,
            transcript_version_id=transcript_version_id,
        )
        return summaries[0] if summaries else None

    async def get_draft_summary(
        self,
        meeting_id: str,
        transcript_version_id: str | None = None,
    ) -> Summary | None:
        """Get the active draft summary for a meeting."""
        return await self.get_latest_summary(
            meeting_id,
            status="draft",
            transcript_version_id=transcript_version_id,
        )

    async def delete_summary(self, summary_id: str) -> None:
        """Delete a summary by ID."""
        async with self._session_factory() as db:
            await db.execute(delete(Summary).where(Summary.id == summary_id))
            await db.commit()

    async def delete_draft_summaries(
        self,
        meeting_id: str,
        transcript_version_id: str | None = None,
    ) -> int:
        """Delete all draft summaries for a meeting."""
        async with self._session_factory() as db:
            statement = delete(Summary).where(
                Summary.meeting_id == meeting_id,
                Summary.status == "draft",
            )
            if transcript_version_id:
                statement = statement.where(Summary.transcript_version_id == transcript_version_id)
            result = await db.execute(statement)
            await db.commit()
            return result.rowcount or 0

    async def replace_draft_summary(
        self,
        meeting_id: str,
        *,
        transcript_version_id: str | None = None,
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
        workflow_data_json: str | None = None,
    ) -> Summary:
        """Replace the active draft summary for a meeting."""
        await self.delete_draft_summaries(
            meeting_id,
            transcript_version_id=transcript_version_id,
        )
        return await self.add_summary(
            meeting_id=meeting_id,
            transcript_version_id=transcript_version_id,
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
            workflow_data_json=workflow_data_json,
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
            transcript_version_id=source.transcript_version_id,
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
            workflow_data_json=source.workflow_data_json,
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
            transcript_version_id=draft.transcript_version_id,
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
            workflow_data_json=draft.workflow_data_json,
        )
        await self.delete_draft_summaries(
            draft.meeting_id,
            transcript_version_id=draft.transcript_version_id,
        )
        return saved

    async def get_sessions_list(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Get list of sessions with summary info for recordings page."""
        async with self._session_factory() as db:
            # Get sessions ordered by start time (newest first)
            result = await db.execute(
                select(Session)
                .options(
                    selectinload(Session.meetings),
                    selectinload(Session.transcript_versions),
                )
                .where(Session.is_active == False)  # Only completed sessions
                .order_by(Session.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
            sessions = list(result.scalars().all())

            recordings = []
            for session in sessions:
                versions = sorted(
                    list(getattr(session, "transcript_versions", []) or []),
                    key=lambda version: (version.version_number, version.created_at),
                    reverse=True,
                )
                latest_ready_version = next(
                    (version for version in versions if version.status == "ready"),
                    versions[0] if versions else None,
                )

                # Get segment count and duration
                segment_query = select(TranscriptSegment).where(TranscriptSegment.session_id == session.id)
                if latest_ready_version:
                    segment_query = segment_query.where(
                        TranscriptSegment.transcript_version_id == latest_ready_version.id
                    )
                seg_result = await db.execute(segment_query.order_by(TranscriptSegment.end_time.desc()))
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
                    if latest_ready_version:
                        if (
                            latest_ready_version.meeting_id == meeting.id
                            and latest_ready_version.speaker_review_required
                            and latest_ready_version.speaker_review_completed_at is None
                        ):
                            needs_speaker_review = True
                    elif meeting.speaker_review_required and meeting.speaker_review_completed_at is None:
                        needs_speaker_review = True
                    summaries_query = select(Summary).where(Summary.meeting_id == meeting.id)
                    if latest_ready_version:
                        summaries_query = summaries_query.where(
                            Summary.transcript_version_id == latest_ready_version.id
                        )
                    meeting_summaries = await db.execute(summaries_query)
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

    async def reindex_session_transcript_search(self, session_id: str) -> None:
        """Rebuild transcript search documents for a single session."""
        async with self._session_factory() as db:
            await self._refresh_transcript_search_index_for_session(db, session_id)
            await db.commit()

    async def search_transcript_segments(
        self,
        *,
        query: str,
        limit: int = 24,
        date_from: date | None = None,
        date_to: date | None = None,
        speaker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search transcript segments across completed recordings using SQLite FTS."""
        params: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(limit), 50)),
        }
        conditions = [
            "sessions.is_active = 0",
            "sessions.has_transcription = 1",
            "transcript_segments_fts MATCH :query",
        ]

        if date_from is not None:
            params["date_from"] = datetime.combine(date_from, time.min, tzinfo=timezone.utc).replace(tzinfo=None)
            conditions.append("sessions.started_at >= :date_from")
        if date_to is not None:
            params["date_to"] = datetime.combine(
                date_to + timedelta(days=1),
                time.min,
                tzinfo=timezone.utc,
            ).replace(tzinfo=None)
            conditions.append("sessions.started_at < :date_to")
        if speaker:
            params["speaker"] = speaker.strip().lower()
            conditions.append("LOWER(COALESCE(transcript_segments.speaker, '')) = :speaker")

        statement = text(
            f"""
            SELECT
                transcript_segments.id AS segment_id,
                transcript_segments.session_id AS session_id,
                transcript_segments.meeting_id AS meeting_id,
                transcript_segments.transcript_version_id AS transcript_version_id,
                transcript_segments.text AS text,
                transcript_segments.start_time AS start_time,
                transcript_segments.end_time AS end_time,
                transcript_segments.is_important AS is_important,
                transcript_segments.speaker AS speaker,
                transcript_segments.speaker_cluster AS speaker_cluster,
                meetings.title AS meeting_title,
                sessions.started_at AS session_started_at,
                sessions.timezone_name AS timezone_name,
                sessions.timezone_offset_minutes AS timezone_offset_minutes,
                bm25(transcript_segments_fts, 6.0, 2.0, 1.0, 0.5) AS rank
            FROM transcript_segments_fts
            JOIN transcript_segments
              ON transcript_segments.id = transcript_segments_fts.segment_id
            JOIN sessions
              ON sessions.id = transcript_segments.session_id
            LEFT JOIN meetings
              ON meetings.id = transcript_segments.meeting_id
            WHERE {" AND ".join(conditions)}
            ORDER BY rank ASC, sessions.started_at DESC, transcript_segments.start_time ASC
            LIMIT :limit
            """
        )

        async with self._session_factory() as db:
            result = await db.execute(statement, params)
            rows = []
            for row in result.mappings().all():
                rows.append(dict(row))
            return rows

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

    async def _ensure_transcript_version_table(self, conn) -> None:
        """Create transcript version table for versioned workspace state."""
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS transcript_versions (
                    id VARCHAR(36) PRIMARY KEY,
                    session_id VARCHAR(36) NOT NULL,
                    meeting_id VARCHAR(36) NOT NULL,
                    version_number INTEGER NOT NULL DEFAULT 1,
                    parent_version_id VARCHAR(36),
                    status VARCHAR(20) NOT NULL DEFAULT 'ready',
                    source_type VARCHAR(32) NOT NULL DEFAULT 'initial_transcription',
                    transcription_backend VARCHAR(50),
                    transcription_model VARCHAR(100),
                    diarization_backend VARCHAR(50),
                    diarization_model VARCHAR(100),
                    template_key VARCHAR(100),
                    custom_prompt TEXT,
                    speaker_review_required BOOLEAN DEFAULT 0,
                    speaker_review_completed_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

    async def _ensure_transcript_segment_version_column(self, conn) -> None:
        """Backfill schema for transcript version linkage."""
        result = await conn.execute(text("PRAGMA table_info(transcript_segments)"))
        column_names = {row[1] for row in result.fetchall()}
        if "transcript_version_id" not in column_names:
            await conn.execute(
                text("ALTER TABLE transcript_segments ADD COLUMN transcript_version_id VARCHAR(36)")
            )

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
        if "workflow_data_json" not in column_names:
            await conn.execute(text("ALTER TABLE summaries ADD COLUMN workflow_data_json TEXT"))

        await conn.execute(
            text(
                """
                UPDATE summaries
                SET status = 'saved'
                WHERE status IS NULL OR status = ''
                """
            )
        )

    async def _ensure_summary_transcript_version_column(self, conn) -> None:
        """Backfill schema for transcript version linkage on summaries."""
        result = await conn.execute(text("PRAGMA table_info(summaries)"))
        column_names = {row[1] for row in result.fetchall()}
        if "transcript_version_id" not in column_names:
            await conn.execute(
                text("ALTER TABLE summaries ADD COLUMN transcript_version_id VARCHAR(36)")
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

    async def _ensure_transcript_search_table(self, conn) -> None:
        """Create the FTS search table for transcript segments."""
        await conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS transcript_segments_fts USING fts5(
                    segment_id UNINDEXED,
                    session_id UNINDEXED,
                    meeting_id UNINDEXED,
                    text,
                    speaker,
                    speaker_cluster,
                    meeting_title
                )
                """
            )
        )

    async def _backfill_transcript_search_index(self, conn) -> None:
        """Rebuild the transcript search index from canonical transcript data."""
        await conn.execute(text("DELETE FROM transcript_segments_fts"))
        await conn.execute(
            text(
                """
                INSERT INTO transcript_segments_fts (
                    segment_id,
                    session_id,
                    meeting_id,
                    text,
                    speaker,
                    speaker_cluster,
                    meeting_title
                )
                SELECT
                    transcript_segments.id,
                    transcript_segments.session_id,
                    COALESCE(transcript_segments.meeting_id, ''),
                    transcript_segments.text,
                    COALESCE(transcript_segments.speaker, ''),
                    COALESCE(transcript_segments.speaker_cluster, ''),
                    COALESCE(meetings.title, '')
                FROM transcript_segments
                JOIN sessions
                  ON sessions.id = transcript_segments.session_id
                LEFT JOIN meetings
                  ON meetings.id = transcript_segments.meeting_id
                WHERE sessions.is_active = 0
                  AND (
                    transcript_segments.transcript_version_id = (
                        SELECT tv.id
                        FROM transcript_versions tv
                        WHERE tv.session_id = transcript_segments.session_id
                          AND tv.status = 'ready'
                        ORDER BY tv.version_number DESC, tv.created_at DESC
                        LIMIT 1
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM transcript_versions tv
                        WHERE tv.session_id = transcript_segments.session_id
                          AND tv.status = 'ready'
                    )
                  )
                """
            )
        )

    async def _transcript_search_index_is_empty(self, conn) -> bool:
        """Return whether the transcript search index has any indexed rows."""
        result = await conn.execute(text("SELECT 1 FROM transcript_segments_fts LIMIT 1"))
        return result.first() is None

    async def _refresh_transcript_search_index_for_session(self, executor, session_id: str) -> None:
        """Refresh transcript search documents for a single session."""
        await executor.execute(
            text("DELETE FROM transcript_segments_fts WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        await executor.execute(
            text(
                """
                INSERT INTO transcript_segments_fts (
                    segment_id,
                    session_id,
                    meeting_id,
                    text,
                    speaker,
                    speaker_cluster,
                    meeting_title
                )
                WITH latest_version AS (
                    SELECT tv.id
                    FROM transcript_versions tv
                    WHERE tv.session_id = :session_id
                      AND tv.status = 'ready'
                    ORDER BY tv.version_number DESC, tv.created_at DESC
                    LIMIT 1
                )
                SELECT
                    transcript_segments.id,
                    transcript_segments.session_id,
                    COALESCE(transcript_segments.meeting_id, ''),
                    transcript_segments.text,
                    COALESCE(transcript_segments.speaker, ''),
                    COALESCE(transcript_segments.speaker_cluster, ''),
                    COALESCE(meetings.title, '')
                FROM transcript_segments
                LEFT JOIN meetings
                  ON meetings.id = transcript_segments.meeting_id
                LEFT JOIN latest_version
                  ON 1 = 1
                WHERE transcript_segments.session_id = :session_id
                  AND (
                    (latest_version.id IS NOT NULL AND transcript_segments.transcript_version_id = latest_version.id)
                    OR latest_version.id IS NULL
                  )
                """
            ),
            {"session_id": session_id},
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
