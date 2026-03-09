"""Summarization backend manager."""

import asyncio
import json
import logging
import re
import time
from typing import Callable, Awaitable, Optional

from config.settings import Settings, SummarizationBackend as SumBackendEnum, get_settings
from src.core.events import EventType, get_event_bus
from src.sessions.manager import SessionManager

logger = logging.getLogger(__name__)

from .anthropic_backend import AnthropicBackend
from .base import SummarizationBackend, SummarizationResult
from .ollama_backend import OllamaBackend
from .openai_backend import OpenAIBackend
from .prompts import get_template_content, normalize_template_key
from .cohesive import generate_cohesive_summary
from .pipeline.pipeline import run_pipeline, build_markdown_output
from .pipeline.types import PipelineResult


# Type alias for pipeline progress callback
PipelineProgressCallback = Callable[[str, str, float], Awaitable[None] | None]
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"(\{.*\})", re.DOTALL)


class SummarizationManager:
    """Manages summarization backends and provides unified interface."""

    def __init__(self, settings: Settings | None = None) -> None:
        """
        Initialize summarization manager.

        Args:
            settings: Application settings (default from get_settings)
        """
        self._settings = settings or get_settings()
        self._event_bus = get_event_bus()
        self._backends: dict[SumBackendEnum, SummarizationBackend] = {}
        self._active_backend: SummarizationBackend | None = None
        self._initialized = False

    @property
    def active_backend(self) -> SummarizationBackend | None:
        """Get the currently active backend."""
        return self._active_backend

    @property
    def active_backend_type(self) -> SumBackendEnum | None:
        """Get the currently active backend type."""
        if self._active_backend is None:
            return None
        for backend_type, backend in self._backends.items():
            if backend is self._active_backend:
                return backend_type
        return None

    async def initialize(self, backend: SumBackendEnum | None = None) -> None:
        """
        Initialize summarization manager with specified or default backend.

        Args:
            backend: Backend to initialize (default from settings)
        """
        backend = backend or self._settings.summarization_backend

        # Create backend if not exists
        if backend not in self._backends:
            self._backends[backend] = self._create_backend(backend)

        # Initialize and set as active
        backend_instance = self._backends[backend]
        await backend_instance.initialize()
        self._active_backend = backend_instance
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown all backends."""
        for backend in self._backends.values():
            await backend.shutdown()
        self._backends.clear()
        self._active_backend = None
        self._initialized = False

    async def switch_backend(self, backend: SumBackendEnum) -> SummarizationBackend:
        """
        Switch to a different summarization backend.

        Args:
            backend: Backend to switch to

        Returns:
            The new active backend
        """
        if backend not in self._backends:
            self._backends[backend] = self._create_backend(backend)

        backend_instance = self._backends[backend]
        if not backend_instance._initialized:
            await backend_instance.initialize()

        self._active_backend = backend_instance
        return backend_instance

    async def summarize_meeting(
        self,
        session_manager: SessionManager,
        meeting_id: str,
        prompt_type: str = "default",
        custom_instructions: str | None = None,
    ) -> SummarizationResult:
        """
        Summarize a meeting by its ID.

        Args:
            session_manager: Session manager to get transcript
            meeting_id: Meeting ID to summarize
            prompt_type: Type of summary (default, quick, action_items, decisions)
            custom_instructions: Optional custom instructions

        Returns:
            SummarizationResult with summary
        """
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        # Get transcript with important markers
        transcript = await session_manager.get_meeting_transcript(
            meeting_id, include_important_tags=True
        )

        if not transcript.strip():
            return SummarizationResult(
                content="No transcript available for this meeting.",
                backend=self._active_backend.name,
                model=self._active_backend.model,
            )

        return await self.summarize(
            transcript=transcript,
            prompt_type=prompt_type,
            custom_instructions=custom_instructions,
        )

    async def summarize(
        self,
        transcript: str,
        prompt_type: str = "default",
        custom_instructions: str | None = None,
        perspective: str | None = None,
        attendees: str | None = None,
        include_structured_tables: bool = False,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> SummarizationResult:
        """
        Generate a summary from transcript.

        Args:
            transcript: The transcript text to summarize
            prompt_type: Type of summary (if not providing custom prompts)
            custom_instructions: Optional custom instructions
            perspective: Optional person/role focus for narrative prioritization
            system_prompt: Optional system prompt override
            user_prompt: Optional user prompt override

        Returns:
            SummarizationResult with summary
        """
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        ctx_len = int(self._settings.ollama_context_length)
        normalized_prompt_type = normalize_template_key(prompt_type)

        # Emit start event
        await self._event_bus.emit(
            EventType.SUMMARIZATION_STARTED,
            {
                "backend": self._active_backend.name,
                "model": self._active_backend.model,
                "transcript_length": len(transcript),
            },
            source="summarization_manager",
        )

        try:
            if system_prompt is not None or user_prompt is not None:
                # Explicit prompt overrides use the legacy one-pass path.
                result = await self._summarize_with_timeout(
                    transcript=transcript,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    num_ctx=ctx_len,
                )
            else:
                async def llm_call(sys_prompt: str, usr_prompt: str) -> str:
                    llm_result = await self._summarize_with_timeout(
                        transcript="",
                        system_prompt=sys_prompt,
                        user_prompt=usr_prompt,
                        num_ctx=ctx_len,
                    )
                    return llm_result.content

                template_contract = (
                    custom_instructions.strip()
                    if normalized_prompt_type == "custom" and custom_instructions
                    else get_template_content(normalized_prompt_type)
                )

                cohesive_text, _, _, _, speaker_map, prompt_audit = await generate_cohesive_summary(
                    llm_call=llm_call,
                    transcript=transcript,
                    template=normalized_prompt_type,
                    template_contract=template_contract,
                    perspective=perspective,
                    context_length=ctx_len,
                    custom_instructions=(
                        custom_instructions if normalized_prompt_type != "custom" else None
                    ),
                    include_structured_tables=include_structured_tables,
                    progress_callback=progress_callback,
                )
                result = SummarizationResult(
                    content=cohesive_text,
                    backend=self._active_backend.name,
                    model=self._active_backend.model,
                    speaker_map=speaker_map,
                    prompt_audit=prompt_audit,
                )

            # Emit completion event
            await self._event_bus.emit(
                EventType.SUMMARIZATION_COMPLETED,
                {
                    "backend": result.backend,
                    "model": result.model,
                    "summary_length": len(result.content),
                },
                source="summarization_manager",
            )

            return result

        except Exception as e:
            # Emit error event
            await self._event_bus.emit(
                EventType.SUMMARIZATION_ERROR,
                {
                    "backend": self._active_backend.name if self._active_backend else "unknown",
                    "error": str(e),
                },
                source="summarization_manager",
            )
            raise

    def _create_backend(self, backend: SumBackendEnum) -> SummarizationBackend:
        """Create a summarization backend for the specified type."""
        if backend == SumBackendEnum.OLLAMA:
            return OllamaBackend()
        elif backend == SumBackendEnum.OPENAI:
            return OpenAIBackend()
        elif backend == SumBackendEnum.ANTHROPIC:
            return AnthropicBackend()
        else:
            raise ValueError(f"Unknown summarization backend: {backend}")

    async def process_with_pipeline(
        self,
        transcript: str,
        template: str = "meeting",
        progress_callback: Optional[PipelineProgressCallback] = None,
        perspective: Optional[str] = None,
        template_prompt_override: Optional[str] = None,
        emit_events: bool = True,
    ) -> PipelineResult:
        """Process transcript using the multi-stage pipeline.

        This method provides structured extraction (actions, decisions, risks,
        questions, follow-ups) plus a narrative summary that references all
        extracted items.

        Args:
            transcript: Full transcript text with timestamps
            template: Summary template key (legacy experimental path)
            progress_callback: Optional callback for progress updates (stage, message, progress)
            perspective: Optional person/role to prioritize in narrative focus
            template_prompt_override: Optional per-export template prompt override

        Returns:
            PipelineResult with narrative, structured items, and metadata
        """
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        # Create LLM call wrapper for the pipeline
        async def llm_call(system_prompt: str, user_prompt: str) -> str:
            result = await self._summarize_with_timeout(
                transcript="",  # Not used when prompts are provided
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return result.content

        if emit_events:
            await self._event_bus.emit(
                EventType.SUMMARIZATION_STARTED,
                {
                    "backend": self._active_backend.name,
                    "model": self._active_backend.model,
                    "transcript_length": len(transcript),
                    "pipeline": True,
                },
                source="summarization_manager",
            )

        try:
            result = await run_pipeline(
                transcript=transcript,
                template=template,
                llm_call=llm_call,
                backend_name=self._active_backend.name,
                model_name=self._active_backend.model,
                progress_callback=progress_callback,
                perspective=perspective,
                narrative_strategy="template_native",
                template_prompt=template_prompt_override or get_template_content(template),
                llm_context_length=int(self._settings.ollama_context_length),
            )

            if emit_events:
                await self._event_bus.emit(
                    EventType.SUMMARIZATION_COMPLETED,
                    {
                        "backend": result.backend,
                        "model": result.model,
                        "narrative_length": len(result.narrative),
                        "items_extracted": len(result.items.all_items()),
                        "coverage_score": result.coverage_score,
                        "pipeline": True,
                    },
                    source="summarization_manager",
                )

            return result

        except Exception as e:
            if emit_events:
                await self._event_bus.emit(
                    EventType.SUMMARIZATION_ERROR,
                    {
                        "backend": self._active_backend.name if self._active_backend else "unknown",
                        "error": str(e),
                        "pipeline": True,
                    },
                    source="summarization_manager",
                )
            raise

    async def refine_summary(self, instruction: str, current_summary: str) -> str:
        """Apply a single AI revision pass to an existing summary.

        Context is just the summary + instruction — fast, ~10-20s vs 60-120s full pipeline.
        """
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        instr = instruction.strip()
        instr_preview = instr[:80] + ("..." if len(instr) > 80 else "")
        logger.info("[step] refine | start | instruction=%r", instr_preview)
        _t0 = time.monotonic()

        system_prompt = (
            "You are editing a meeting summary. Make only the requested change. "
            "Preserve all section headers (##), factual content, names, dates, and details "
            "you were not asked to change. Return only the revised summary in full."
        )
        user_prompt = f"Instruction: {instr}\n\nCurrent summary:\n{current_summary.strip()}"

        result = await self._summarize_with_timeout(
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        logger.info("[step] refine | done | elapsed=%.1fs", time.monotonic() - _t0)
        return result.content

    async def answer_question_with_citations(
        self,
        *,
        question: str,
        evidence_windows: list[dict[str, str | None]],
    ) -> dict[str, object]:
        """Answer a user question using only provided evidence windows."""
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        if not evidence_windows:
            return {
                "answer": "I didn’t find grounded transcript evidence for that.",
                "citations": [],
                "confidence": "low",
            }

        evidence_lines = []
        for index, window in enumerate(evidence_windows):
            evidence_lines.append(
                "\n".join(
                    [
                        f"[{index}] Recording: {window.get('recording_title') or 'Untitled Recording'}",
                        f"Recorded At: {window.get('recorded_at') or '-'}",
                        f"Speaker: {window.get('speaker') or '-'}",
                        f"Timestamp: {window.get('timestamp') or '-'}",
                        f"Snippet: {window.get('snippet') or ''}",
                    ]
                )
            )

        system_prompt = (
            "You answer questions about meeting recordings using only the provided evidence windows. "
            "Do not infer facts that are not grounded in the snippets. "
            "If the evidence is insufficient, say that clearly. "
            "Return JSON only with keys: answer, citations, confidence. "
            "confidence must be one of high, medium, low. "
            "citations must be an array of integer window indexes. "
            "Keep the answer concise and factual."
        )
        user_prompt = (
            f"Question: {question.strip()}\n\n"
            "Evidence windows:\n"
            f"{chr(10).join(evidence_lines)}\n\n"
            "Return JSON only."
        )

        result = await self._summarize_with_timeout(
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(int(self._settings.ollama_context_length), 8192),
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(result.content)
            parsed = self._parse_json_response(repair)
        if parsed is None:
            raise ValueError("Search answer model response was not valid JSON")

        answer = str(parsed.get("answer") or "").strip()
        confidence = str(parsed.get("confidence") or "low").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"

        citations: list[int] = []
        for raw in parsed.get("citations", []):
            try:
                citations.append(int(raw))
            except (TypeError, ValueError):
                continue

        return {
            "answer": answer,
            "citations": citations,
            "confidence": confidence,
        }

    async def _summarize_with_timeout(
        self,
        transcript: str,
        system_prompt: str,
        user_prompt: str,
        num_ctx: int | None = None,
    ) -> SummarizationResult:
        if not self._active_backend:
            raise RuntimeError("No active summarization backend")

        timeout_seconds = int(self._settings.summarization_timeout_seconds)
        if timeout_seconds <= 0:
            return await self._active_backend.summarize(
                transcript=transcript,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                num_ctx=num_ctx,
            )

        try:
            return await asyncio.wait_for(
                self._active_backend.summarize(
                    transcript=transcript,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    num_ctx=num_ctx,
                ),
                timeout=float(timeout_seconds),
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Summarization model call timed out after {timeout_seconds} seconds"
            ) from exc

    def _parse_json_response(self, content: str) -> dict[str, object] | None:
        text = str(content or "").strip()
        if not text:
            return None

        block_match = _JSON_BLOCK_RE.search(text)
        if block_match:
            text = block_match.group(1)
        elif not text.startswith("{"):
            object_match = _JSON_OBJECT_RE.search(text)
            if object_match:
                text = object_match.group(1)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None

        return parsed if isinstance(parsed, dict) else None

    async def _repair_json_response(self, invalid_response: str) -> str:
        """Ask the backend to rewrite invalid output into the required JSON schema."""
        repair_prompt = (
            "Rewrite the following response as valid JSON only with keys "
            "answer, citations, confidence. citations must be integer indexes. "
            "confidence must be high, medium, or low.\n\n"
            f"{invalid_response.strip()}"
        )
        repaired = await self._summarize_with_timeout(
            transcript="",
            system_prompt="You repair malformed JSON responses. Return JSON only.",
            user_prompt=repair_prompt,
            num_ctx=2048,
        )
        return repaired.content
