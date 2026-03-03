"""Summarization backend manager."""

import asyncio
from typing import Callable, Awaitable, Optional

from config.settings import Settings, SummarizationBackend as SumBackendEnum, get_settings
from src.core.events import EventType, get_event_bus
from src.sessions.manager import SessionManager

from .anthropic_backend import AnthropicBackend
from .base import SummarizationBackend, SummarizationResult
from .ollama_backend import OllamaBackend
from .openai_backend import OpenAIBackend
from .prompts import get_prompt
from .pipeline.pipeline import run_pipeline, build_markdown_output
from .pipeline.types import PipelineResult


# Type alias for pipeline progress callback
PipelineProgressCallback = Callable[[str, str, float], Awaitable[None] | None]


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
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> SummarizationResult:
        """
        Generate a summary from transcript.

        Args:
            transcript: The transcript text to summarize
            prompt_type: Type of summary (if not providing custom prompts)
            custom_instructions: Optional custom instructions
            system_prompt: Optional system prompt override
            user_prompt: Optional user prompt override

        Returns:
            SummarizationResult with summary
        """
        if not self._initialized or self._active_backend is None:
            await self.initialize()

        # Get prompts
        if system_prompt is None or user_prompt is None:
            sys_prompt, usr_prompt = get_prompt(
                prompt_type=prompt_type,
                transcript=transcript,
                custom_instructions=custom_instructions,
            )
            system_prompt = system_prompt or sys_prompt
            user_prompt = user_prompt or usr_prompt

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
            result = await self._summarize_with_timeout(
                transcript=transcript,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
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
    ) -> PipelineResult:
        """Process transcript using the multi-stage pipeline.

        This method provides structured extraction (actions, decisions, risks,
        questions, follow-ups) plus a narrative summary that references all
        extracted items.

        Args:
            transcript: Full transcript text with timestamps
            template: Template type for narrative style
            progress_callback: Optional callback for progress updates (stage, message, progress)

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

        # Emit start event
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
            )

            # Emit completion event
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
            # Emit error event
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

    async def _summarize_with_timeout(
        self,
        transcript: str,
        system_prompt: str,
        user_prompt: str,
    ) -> SummarizationResult:
        if not self._active_backend:
            raise RuntimeError("No active summarization backend")

        timeout_seconds = int(self._settings.summarization_timeout_seconds)
        if timeout_seconds <= 0:
            return await self._active_backend.summarize(
                transcript=transcript,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

        try:
            return await asyncio.wait_for(
                self._active_backend.summarize(
                    transcript=transcript,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                ),
                timeout=float(timeout_seconds),
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Summarization model call timed out after {timeout_seconds} seconds"
            ) from exc
