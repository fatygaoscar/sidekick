"""Summarization backend using Anthropic Claude."""

import time
from typing import Any

import httpx

from config.settings import get_settings

from src.core.exceptions import ConfigurationError

from .base import BackendProbeResult, SummarizationBackend, SummarizationResult
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
        if not self._api_key.strip():
            raise ConfigurationError("Anthropic is not configured: ANTHROPIC_API_KEY is empty")

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
        num_ctx: int | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> SummarizationResult:
        """Generate summary using Claude."""
        del num_ctx, json_mode
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        response = await self._client.messages.create(
            model=self._model_name,
            max_tokens=max_output_tokens or 2048,
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

    async def probe(self) -> BackendProbeResult:
        """Probe Anthropic readiness with a lightweight models call."""
        started = time.monotonic()
        if not self._api_key.strip():
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message="ANTHROPIC_API_KEY is not configured.",
            )
        try:
            async with httpx.AsyncClient(
                base_url="https://api.anthropic.com",
                timeout=20.0,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            ) as client:
                response = await client.get("/v1/models")
        except httpx.HTTPError as exc:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message=f"Anthropic probe failed: {exc}",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        if response.is_success:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=True,
                message="Ready",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        return BackendProbeResult(
            provider=self.name,
            model=self._model_name,
            ready=False,
            message=f"Anthropic probe failed with HTTP {response.status_code}.",
            latency_ms=(time.monotonic() - started) * 1000.0,
            details={"status_code": response.status_code},
        )
