"""Abstract base interface for summarization backends."""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SummarizationResult:
    """Result from summarization backend."""

    content: str
    backend: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    speaker_map: dict[str, str] = field(default_factory=dict)
    prompt_audit: dict[str, str] = field(default_factory=dict)
    workflow_data: dict[str, object] = field(default_factory=dict)


@dataclass
class BackendProbeResult:
    """Structured readiness result for one backend."""

    provider: str
    model: str
    ready: bool
    message: str
    latency_ms: float | None = None
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the probe result for API responses."""
        return asdict(self)


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

    @property
    def supports_structured_outputs(self) -> bool:
        """Whether the backend can enforce structured JSON output."""
        return False

    @property
    def supports_context_override(self) -> bool:
        """Whether the backend honors runtime context-length overrides."""
        return False

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
        num_ctx: int | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> SummarizationResult:
        """
        Generate a summary from transcript.

        Args:
            transcript: The transcript text to summarize
            system_prompt: Optional system prompt override
            user_prompt: Optional user prompt template (use {transcript} placeholder)
            num_ctx: Optional context length override
            json_mode: Whether the backend should bias or enforce JSON output
            max_output_tokens: Optional completion/output token cap

        Returns:
            SummarizationResult with summary and metadata
        """
        pass

    @abstractmethod
    async def probe(self) -> BackendProbeResult:
        """Probe backend readiness without mutating manager state."""
        pass
