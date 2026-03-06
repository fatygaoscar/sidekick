"""Summarization backend using Ollama."""

import logging
import re
from typing import Any

from config.settings import get_settings

logger = logging.getLogger(__name__)

from .base import SummarizationBackend, SummarizationResult
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
        """
        Initialize Ollama backend.

        Args:
            host: Ollama server URL (default from settings)
            model: Model to use (default from settings)
        """
        settings = get_settings()
        self._host = host or settings.ollama_host
        self._model_name = model or settings.ollama_model
        self._context_length = settings.ollama_context_length
        self._think = settings.ollama_think
        self._num_gpu = settings.ollama_num_gpu
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
        num_ctx: int | None = None,
    ) -> SummarizationResult:
        """Generate summary using Ollama."""
        if not self._initialized:
            await self.initialize()

        system = system_prompt or SYSTEM_PROMPT
        user = user_prompt or USER_PROMPT_TEMPLATE.format(transcript=transcript)

        ctx_len = num_ctx or self._context_length
        options: dict[str, Any] = {"num_ctx": ctx_len, "num_gpu": self._num_gpu, "temperature": 0.3}
        if not self._think:
            options["think"] = False

        response = await self._client.chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options=options,
            keep_alive=0,
        )

        content = response["message"]["content"]
        # Strip complete think blocks and tokenizer artifacts that can leak into output.
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
