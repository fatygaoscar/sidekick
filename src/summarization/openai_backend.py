"""Summarization backend using OpenAI."""

from __future__ import annotations

import time
from typing import Any

import httpx

from config.settings import get_settings
from src.core.exceptions import ConfigurationError, SummarizationError

from .base import BackendProbeResult, SummarizationBackend, SummarizationResult
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE


class OpenAIBackend(SummarizationBackend):
    """Summarization backend using OpenAI API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.openai_api_key
        self._model_name = model or settings.openai_summarization_model
        self._timeout_seconds = int(settings.openai_timeout_seconds)
        self._max_retries = int(settings.openai_max_retries)
        self._temperature = float(settings.openai_summarization_temperature)
        self._max_output_tokens = int(settings.openai_summarization_max_output_tokens)
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

    @property
    def supports_structured_outputs(self) -> bool:
        return True

    async def initialize(self) -> None:
        """Initialize the OpenAI client."""
        if self._initialized:
            return
        if not self._api_key.strip():
            raise ConfigurationError("OpenAI is not configured: OPENAI_API_KEY is empty")

        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            timeout=float(self._timeout_seconds),
            max_retries=self._max_retries,
        )
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
        """Generate summary using OpenAI."""
        del num_ctx
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        request_kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._temperature,
            "max_tokens": max_output_tokens or self._max_output_tokens,
        }
        if json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}

        try:
            response = await self._client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            raise SummarizationError(self._normalize_exception(exc)) from exc

        content = response.choices[0].message.content or ""

        return SummarizationResult(
            content=content,
            backend=self.name,
            model=self._model_name,
            prompt_tokens=response.usage.prompt_tokens if response.usage else None,
            completion_tokens=response.usage.completion_tokens if response.usage else None,
        )

    async def probe(self) -> BackendProbeResult:
        """Probe OpenAI readiness with a lightweight model lookup."""
        started = time.monotonic()
        if not self._api_key.strip():
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message="OPENAI_API_KEY is not configured.",
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"https://api.openai.com/v1/models/{self._model_name}"
        try:
            async with httpx.AsyncClient(timeout=float(self._timeout_seconds)) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message=f"Timed out reaching OpenAI within {self._timeout_seconds}s.",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )
        except httpx.HTTPError as exc:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message=f"OpenAI probe failed: {exc}",
                latency_ms=(time.monotonic() - started) * 1000.0,
            )

        request_id = response.headers.get("x-request-id")
        if response.is_success:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=True,
                message="Ready",
                latency_ms=(time.monotonic() - started) * 1000.0,
                request_id=request_id,
            )

        if response.status_code == 401:
            message = "OpenAI rejected the API key."
        elif response.status_code == 404:
            message = f"OpenAI model '{self._model_name}' is unavailable."
        elif response.status_code == 429:
            message = "OpenAI rate limited the readiness check."
        else:
            message = f"OpenAI probe failed with HTTP {response.status_code}."

        return BackendProbeResult(
            provider=self.name,
            model=self._model_name,
            ready=False,
            message=message,
            latency_ms=(time.monotonic() - started) * 1000.0,
            request_id=request_id,
            details={"status_code": response.status_code},
        )

    def _normalize_exception(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        request_id = getattr(exc, "request_id", None)
        if request_id:
            return f"{message} (request_id={request_id})"
        return message
