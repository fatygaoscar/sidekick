"""Summarization backend using OpenAI."""

from typing import Any

from config.settings import get_settings

from .base import SummarizationBackend, SummarizationResult
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE


class OpenAIBackend(SummarizationBackend):
    """Summarization backend using OpenAI API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize OpenAI backend.

        Args:
            api_key: OpenAI API key (default from settings)
            model: Model to use (default from settings)
        """
        settings = get_settings()
        self._api_key = api_key or settings.openai_api_key
        self._model_name = model or settings.openai_summarization_model
        self._client: Any = None
        self._initialized = False

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def is_local(self) -> bool:
        return False

    async def initialize(self) -> None:
        """Initialize the OpenAI client."""
        if self._initialized:
            return

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=self._api_key)
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
        """Generate summary using OpenAI."""
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        response = await self._client.chat.completions.create(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.7,
            max_tokens=2048,
        )

        content = response.choices[0].message.content or ""

        return SummarizationResult(
            content=content,
            backend=self.name,
            model=self._model_name,
            prompt_tokens=response.usage.prompt_tokens if response.usage else None,
            completion_tokens=response.usage.completion_tokens if response.usage else None,
        )
