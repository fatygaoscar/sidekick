"""Session lifecycle management."""

from datetime import datetime, timedelta

from config.settings import get_settings
from src.core.events import EventType, get_event_bus

from .models import ImportantMarker, Meeting, Session, TranscriptSegment
from .repository import Repository


class SessionManager:
    """Manages session and meeting lifecycle."""

    def __init__(self, repository: Repository) -> None:
        self._repo = repository
        self._settings = get_settings()
        self._event_bus = get_event_bus()
        self._current_session: Session | None = None
        self._current_meeting: Meeting | None = None
        self._session_start_time: datetime | None = None

    @property
    def current_session(self) -> Session | None:
        """Get the current active session."""
        return self._current_session

    @property
    def current_meeting(self) -> Meeting | None:
        """Get the current active meeting."""
        return self._current_meeting

    @property
    def session_elapsed_seconds(self) -> float:
        """Get elapsed seconds since session start."""
        if self._session_start_time is None:
            return 0.0
        return (datetime.utcnow() - self._session_start_time).total_seconds()

    async def start_session(self, mode: str = "work", submode: str | None = None) -> Session:
        """Start a new session."""
        # End any existing active session
        if self._current_session:
            await self.end_session()

        self._current_session = await self._repo.create_session(mode=mode, submode=submode)
        self._session_start_time = self._current_session.started_at

        await self._event_bus.emit(
            EventType.SESSION_STARTED,
            {
                "session_id": self._current_session.id,
                "mode": mode,
                "submode": submode,
            },
            source="session_manager",
        )

        return self._current_session

    async def end_session(self) -> Session | None:
        """End the current session."""
        if not self._current_session:
            return None

        # End any active meeting first
        if self._current_meeting:
            await self.end_meeting()

        session = await self._repo.end_session(self._current_session.id)

        await self._event_bus.emit(
            EventType.SESSION_ENDED,
            {"session_id": self._current_session.id},
            source="session_manager",
        )

        self._current_session = None
        self._session_start_time = None

        return session

    async def change_mode(self, mode: str, submode: str | None = None) -> Session | None:
        """Change the current session mode."""
        if not self._current_session:
            return None

        self._current_session = await self._repo.update_session_mode(
            self._current_session.id, mode, submode
        )

        await self._event_bus.emit(
            EventType.SESSION_MODE_CHANGED,
            {
                "session_id": self._current_session.id,
                "mode": mode,
                "submode": submode,
            },
            source="session_manager",
        )

        return self._current_session

    async def start_meeting(self, title: str | None = None) -> Meeting | None:
        """Start a new meeting (Key Start)."""
        if not self._current_session:
            return None

        # End any existing active meeting
        if self._current_meeting:
            await self.end_meeting()

        self._current_meeting = await self._repo.create_meeting(
            self._current_session.id, title=title
        )

        await self._event_bus.emit(
            EventType.MEETING_STARTED,
            {
                "meeting_id": self._current_meeting.id,
                "session_id": self._current_session.id,
                "title": title,
            },
            source="session_manager",
        )

        return self._current_meeting

    async def end_meeting(self) -> Meeting | None:
        """End the current meeting (Key Stop)."""
        if not self._current_meeting:
            return None

        meeting = await self._repo.end_meeting(self._current_meeting.id)

        await self._event_bus.emit(
            EventType.MEETING_ENDED,
            {
                "meeting_id": self._current_meeting.id,
                "session_id": self._current_session.id if self._current_session else None,
            },
            source="session_manager",
        )

        self._current_meeting = None

        return meeting

    async def add_transcript_segment(
        self,
        text: str,
        start_time: float,
        end_time: float,
        confidence: float | None = None,
    ) -> TranscriptSegment:
        """Add a transcript segment to the current session/meeting."""
        if not self._current_session:
            raise ValueError("No active session")

        segment = await self._repo.add_segment(
            session_id=self._current_session.id,
            text=text,
            start_time=start_time,
            end_time=end_time,
            meeting_id=self._current_meeting.id if self._current_meeting else None,
            confidence=confidence,
        )

        await self._event_bus.emit(
            EventType.TRANSCRIPTION_SEGMENT,
            {
                "segment_id": segment.id,
                "text": text,
                "start_time": start_time,
                "end_time": end_time,
                "is_important": segment.is_important,
            },
            source="session_manager",
        )

        return segment

    async def mark_important(
        self, note: str | None = None, duration_seconds: int | None = None
    ) -> ImportantMarker:
        """Mark the current moment as important."""
        if not self._current_session:
            raise ValueError("No active session")

        duration = duration_seconds or self._settings.important_marker_duration

        marker = await self._repo.add_important_marker(
            session_id=self._current_session.id,
            meeting_id=self._current_meeting.id if self._current_meeting else None,
            duration_seconds=duration,
            note=note,
        )

        # Calculate time range for marking segments
        current_time = self.session_elapsed_seconds
        end_time = current_time + duration

        # Mark future segments as important (they'll be flagged as they come in)
        # This is handled by checking markers when adding segments

        await self._event_bus.emit(
            EventType.IMPORTANT_MARKED,
            {
                "marker_id": marker.id,
                "session_id": self._current_session.id,
                "meeting_id": self._current_meeting.id if self._current_meeting else None,
                "marked_at": marker.marked_at.isoformat(),
                "duration_seconds": duration,
            },
            source="session_manager",
        )

        return marker

    async def get_meeting_transcript(
        self, meeting_id: str, include_important_tags: bool = True
    ) -> str:
        """Get the full transcript for a meeting, optionally with important tags."""
        segments = await self._repo.get_segments(meeting_id=meeting_id)
        markers = await self._repo.get_important_markers(meeting_id=meeting_id)

        if not include_important_tags or not markers:
            return " ".join(s.text for s in segments)

        # Build transcript with important markers
        result = []
        in_important = False

        for segment in segments:
            is_important = self._is_segment_important(segment, markers)

            if is_important and not in_important:
                result.append("[IMPORTANT START]")
                in_important = True
            elif not is_important and in_important:
                result.append("[IMPORTANT END]")
                in_important = False

            result.append(segment.text)

        if in_important:
            result.append("[IMPORTANT END]")

        return " ".join(result)

    def _is_segment_important(
        self, segment: TranscriptSegment, markers: list[ImportantMarker]
    ) -> bool:
        """Check if a segment falls within an important marker window."""
        if segment.is_important:
            return True

        for marker in markers:
            marker_start = marker.marked_at.timestamp()
            marker_end = marker_start + marker.duration_seconds

            # Convert segment times (relative to session start) to absolute
            if self._session_start_time:
                segment_abs_start = (
                    self._session_start_time + timedelta(seconds=segment.start_time)
                ).timestamp()

                if marker_start <= segment_abs_start <= marker_end:
                    return True

        return False

    async def restore_session(self) -> Session | None:
        """Restore the last active session from database."""
        session = await self._repo.get_active_session()
        if session:
            self._current_session = session
            self._session_start_time = session.started_at

            # Check for active meeting
            meeting = await self._repo.get_active_meeting(session.id)
            if meeting:
                self._current_meeting = meeting

        return session
