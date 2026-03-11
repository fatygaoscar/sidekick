"""Summarization backend using local Ollama."""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

from .base import BackendProbeResult, SummarizationBackend, SummarizationResult
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"^.*?</think>", re.DOTALL)
_SPECIAL_TOKEN_TAIL_RE = re.compile(r"<\|[^|]+\|>.*", re.DOTALL)


class OllamaBackend(SummarizationBackend):
    """Summarization backend using local Ollama."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._host = host or settings.ollama_host
        self._model_name = model or settings.ollama_model
        self._context_length = settings.ollama_context_length
        self._think = settings.ollama_think
        self._num_gpu = settings.ollama_num_gpu
        self._temperature = settings.ollama_temperature
        self._top_p = settings.ollama_top_p
        self._top_k = settings.ollama_top_k
        self._repeat_penalty = settings.ollama_repeat_penalty
        self._seed = settings.ollama_seed
        self._timeout_seconds = max(5, int(settings.summarization_timeout_seconds))
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

    @property
    def supports_context_override(self) -> bool:
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
        num_ctx: int | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> SummarizationResult:
        """Generate summary using Ollama."""
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        ctx_len = num_ctx or self._context_length
        options: dict[str, Any] = {
            "num_ctx": ctx_len,
            "num_gpu": self._num_gpu,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "top_k": self._top_k,
            "repeat_penalty": self._repeat_penalty,
        }
        if self._seed is not None:
            options["seed"] = self._seed
        if not self._think:
            options["think"] = False
        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens

        request_kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": options,
            "keep_alive": 0,
        }
        if json_mode:
            request_kwargs["format"] = "json"

        response = await self._client.chat(
            **request_kwargs,
        )

        content = response["message"]["content"]
        content = _THINK_BLOCK_RE.sub("", content)
        content = _ORPHAN_THINK_CLOSE_RE.sub("", content)
        content = _SPECIAL_TOKEN_TAIL_RE.sub("", content)
        content = content.strip()

        try:
            eval_count = response["eval_count"]
            eval_duration_s = response["eval_duration"] / 1e9
            tok_s = eval_count / eval_duration_s if eval_duration_s > 0 else 0
            logger.info("ollama: %d tok out, %.0f tok/s", eval_count, tok_s)
        except (KeyError, TypeError, ZeroDivisionError):
            pass

        return SummarizationResult(
            content=content,
            backend=self.name,
            model=self._model_name,
        )

    async def probe(self) -> BackendProbeResult:
        """Probe Ollama readiness by checking daemon reachability and model presence."""
        started = time.monotonic()
        parsed = urlparse(self._host)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else self._host

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=float(self._timeout_seconds)) as client:
                tags_response = await client.get("/api/tags")
                if tags_response.is_error:
                    return BackendProbeResult(
                        provider=self.name,
                        model=self._model_name,
                        ready=False,
                        message=f"Ollama probe failed with HTTP {tags_response.status_code}.",
                        latency_ms=(time.monotonic() - started) * 1000.0,
                        details={"host": self._host, "status_code": tags_response.status_code},
                    )
                show_response = await client.post("/api/show", json={"name": self._model_name})
        except httpx.TimeoutException:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message=f"Timed out reaching Ollama at {self._host}.",
                latency_ms=(time.monotonic() - started) * 1000.0,
                details={"host": self._host},
            )
        except httpx.HTTPError as exc:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=False,
                message=f"Could not reach Ollama at {self._host}: {exc}",
                latency_ms=(time.monotonic() - started) * 1000.0,
                details={"host": self._host},
            )

        if show_response.is_success:
            return BackendProbeResult(
                provider=self.name,
                model=self._model_name,
                ready=True,
                message="Ready",
                latency_ms=(time.monotonic() - started) * 1000.0,
                details={"host": self._host},
            )

        message = (
            f"Model '{self._model_name}' is not available on Ollama."
            if show_response.status_code == 404
            else f"Ollama model probe failed with HTTP {show_response.status_code}."
        )
        return BackendProbeResult(
            provider=self.name,
            model=self._model_name,
            ready=False,
            message=message,
            latency_ms=(time.monotonic() - started) * 1000.0,
            details={"host": self._host, "status_code": show_response.status_code},
        )
