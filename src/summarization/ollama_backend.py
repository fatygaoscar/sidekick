"""Summarization backend using Ollama."""

from typing import Any

from config.settings import get_settings

from .base import SummarizationBackend, SummarizationResult
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE


class OllamaBackend(SummarizationBackend):
    """Summarization backend using local Ollama."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize Ollama backend.

        Args:
            host: Ollama server URL (default from settings)
            model: Model to use (default from settings)
        """
        settings = get_settings()
        self._host = host or settings.ollama_host
        self._model_name = model or settings.ollama_model
        self._client: Any = None
        self._initialized = False

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def is_local(self) -> bool:
        return True

    async def initialize(self) -> None:
        """Initialize the Ollama client."""
        if self._initialized:
            return

        import ollama

        self._client = ollama.AsyncClient(host=self._host)
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the backend."""
        self._client = None
        self._initialized = False

    async def summarize(
        self,
        transcript: str,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> SummarizationResult:
        """Generate summary using Ollama."""
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        response = await self._client.chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        content = response["message"]["content"]

        # Ollama doesn't provide token counts in the same way
        return SummarizationResult(
            content=content,
            backend=self.name,
            model=self._model_name,
        )
