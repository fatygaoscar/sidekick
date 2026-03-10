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

    async def chat_about_recording(
        self,
        *,
        question: str,
        current_summary: str | None,
        evidence_windows: list[dict[str, str | None]],
        recent_turns: list[dict[str, str]],
    ) -> dict[str, object]:
        """Answer a workspace chat turn with transcript-grounded context when available."""
        if not self._initialized or self._active_backend is None:
            await self.initialize()

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

        recent_turn_lines = []
        for turn in recent_turns[-6:]:
            role = "User" if str(turn.get("role") or "").lower() == "user" else "Assistant"
            content = str(turn.get("content") or "").strip()
            if content:
                recent_turn_lines.append(f"{role}: {content}")

        system_prompt = (
            "You are a meeting assistant helping the user interrogate and improve a meeting summary. "
            "Use transcript evidence windows for factual claims about the meeting. "
            "Do not invent facts, decisions, owners, dates, or risks that are not grounded in the evidence. "
            "You may comment on summary wording, structure, omissions, or clarity using the current summary. "
            "Return JSON only with keys: answer, citations, confidence, intent_label, intent_confidence, "
            "suggests_summary_change, suggested_change_kind, apply_ready. "
            "confidence must be high, medium, or low. "
            "intent_label must be one of add_missing_fact, remove_noise, tighten_wording, clarify_decision, "
            "clarify_action_item, clarify_owner, surface_risk, correct_inaccuracy, restructure, speaker_identity, "
            "general_qa, insufficient_evidence. "
            "suggested_change_kind must be one of add, remove, clarify, restructure, tighten, none. "
            "citations must be an array of integer evidence window indexes."
        )
        user_prompt = (
            f"User question: {question.strip()}\n\n"
            f"Current summary:\n{(current_summary or '').strip() or '(none)'}\n\n"
            f"Recent turns:\n{chr(10).join(recent_turn_lines) or '(none)'}\n\n"
            f"Evidence windows:\n{chr(10).join(evidence_lines) or '(none)'}\n\n"
            "Rules:\n"
            "- If the user is asking about factual meeting content, cite evidence indexes.\n"
            "- If the user is asking for a style or structure change to the summary, citations may be empty.\n"
            "- If evidence is insufficient for a factual claim, say so clearly.\n"
            "- Set apply_ready=true only when your response can be cleanly applied as a summary update.\n"
            "Return JSON only."
        )

        result = await self._summarize_with_timeout(
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(int(self._settings.ollama_context_length), 12288),
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(
                result.content,
                schema_hint=(
                    "answer, citations, confidence, intent_label, intent_confidence, "
                    "suggests_summary_change, suggested_change_kind, apply_ready"
                ),
            )
            parsed = self._parse_json_response(repair)

        if parsed is None:
            return {
                "answer": "I couldn’t produce a grounded chat answer for that.",
                "citations": [],
                "confidence": "low",
                "intent_label": self._infer_chat_intent(question),
                "intent_confidence": 0.2,
                "suggests_summary_change": self._question_suggests_summary_change(question),
                "suggested_change_kind": self._infer_change_kind(question),
                "apply_ready": bool(current_summary and self._question_suggests_summary_change(question)),
            }

        citations: list[int] = []
        for raw in parsed.get("citations", []):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= value < len(evidence_windows):
                citations.append(value)

        intent_label = self._normalize_chat_intent(parsed.get("intent_label"), question)
        suggested_change_kind = self._normalize_change_kind(
            parsed.get("suggested_change_kind"),
            question,
        )
        suggests_summary_change = bool(parsed.get("suggests_summary_change"))
        if not suggests_summary_change:
            suggests_summary_change = self._question_suggests_summary_change(question)
        apply_ready = bool(parsed.get("apply_ready")) and suggests_summary_change and bool(current_summary)

        confidence = str(parsed.get("confidence") or "low").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"

        try:
            intent_confidence = float(parsed.get("intent_confidence") or 0.0)
        except (TypeError, ValueError):
            intent_confidence = 0.0

        return {
            "answer": str(parsed.get("answer") or "").strip() or "I couldn’t answer that clearly.",
            "citations": citations,
            "confidence": confidence,
            "intent_label": intent_label,
            "intent_confidence": max(0.0, min(intent_confidence, 1.0)),
            "suggests_summary_change": suggests_summary_change,
            "suggested_change_kind": suggested_change_kind,
            "apply_ready": apply_ready,
        }

    async def apply_chat_turn_to_summary(
        self,
        *,
        instruction: str,
        current_summary: str,
        evidence_windows: list[dict[str, str | None]],
        assistant_answer: str,
        recent_turns: list[dict[str, str]],
    ) -> dict[str, object]:
        """Apply a transcript-grounded chat turn to an existing summary draft."""
        if not self._initialized or self._active_backend is None:
            await self.initialize()

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

        recent_turn_lines = []
        for turn in recent_turns[-6:]:
            role = "User" if str(turn.get("role") or "").lower() == "user" else "Assistant"
            content = str(turn.get("content") or "").strip()
            if content:
                recent_turn_lines.append(f"{role}: {content}")

        system_prompt = (
            "You edit a meeting summary in response to a chat instruction. "
            "Preserve section headers and unrelated facts unless the requested change requires otherwise. "
            "Use transcript evidence windows for factual additions or corrections. "
            "If the request is purely stylistic, you may edit using the current summary. "
            "If the evidence is insufficient for a factual change, do not invent details. "
            "Return JSON only with keys: revised_summary, changed, reason. "
            "changed must be true or false."
        )
        user_prompt = (
            f"Instruction: {instruction.strip()}\n\n"
            f"Assistant answer to apply:\n{assistant_answer.strip()}\n\n"
            f"Current summary:\n{current_summary.strip()}\n\n"
            f"Recent turns:\n{chr(10).join(recent_turn_lines) or '(none)'}\n\n"
            f"Evidence windows:\n{chr(10).join(evidence_lines) or '(none)'}\n\n"
            "Return JSON only."
        )

        result = await self._summarize_with_timeout(
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(int(self._settings.ollama_context_length), 12288),
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(
                result.content,
                schema_hint="revised_summary, changed, reason",
            )
            parsed = self._parse_json_response(repair)

        if parsed is None:
            fallback_content = str(result.content or "").strip()
            changed = bool(fallback_content and fallback_content != current_summary.strip())
            return {
                "revised_summary": fallback_content or current_summary.strip(),
                "changed": changed,
                "reason": None if changed else "The assistant did not return a grounded summary change.",
            }

        revised_summary = str(parsed.get("revised_summary") or current_summary).strip() or current_summary.strip()
        changed = bool(parsed.get("changed"))
        if revised_summary == current_summary.strip():
            changed = False
        reason = str(parsed.get("reason") or "").strip() or None
        return {
            "revised_summary": revised_summary,
            "changed": changed,
            "reason": reason,
        }

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

    async def _repair_json_response(
        self,
        invalid_response: str,
        *,
        schema_hint: str = "answer, citations, confidence",
    ) -> str:
        """Ask the backend to rewrite invalid output into the required JSON schema."""
        repair_prompt = (
            "Rewrite the following response as valid JSON only with keys "
            f"{schema_hint}.\n\n"
            f"{invalid_response.strip()}"
        )
        repaired = await self._summarize_with_timeout(
            transcript="",
            system_prompt="You repair malformed JSON responses. Return JSON only.",
            user_prompt=repair_prompt,
            num_ctx=2048,
        )
        return repaired.content

    def _normalize_chat_intent(self, value: object, question: str) -> str:
        allowed = {
            "add_missing_fact",
            "remove_noise",
            "tighten_wording",
            "clarify_decision",
            "clarify_action_item",
            "clarify_owner",
            "surface_risk",
            "correct_inaccuracy",
            "restructure",
            "speaker_identity",
            "general_qa",
            "insufficient_evidence",
        }
        normalized = str(value or "").strip().lower()
        if normalized in allowed:
            return normalized
        return self._infer_chat_intent(question)

    def _normalize_change_kind(self, value: object, question: str) -> str:
        allowed = {"add", "remove", "clarify", "restructure", "tighten", "none"}
        normalized = str(value or "").strip().lower()
        if normalized in allowed:
            return normalized
        return self._infer_change_kind(question)

    def _infer_chat_intent(self, question: str) -> str:
        text = str(question or "").strip().lower()
        if not text:
            return "general_qa"
        if any(term in text for term in ("speaker", "who said", "who owns")):
            return "speaker_identity"
        if any(term in text for term in ("missing", "left out", "didn't include", "add ")):
            return "add_missing_fact"
        if any(term in text for term in ("remove", "too much", "too verbose", "noise", "filler")):
            return "remove_noise"
        if any(term in text for term in ("tighten", "concise", "shorter", "trim")):
            return "tighten_wording"
        if "decision" in text:
            return "clarify_decision"
        if any(term in text for term in ("action item", "next step", "follow up")):
            return "clarify_action_item"
        if any(term in text for term in ("owner", "assigned", "responsible")):
            return "clarify_owner"
        if any(term in text for term in ("risk", "concern", "blocker")):
            return "surface_risk"
        if any(term in text for term in ("wrong", "incorrect", "inaccurate", "fix")):
            return "correct_inaccuracy"
        if any(term in text for term in ("restructure", "format", "section", "organize")):
            return "restructure"
        return "general_qa"

    def _infer_change_kind(self, question: str) -> str:
        text = str(question or "").strip().lower()
        if any(term in text for term in ("add", "missing", "include", "left out")):
            return "add"
        if any(term in text for term in ("remove", "cut", "delete")):
            return "remove"
        if any(term in text for term in ("clarify", "explain", "make explicit", "owner", "decision")):
            return "clarify"
        if any(term in text for term in ("restructure", "reorganize", "format", "section")):
            return "restructure"
        if any(term in text for term in ("tighten", "shorter", "concise", "trim")):
            return "tighten"
        return "none"

    def _question_suggests_summary_change(self, question: str) -> bool:
        text = str(question or "").strip().lower()
        return any(
            term in text
            for term in (
                "summary",
                "revise",
                "rewrite",
                "tighten",
                "shorter",
                "longer",
                "add",
                "remove",
                "include",
                "clarify",
                "restructure",
                "make it",
            )
        )
