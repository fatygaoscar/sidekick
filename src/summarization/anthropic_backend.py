"""Summarization backend using Anthropic Claude."""

from typing import Any

from config.settings import get_settings

from .base import SummarizationBackend, SummarizationResult
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE


class AnthropicBackend(SummarizationBackend):
    """Summarization backend using Anthropic Claude API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize Anthropic backend.

        Args:
            api_key: Anthropic API key (default from settings)
            model: Model to use (default from settings)
        """
        settings = get_settings()
        self._api_key = api_key or settings.anthropic_api_key
        self._model_name = model or settings.anthropic_summarization_model
        self._client: Any = None
        self._initialized = False

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def is_local(self) -> bool:
        return False

    async def initialize(self) -> None:
        """Initialize the Anthropic client."""
        if self._initialized:
            return

        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown the backend."""
        if self._client:
            await self._client.close()
        self._client = None
        self._initialized = False

    async def summarize(
        self,
        transcript: str,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> SummarizationResult:
        """Generate summary using Claude."""
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        response = await self._client.messages.create(
            model=self._model_name,
            max_tokens=2048,
            system=system,
            messages=[
                {"role": "user", "content": user},
            ],
        )

        # Extract text from response
        content = ""
        for block in response.content:
            if block.type == "text":
                content += block.text

        return SummarizationResult(
            content=content,
            backend=self.name,
            model=self._model_name,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
        )
