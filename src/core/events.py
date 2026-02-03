"""Event bus for inter-component communication."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine
from uuid import UUID, uuid4


class EventType(str, Enum):
    """Event types for the event bus."""

    # Audio events
    AUDIO_CHUNK_RECEIVED = "audio.chunk_received"
    AUDIO_VAD_SPEECH_START = "audio.vad_speech_start"
    AUDIO_VAD_SPEECH_END = "audio.vad_speech_end"

    # Transcription events
    TRANSCRIPTION_STARTED = "transcription.started"
    TRANSCRIPTION_SEGMENT = "transcription.segment"
    TRANSCRIPTION_COMPLETED = "transcription.completed"
    TRANSCRIPTION_ERROR = "transcription.error"

    # Session events
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    SESSION_MODE_CHANGED = "session.mode_changed"

    # Meeting events
    MEETING_STARTED = "meeting.started"
    MEETING_ENDED = "meeting.ended"

    # Important marker events
    IMPORTANT_MARKED = "important.marked"

    # Summarization events
    SUMMARIZATION_STARTED = "summarization.started"
    SUMMARIZATION_COMPLETED = "summarization.completed"
    SUMMARIZATION_ERROR = "summarization.error"

    # WebSocket events
    WEBSOCKET_CONNECTED = "websocket.connected"
    WEBSOCKET_DISCONNECTED = "websocket.disconnected"


@dataclass
class Event:
    """Event object containing type, data, and metadata."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    event_id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str | None = None


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for pub/sub communication between components."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._all_handlers: list[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type."""
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe a handler to all events."""
        self._all_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe a handler from a specific event type."""
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    def unsubscribe_all(self, handler: EventHandler) -> None:
        """Unsubscribe a handler from all events."""
        if handler in self._all_handlers:
            self._all_handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribed handlers."""
        handlers = self._handlers[event.type] + self._all_handlers

        if not handlers:
            return

        # Run all handlers concurrently
        await asyncio.gather(
            *[self._safe_call(handler, event) for handler in handlers],
            return_exceptions=True,
        )

    async def _safe_call(self, handler: EventHandler, event: Event) -> None:
        """Safely call a handler, catching any exceptions."""
        try:
            await handler(event)
        except Exception as e:
            # Log but don't propagate handler exceptions
            print(f"Event handler error for {event.type}: {e}")

    async def emit(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> Event:
        """Convenience method to create and publish an event."""
        event = Event(
            type=event_type,
            data=data or {},
            source=source,
        )
        await self.publish(event)
        return event


# Global event bus instance
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def reset_event_bus() -> None:
    """Reset the global event bus (mainly for testing)."""
    global _event_bus
    _event_bus = None
