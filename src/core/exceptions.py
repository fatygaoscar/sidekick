"""Custom exceptions for Sidekick."""


class SidekickError(Exception):
    """Base exception for all Sidekick errors."""

    pass


class AudioError(SidekickError):
    """Error related to audio capture or processing."""

    pass


class TranscriptionError(SidekickError):
    """Error during transcription."""

    pass


class SummarizationError(SidekickError):
    """Error during summarization."""

    pass


class SessionError(SidekickError):
    """Error related to session management."""

    pass


class MeetingError(SidekickError):
    """Error related to meeting management."""

    pass


class DatabaseError(SidekickError):
    """Error related to database operations."""

    pass


class ConfigurationError(SidekickError):
    """Error related to configuration."""

    pass


class WebSocketError(SidekickError):
    """Error related to WebSocket communication."""

    pass
