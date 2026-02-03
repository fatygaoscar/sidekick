"""Abstract base interface for summarization backends."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SummarizationResult:
    """Result from summarization backend."""

    content: str
    backend: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class SummarizationBackend(ABC):
    """Abstract base class for summarization backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the backend name."""
        pass

    @property
    @abstractmethod
    def model(self) -> str:
        """Get the model name."""
        pass

    @property
    @abstractmethod
    def is_local(self) -> bool:
        """Check if the backend runs locally."""
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the backend."""
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the backend."""
        pass

    @abstractmethod
    async def summarize(
        self,
        transcript: str,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> SummarizationResult:
        """
        Generate a summary from transcript.

        Args:
            transcript: The transcript text to summarize
            system_prompt: Optional system prompt override
            user_prompt: Optional user prompt template (use {transcript} placeholder)

        Returns:
            SummarizationResult with summary and metadata
        """
        pass
