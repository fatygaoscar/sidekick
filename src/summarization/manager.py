"""Summarization backend manager."""

import asyncio
import json
import logging
import re
import time
from typing import Callable, Awaitable, Optional

from config.settings import (
    Settings,
    SummarizationBackend as SumBackendEnum,
    SummarizationPipelineStrategy as PipelineStrategyEnum,
    get_settings,
)
from src.core.events import EventType, get_event_bus
from src.sessions.manager import SessionManager

logger = logging.getLogger(__name__)

from .anthropic_backend import AnthropicBackend
from .base import BackendProbeResult, SummarizationBackend, SummarizationResult
from .ollama_backend import OllamaBackend
from .openai_backend import OpenAIBackend
from .prompts import DEFAULT_TEMPLATE_KEY, get_template_content, normalize_template_key
from .cohesive import generate_cohesive_summary
from .pipeline.pipeline import run_pipeline
from .pipeline.types import PipelineResult
from .topic_segmented import (
    build_topic_identification_prompts,
    build_topic_extraction_prompts,
    build_topic_segmented_summary,
    evaluate_topic_quality,
    normalize_topic_payload,
    normalize_identified_topics,
    prepare_business_transcript,
    render_topic_segmented_markdown,
    segment_transcript,
)


# Type alias for pipeline progress callback
PipelineProgressCallback = Callable[[str, str, float], Awaitable[None] | None]
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"(\{.*\})", re.DOTALL)
_TRANSCRIPT_LINE_RE = re.compile(r"^\[(?P<timestamp>\d{2}:\d{2})\]\s+(?P<body>.+)$")
_TRANSCRIPT_BODY_RE = re.compile(r"^(?:(?P<speaker>[^:]+):\s+)?(?P<text>.+)$")
_REVISION_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "make",
    "more",
    "not",
    "of",
    "on",
    "or",
    "our",
    "please",
    "revise",
    "rewrite",
    "should",
    "so",
    "summary",
    "that",
    "the",
    "this",
    "to",
    "up",
    "use",
    "we",
    "with",
}
_STYLE_ONLY_HINTS = (
    "brief",
    "bullets",
    "clearer",
    "concise",
    "executive",
    "format",
    "grammar",
    "polish",
    "professional",
    "reorganize",
    "rephrase",
    "rewrite",
    "shorter",
    "simpler",
    "structure",
    "tighten",
    "tone",
)
_EVIDENCE_NEEDED_HINTS = (
    "add",
    "blocker",
    "decision",
    "detail",
    "example",
    "expand",
    "include",
    "missed",
    "missing",
    "owner",
    "risk",
    "technical",
    "timeline",
    "what did",
    "what happened",
    "what they said",
    "why",
)


def classify_revision_route(instruction: str) -> str:
    """Classify whether a revise request needs transcript evidence."""
    normalized = " ".join((instruction or "").strip().lower().split())
    if not normalized:
        return "style_only"
    if any(hint in normalized for hint in _EVIDENCE_NEEDED_HINTS):
        return "evidence_needed"
    if any(hint in normalized for hint in _STYLE_ONLY_HINTS):
        return "style_only"
    return "uncertain"


def _extract_significant_terms(text: str, *, min_length: int = 4, limit: int = 24) -> list[str]:
    counts: dict[str, int] = {}
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", text or ""):
        normalized = token.lower()
        if len(normalized) < min_length or normalized in _REVISION_STOPWORDS:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [token for token, _count in ranked[:limit]]


def _extract_summary_headings(summary: str) -> list[str]:
    headings: list[str] = []
    for line in (summary or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            headings.append(stripped.lstrip("#").strip())
    return headings


def select_revision_evidence_windows(
    *,
    transcript: str,
    instruction: str,
    current_summary: str,
    route: str,
    max_windows: int = 8,
) -> list[dict[str, object]]:
    """Select compact transcript windows for revise requests."""
    transcript_text = (transcript or "").strip()
    if not transcript_text:
        return []
    if route == "style_only":
        return []

    if len(transcript_text) <= 12000:
        return [
            {
                "timestamp": None,
                "speaker": None,
                "snippet": transcript_text,
                "line_count": len([line for line in transcript_text.splitlines() if line.strip()]),
            }
        ]

    lines = [line.strip() for line in transcript_text.splitlines() if line.strip()]
    if not lines:
        return []

    query_terms = _extract_significant_terms(instruction, min_length=3, limit=18)
    heading_terms = _extract_significant_terms(" ".join(_extract_summary_headings(current_summary)), limit=10)
    if not query_terms and not heading_terms:
        return []

    window_size = 10 if len(lines) > 60 else 8
    overlap = 4 if len(lines) > 30 else 3
    step = max(1, window_size - overlap)
    candidates: list[dict[str, object]] = []
    seen_texts: set[str] = set()

    for start in range(0, len(lines), step):
        chunk = lines[start:start + window_size]
        if not chunk:
            continue
        chunk_text = "\n".join(chunk)
        normalized_chunk = chunk_text.lower()
        query_hits = sum(normalized_chunk.count(term) for term in query_terms)
        heading_hits = sum(normalized_chunk.count(term) for term in heading_terms)
        if query_hits == 0 and heading_hits == 0:
            continue

        first_match = _TRANSCRIPT_LINE_RE.match(chunk[0])
        body_match = _TRANSCRIPT_BODY_RE.match(first_match.group("body")) if first_match else None
        snippet = chunk_text
        if snippet in seen_texts:
            continue
        seen_texts.add(snippet)
        candidates.append(
            {
                "score": (query_hits * 5) + heading_hits + min(len(chunk_text) / 500.0, 2.0),
                "timestamp": first_match.group("timestamp") if first_match else None,
                "speaker": (
                    body_match.group("speaker").strip()
                    if body_match and body_match.group("speaker")
                    else None
                ),
                "snippet": snippet,
                "line_count": len(chunk),
                "start_index": start,
            }
        )

    ranked = sorted(
        candidates,
        key=lambda item: (-float(item["score"]), int(item["start_index"])),
    )
    selected: list[dict[str, object]] = []
    used_ranges: list[tuple[int, int]] = []
    for candidate in ranked:
        start = int(candidate["start_index"])
        end = start + int(candidate["line_count"])
        if any(not (end <= other_start or start >= other_end) for other_start, other_end in used_ranges):
            continue
        selected.append(
            {
                "timestamp": candidate["timestamp"],
                "speaker": candidate["speaker"],
                "snippet": candidate["snippet"],
                "line_count": candidate["line_count"],
            }
        )
        used_ranges.append((start, end))
        if len(selected) >= max_windows:
            break
    return selected


def _extract_summary_headers(text: str) -> list[str]:
    headers: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            headers.append(stripped.lstrip("#").strip())
    return headers


def _contains_markdown_table(text: str) -> bool:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    for index in range(len(lines) - 1):
        if "|" not in lines[index] or "|" not in lines[index + 1]:
            continue
        separator = lines[index + 1].replace("|", "").replace("-", "").replace(":", "").replace(" ", "")
        if separator == "":
            return True
    return False


def evaluate_revision_structure(
    *,
    original_summary: str,
    revised_summary: str,
    template_key: str,
) -> tuple[bool, str | None]:
    """Reject only clearly degraded structure; allow useful drift."""
    original_headers = _extract_summary_headers(original_summary)
    revised_headers = _extract_summary_headers(revised_summary)
    original_has_table = _contains_markdown_table(original_summary)
    revised_has_table = _contains_markdown_table(revised_summary)
    normalized_template = normalize_template_key(template_key or DEFAULT_TEMPLATE_KEY)

    if len(original_headers) >= 2 and len(revised_headers) == 0:
        return False, "The revision removed all section headers from a previously structured summary."
    if len(original_headers) >= 3 and len(revised_headers) == 1:
        return False, "The revision collapsed most of the summary structure into a single section."
    if "## Action Items" in revised_summary and not revised_has_table:
        return False, "The revision kept Action Items but degraded the markdown table structure."
    if (
        original_has_table
        and not revised_has_table
        and "## Action Items" in original_summary
        and "## Action Items" in revised_summary
        and normalized_template == "meeting"
    ):
        return False, "The revision kept Action Items but removed the markdown table."

    nonempty_lines = [line.strip() for line in revised_summary.splitlines() if line.strip()]
    if len(revised_headers) == 0 and len(nonempty_lines) <= 3:
        return False, "The revision became too flat to be a useful meeting summary."

    if len(revised_headers) == 0:
        bullet_lines = [line for line in nonempty_lines if line.startswith("-") or line.startswith("|")]
        if len(bullet_lines) < max(2, len(nonempty_lines) // 3):
            return False, "The revision lost too much markdown structure and scannability."

    return True, None


def _build_template_guidance(
    *,
    template_key: str,
    custom_prompt: str | None,
    current_summary: str,
) -> str:
    normalized_template = normalize_template_key(template_key or DEFAULT_TEMPLATE_KEY)
    if normalized_template == "custom":
        base_guidance = (custom_prompt or "").strip() or "Preserve the summary's existing custom structure."
    else:
        base_guidance = get_template_content(normalized_template).strip()
        extra = (custom_prompt or "").strip()
        if extra:
            base_guidance = f"{base_guidance}\n\nAdditional recording-specific guidance:\n{extra}"

    current_headers = _extract_summary_headers(current_summary)
    current_header_block = "\n".join(f"- {header}" for header in current_headers) or "- No stable headers detected"
    return (
        f"Active template key: {normalized_template}\n\n"
        f"Template guidance:\n{base_guidance}\n\n"
        "Current summary structure:\n"
        f"{current_header_block}"
    )


def _build_revise_prompt(
    *,
    instruction: str,
    current_summary: str,
    template_guidance: str,
    evidence_lines: list[str],
    route: str,
) -> tuple[str, str]:
    uses_evidence = bool(evidence_lines)
    system_prompt = (
        "You are editing a meeting summary for clarity, accuracy, and scannability. "
        "Use the active template as a structural guide, not as a rigid checklist. "
        "Preserve useful markdown structure such as headings, bullets, and tables when they improve readability. "
        "You may merge, omit, or tighten sections if they are duplicative, empty, or low-signal for this meeting. "
        "Do not flatten the summary into generic prose. "
        "Do not invent facts, decisions, owners, dates, blockers, or examples. "
        "If Action Items remain useful, keep them as a markdown table. "
        "Return JSON only with keys: revised_summary, changed, reason. changed must be true or false."
    )
    user_prompt = (
        f"Instruction: {instruction}\n\n"
        f"{template_guidance}\n\n"
        f"Current summary:\n{current_summary.strip()}\n\n"
        f"Revise route: {route}\n\n"
    )
    if uses_evidence:
        user_prompt += f"Evidence windows:\n{chr(10).join(evidence_lines)}\n\n"
    else:
        user_prompt += "Evidence windows:\n(none)\n\n"
    user_prompt += (
        "Requirements:\n"
        "- Keep the summary grounded in the current summary and any supplied evidence.\n"
        "- Preserve or improve structure and readability.\n"
        "- You may intelligently merge or drop sections that do not help the reader.\n"
        "- Do not rigidly force every original template section back in if it does not fit this meeting.\n"
        "- Return the full revised summary.\n"
        "Return JSON only."
    )
    return system_prompt, user_prompt


def _build_structure_repair_prompt(
    *,
    template_guidance: str,
    original_summary: str,
    revised_summary: str,
) -> tuple[str, str]:
    system_prompt = (
        "You are repairing the structure of a meeting summary without changing its meaning. "
        "Restore scannability using headings, bullets, spacing, and tables where appropriate. "
        "Use the template as guidance, not as a rigid schema. "
        "Do not force every original section back in if a merged or simplified structure is clearer. "
        "Do not add new facts. Return JSON only with keys: revised_summary, changed, reason."
    )
    user_prompt = (
        f"{template_guidance}\n\n"
        f"Original summary before revise:\n{original_summary.strip()}\n\n"
        f"Current revised summary that needs structure repair:\n{revised_summary.strip()}\n\n"
        "Repair goals:\n"
        "- Restore markdown structure and readability.\n"
        "- Keep meaningful sections and tables when they help the reader.\n"
        "- Allow merged or omitted sections if they are clearer than the original template shape.\n"
        "- Do not collapse everything into prose.\n"
        "Return JSON only."
    )
    return system_prompt, user_prompt


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
        self._selected_backend_type = self._settings.summarization_backend
        self._default_pipeline_strategy = self._settings.summarization_pipeline_strategy
        self._active_backend: SummarizationBackend | None = None
        self._switch_lock = asyncio.Lock()
        self._initialized = False

    @property
    def active_backend(self) -> SummarizationBackend | None:
        """Get the currently active backend."""
        return self._backends.get(self._selected_backend_type, self._active_backend)

    @property
    def active_backend_type(self) -> SumBackendEnum | None:
        """Get the currently active backend type."""
        return self._selected_backend_type

    @property
    def default_pipeline_strategy(self) -> PipelineStrategyEnum:
        """Get the runtime default summarization pipeline strategy."""
        return self._default_pipeline_strategy

    def set_selected_backend(self, backend: SumBackendEnum | str) -> None:
        """Update the selected backend without forcing immediate initialization."""
        self._selected_backend_type = self._coerce_backend_type(backend)
        self._active_backend = self._backends.get(self._selected_backend_type)

    def set_default_pipeline_strategy(
        self,
        strategy: PipelineStrategyEnum | str,
    ) -> None:
        """Update the runtime default pipeline strategy."""
        self._default_pipeline_strategy = self._coerce_pipeline_strategy(strategy)

    async def initialize(self, backend: SumBackendEnum | None = None) -> None:
        """
        Initialize summarization manager with specified or default backend.

        Args:
            backend: Backend to initialize (default from settings)
        """
        backend = self._coerce_backend_type(backend or self._selected_backend_type)
        self._selected_backend_type = backend

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
        backend = self._coerce_backend_type(backend)
        async with self._switch_lock:
            backend_instance = await self._ensure_backend(backend)
            self._selected_backend_type = backend
            self._active_backend = backend_instance
            self._initialized = True
            return backend_instance

    async def probe_backend(self, backend: SumBackendEnum | str) -> BackendProbeResult:
        """Probe backend readiness."""
        backend_type = self._coerce_backend_type(backend)
        backend_instance = self._backends.get(backend_type) or self._create_backend(backend_type)
        if backend_type not in self._backends:
            self._backends[backend_type] = backend_instance
        return await backend_instance.probe()

    async def diagnose_backends(
        self,
        backends: list[SumBackendEnum] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Probe multiple backends for settings-page diagnostics."""
        selected = backends or [SumBackendEnum.OLLAMA, SumBackendEnum.OPENAI]
        results: dict[str, dict[str, object]] = {}
        for backend in selected:
            probe = await self.probe_backend(backend)
            results[backend.value] = probe.to_dict()
        return results

    def describe_backends(self) -> dict[str, dict[str, object]]:
        """Return read-only provider metadata for the settings UI."""
        return {
            SumBackendEnum.OLLAMA.value: {
                "label": "Local qwen3:8b",
                "model": self._settings.ollama_model,
                "configured": bool(self._settings.ollama_host and self._settings.ollama_model),
                "host": self._settings.ollama_host,
            },
            SumBackendEnum.OPENAI.value: {
                "label": "OpenAI",
                "model": self._settings.openai_summarization_model,
                "configured": bool(self._settings.openai_api_key.strip()),
            },
        }

    def runtime_state(self) -> dict[str, object]:
        """Return active selection state for the settings UI."""
        active_backend = self._backends.get(self._selected_backend_type)
        return {
            "selected_backend": self._selected_backend_type.value,
            "selected_pipeline_strategy": self._default_pipeline_strategy.value,
            "active_backend": (
                self._selected_backend_type.value if active_backend is not None else None
            ),
            "applies_to": "new_requests_only",
            "providers": self.describe_backends(),
            "pipeline_strategies": [
                PipelineStrategyEnum.COHESIVE.value,
                PipelineStrategyEnum.TOPIC_SEGMENTED_V1.value,
            ],
        }

    async def summarize_meeting(
        self,
        session_manager: SessionManager,
        meeting_id: str,
        prompt_type: str = "default",
        custom_instructions: str | None = None,
        pipeline_strategy: PipelineStrategyEnum | str | None = None,
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
        backend = await self._backend_for_operation()

        # Get transcript with important markers
        transcript = await session_manager.get_meeting_transcript(
            meeting_id, include_important_tags=True
        )

        if not transcript.strip():
            return SummarizationResult(
                content="No transcript available for this meeting.",
                backend=backend.name,
                model=backend.model,
            )

        return await self.summarize(
            transcript=transcript,
            prompt_type=prompt_type,
            custom_instructions=custom_instructions,
            pipeline_strategy=pipeline_strategy,
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
        pipeline_strategy: PipelineStrategyEnum | str | None = None,
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
        backend = await self._backend_for_operation()
        ctx_len = self._context_length()
        normalized_prompt_type = normalize_template_key(prompt_type)
        resolved_pipeline_strategy = self._coerce_pipeline_strategy(
            pipeline_strategy or self._default_pipeline_strategy
        )

        # Emit start event
        await self._event_bus.emit(
            EventType.SUMMARIZATION_STARTED,
            {
                "backend": backend.name,
                "model": backend.model,
                "transcript_length": len(transcript),
            },
            source="summarization_manager",
        )

        try:
            if system_prompt is not None or user_prompt is not None:
                # Explicit prompt overrides use the legacy one-pass path.
                result = await self._summarize_with_timeout(
                    backend=backend,
                    transcript=transcript,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    num_ctx=ctx_len,
                )
            else:
                if resolved_pipeline_strategy == PipelineStrategyEnum.TOPIC_SEGMENTED_V1:
                    result = await self._summarize_topic_segmented(
                        backend=backend,
                        transcript=transcript,
                        progress_callback=progress_callback,
                    )
                else:
                    async def llm_call(sys_prompt: str, usr_prompt: str) -> str:
                        llm_result = await self._summarize_with_timeout(
                            backend=backend,
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
                        backend=backend.name,
                        model=backend.model,
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
                    "backend": backend.name,
                    "selected_backend": self._selected_backend_type.value,
                    "error": str(e),
                },
                source="summarization_manager",
            )
            raise

    async def _summarize_topic_segmented(
        self,
        *,
        backend: SummarizationBackend,
        transcript: str,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> SummarizationResult:
        """Summarize by segmenting the transcript into simple topics first."""
        def _emit(progress: float) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress)
            except Exception:
                pass

        _emit(0.05)
        business_turns, participants, speaker_map, segmentation_warnings, segmentation_context = prepare_business_transcript(transcript)
        _emit(0.14)
        if not business_turns:
            return SummarizationResult(
                content="## Summary\n\n- No transcript content was available for topic-segmented summarization.",
                backend=backend.name,
                model=backend.model,
                speaker_map=speaker_map,
                prompt_audit={
                    "topic_identification_system_prompt": "",
                    "topic_identification_user_prompt": "",
                    "pass1_system_prompt": "",
                    "pass1_user_prompt": "",
                    "pass2_system_prompt": "Deterministic topic-segmented markdown renderer.",
                    "pass2_user_prompt": "",
                },
                workflow_data={
                    "pipeline_version": PipelineStrategyEnum.TOPIC_SEGMENTED_V1.value,
                    "topic_segmented_summary": {
                        "schema_version": "topic_segmented_summary_v1",
                        "participants": [],
                        "meeting_summary": ["No transcript content was available."],
                        "topics": [],
                        "warnings": ["empty_transcript"],
                    },
                    "segmentation": {
                        "topics": [],
                        "warnings": ["empty_transcript"],
                        "boundary_version": "windowed_v2",
                        "label_strategy": "post_extract_llm_v1",
                        "business_start_index": 0,
                        "preamble_turn_ids": [],
                        "preamble_turn_count": 0,
                    },
                },
            )

        prompt_tokens_total = 0
        completion_tokens_total = 0
        extracted_topics: list[dict[str, object]] = []
        topic_quality: list[dict[str, object]] = []
        warnings: list[str] = [str(item.get("code") or "segmentation_warning") for item in segmentation_warnings]
        topic_identification_system_prompt = ""
        topic_identification_user_prompt = "Topic-identification prompt payload omitted from audit."
        extraction_system_prompt = ""
        extraction_user_prompt = "Per-topic extraction prompt payload omitted from audit."
        topic_identification_source = "llm_topics_v1"

        topic_identification_system_prompt, topic_identification_user_prompt = build_topic_identification_prompts(
            business_turns,
            participants,
            max_topics=6,
        )
        llm_topics: list[dict[str, object]] = []
        try:
            topic_result = await self._summarize_with_timeout(
                backend=backend,
                transcript="",
                system_prompt=topic_identification_system_prompt,
                user_prompt=topic_identification_user_prompt,
                num_ctx=min(self._context_length(), 16384),
                json_mode=backend.supports_structured_outputs,
                max_output_tokens=1800,
            )
            if topic_result.prompt_tokens is not None:
                prompt_tokens_total += int(topic_result.prompt_tokens)
            if topic_result.completion_tokens is not None:
                completion_tokens_total += int(topic_result.completion_tokens)
            parsed_topics = self._parse_json_response(topic_result.content)
            if parsed_topics is None:
                repaired_topics = await self._repair_json_response(
                    backend=backend,
                    invalid_response=topic_result.content,
                    schema_hint="topics, warnings",
                )
                parsed_topics = self._parse_json_response(repaired_topics)
                warnings.append("malformed_topic_identification_repaired")
            llm_topics, topic_identification_warnings = normalize_identified_topics(
                parsed_topics,
                business_turns,
                max_topics=6,
            )
            warnings.extend(topic_identification_warnings)
        except Exception:
            logger.exception("topic identification failed; falling back to deterministic segmentation")
            llm_topics = []
            warnings.append("topic_identification_failed")

        if llm_topics:
            topics = llm_topics
        else:
            topics, fallback_warnings, _fallback_participants, _fallback_speaker_map, fallback_context = segment_transcript(transcript)
            segmentation_warnings = fallback_warnings
            warnings.extend(str(item.get("code") or "segmentation_warning") for item in fallback_warnings)
            segmentation_context = fallback_context
            topic_identification_source = "deterministic_fallback"

        for index, topic in enumerate(topics):
            system_prompt, user_prompt = build_topic_extraction_prompts(topic, participants)
            extraction_system_prompt = extraction_system_prompt or system_prompt
            llm_result = await self._summarize_with_timeout(
                backend=backend,
                transcript="",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                num_ctx=min(self._context_length(), 16384),
                json_mode=backend.supports_structured_outputs,
                max_output_tokens=1800,
            )
            if llm_result.prompt_tokens is not None:
                prompt_tokens_total += int(llm_result.prompt_tokens)
            if llm_result.completion_tokens is not None:
                completion_tokens_total += int(llm_result.completion_tokens)

            parsed = self._parse_json_response(llm_result.content)
            if parsed is None:
                repaired = await self._repair_json_response(
                    backend=backend,
                    invalid_response=llm_result.content,
                    schema_hint="summary, decisions, action_items, milestones, unresolved_questions",
                )
                parsed = self._parse_json_response(repaired)
                warnings.append("malformed_extraction_repaired")

            normalized_topic, topic_warnings = normalize_topic_payload(topic, parsed)
            warnings.extend(topic_warnings)
            extracted_topics.append(normalized_topic)
            _emit(0.14 + (0.68 * ((index + 1) / max(len(topics), 1))))

        for topic, extracted_topic in zip(topics, extracted_topics):
            extracted_topic["label"] = str(topic.get("label") or extracted_topic.get("label") or "Discussion")
            quality = evaluate_topic_quality(extracted_topic, list(topic.get("_turns") or []))
            topic_quality.append(quality)
            warnings.extend(list(quality.get("warnings") or []))

        structured_payload = build_topic_segmented_summary(
            participants=participants,
            topics=extracted_topics,
            warnings=warnings,
        )
        content = render_topic_segmented_markdown(structured_payload)
        _emit(0.92)

        segmentation_metadata = {
            "topic_count": len(topics),
            "topics": [
                {
                    "topic_id": str(topic.get("topic_id") or ""),
                    "label": str(topic.get("label") or ""),
                    "why_this_is_a_topic": str(topic.get("why_this_is_a_topic") or ""),
                    "start_timestamp": topic.get("start_timestamp"),
                    "end_timestamp": topic.get("end_timestamp"),
                    "turn_ids": list(topic.get("turn_ids") or []),
                    "turn_count": int(topic.get("turn_count") or 0),
                }
                for topic in topics
            ],
            "warnings": segmentation_warnings,
            "boundary_version": "windowed_v2",
            "label_strategy": topic_identification_source,
            "topic_identification_version": "llm_topics_v1",
            "source": topic_identification_source,
            "business_start_index": int(segmentation_context.get("business_start_index") or 0),
            "preamble_turn_ids": list(segmentation_context.get("preamble_turn_ids") or []),
            "preamble_turn_count": int(segmentation_context.get("preamble_turn_count") or 0),
        }
        _emit(0.98)
        return SummarizationResult(
            content=content,
            backend=backend.name,
            model=backend.model,
            prompt_tokens=(prompt_tokens_total or None),
            completion_tokens=(completion_tokens_total or None),
            speaker_map=speaker_map,
            prompt_audit={
                "topic_identification_system_prompt": topic_identification_system_prompt,
                "topic_identification_user_prompt": topic_identification_user_prompt,
                "pass1_system_prompt": extraction_system_prompt,
                "pass1_user_prompt": extraction_user_prompt,
                "pass2_system_prompt": "Deterministic topic-segmented markdown renderer.",
                "pass2_user_prompt": "Render the extracted topic JSON into final Obsidian markdown without adding facts.",
            },
            workflow_data={
                "pipeline_version": PipelineStrategyEnum.TOPIC_SEGMENTED_V1.value,
                "topic_segmented_summary": structured_payload,
                "segmentation": segmentation_metadata,
                "topic_quality": topic_quality,
                "warnings": warnings,
            },
        )

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
        backend = await self._backend_for_operation()

        # Create LLM call wrapper for the pipeline
        async def llm_call(system_prompt: str, user_prompt: str) -> str:
            result = await self._summarize_with_timeout(
                backend=backend,
                transcript="",  # Not used when prompts are provided
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return result.content

        if emit_events:
            await self._event_bus.emit(
                EventType.SUMMARIZATION_STARTED,
                {
                    "backend": backend.name,
                    "model": backend.model,
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
                backend_name=backend.name,
                model_name=backend.model,
                progress_callback=progress_callback,
                perspective=perspective,
                narrative_strategy="template_native",
                template_prompt=template_prompt_override or get_template_content(template),
                llm_context_length=self._context_length(),
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
                        "backend": backend.name,
                        "error": str(e),
                        "pipeline": True,
                    },
                    source="summarization_manager",
                )
            raise

    async def refine_summary(
        self,
        *,
        instruction: str,
        current_summary: str,
        template_key: str = DEFAULT_TEMPLATE_KEY,
        custom_prompt: str | None = None,
        transcript: str | None = None,
        transcript_windows: list[dict[str, object]] | None = None,
        route: str | None = None,
    ) -> dict[str, object]:
        """Apply a single AI revision pass to an existing summary."""
        backend = await self._backend_for_operation()

        instr = instruction.strip()
        instr_preview = instr[:80] + ("..." if len(instr) > 80 else "")
        resolved_route = route or classify_revision_route(instr)
        evidence_windows = list(transcript_windows or [])
        used_transcript_context = bool(evidence_windows)
        logger.info(
            "[step] refine | start | route=%s | windows=%d | instruction=%r",
            resolved_route,
            len(evidence_windows),
            instr_preview,
        )
        _t0 = time.monotonic()
        if resolved_route != "style_only" and not evidence_windows and transcript:
            evidence_windows = select_revision_evidence_windows(
                transcript=transcript,
                instruction=instr,
                current_summary=current_summary,
                route=resolved_route,
            )
            used_transcript_context = bool(evidence_windows)

        evidence_lines: list[str] = []
        for index, window in enumerate(evidence_windows):
            evidence_lines.append(
                "\n".join(
                    [
                        f"[{index}] Timestamp: {window.get('timestamp') or '-'}",
                        f"Speaker: {window.get('speaker') or '-'}",
                        f"Snippet: {window.get('snippet') or ''}",
                    ]
                )
            )
        template_guidance = _build_template_guidance(
            template_key=template_key,
            custom_prompt=custom_prompt,
            current_summary=current_summary,
        )
        system_prompt, user_prompt = _build_revise_prompt(
            instruction=instr,
            current_summary=current_summary,
            template_guidance=template_guidance,
            evidence_lines=evidence_lines,
            route=resolved_route,
        )

        result = await self._summarize_with_timeout(
            backend=backend,
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(self._context_length(), 12288),
            json_mode=backend.supports_structured_outputs,
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(
                backend=backend,
                invalid_response=result.content,
                schema_hint="revised_summary, changed, reason",
            )
            parsed = self._parse_json_response(repair)

        if parsed is None:
            fallback_content = str(result.content or "").strip() or current_summary.strip()
            changed = fallback_content != current_summary.strip()
            structure_valid, structure_reason = evaluate_revision_structure(
                original_summary=current_summary,
                revised_summary=fallback_content,
                template_key=template_key,
            )
            logger.info("[step] refine | done | elapsed=%.1fs | parsed=false", time.monotonic() - _t0)
            return {
                "revised_summary": fallback_content,
                "changed": changed,
                "reason": structure_reason or (None if changed else "The assistant did not return a structured revision result."),
                "route": resolved_route,
                "used_transcript_context": used_transcript_context,
                "evidence_window_count": len(evidence_windows),
                "backend": backend.name,
                "model": result.model,
                "structure_valid": structure_valid,
                "template_guidance_used": True,
                "structure_repair_applied": False,
            }

        revised_summary = str(parsed.get("revised_summary") or current_summary).strip() or current_summary.strip()
        changed = bool(parsed.get("changed"))
        if revised_summary == current_summary.strip():
            changed = False
        reason = str(parsed.get("reason") or "").strip() or None
        structure_valid, structure_reason = evaluate_revision_structure(
            original_summary=current_summary,
            revised_summary=revised_summary,
            template_key=template_key,
        )
        structure_repair_applied = False
        if not structure_valid:
            repair_system_prompt, repair_user_prompt = _build_structure_repair_prompt(
                template_guidance=template_guidance,
                original_summary=current_summary,
                revised_summary=revised_summary,
            )
            repair_result = await self._summarize_with_timeout(
                backend=backend,
                transcript="",
                system_prompt=repair_system_prompt,
                user_prompt=repair_user_prompt,
                num_ctx=min(self._context_length(), 12288),
                json_mode=backend.supports_structured_outputs,
            )
            repair_parsed = self._parse_json_response(repair_result.content)
            if repair_parsed is None:
                repair_json = await self._repair_json_response(
                    backend=backend,
                    invalid_response=repair_result.content,
                    schema_hint="revised_summary, changed, reason",
                )
                repair_parsed = self._parse_json_response(repair_json)
            if repair_parsed is not None:
                repaired_summary = (
                    str(repair_parsed.get("revised_summary") or revised_summary).strip() or revised_summary
                )
                repaired_valid, repaired_reason = evaluate_revision_structure(
                    original_summary=current_summary,
                    revised_summary=repaired_summary,
                    template_key=template_key,
                )
                if repaired_valid:
                    revised_summary = repaired_summary
                    changed = repaired_summary != current_summary.strip()
                    reason = str(repair_parsed.get("reason") or reason or "").strip() or None
                    structure_valid = True
                    structure_reason = None
                    structure_repair_applied = True
                    result = repair_result
        if not structure_valid and structure_reason:
            reason = structure_reason
        logger.info(
            "[step] refine | done | elapsed=%.1fs | route=%s | windows=%d | changed=%s | structure_valid=%s",
            time.monotonic() - _t0,
            resolved_route,
            len(evidence_windows),
            changed,
            structure_valid,
        )
        return {
            "revised_summary": revised_summary,
            "changed": changed,
            "reason": reason,
            "route": resolved_route,
            "used_transcript_context": used_transcript_context,
            "evidence_window_count": len(evidence_windows),
            "backend": backend.name,
            "model": result.model,
            "structure_valid": structure_valid,
            "template_guidance_used": True,
            "structure_repair_applied": structure_repair_applied,
        }

    async def chat_about_recording(
        self,
        *,
        question: str,
        current_summary: str | None,
        evidence_windows: list[dict[str, str | None]],
        recent_turns: list[dict[str, str]],
    ) -> dict[str, object]:
        """Answer a workspace chat turn with transcript-grounded context when available."""
        backend = await self._backend_for_operation()

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
            backend=backend,
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(self._context_length(), 12288),
            json_mode=backend.supports_structured_outputs,
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(
                backend=backend,
                invalid_response=result.content,
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
        backend = await self._backend_for_operation()

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
            backend=backend,
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(self._context_length(), 12288),
            json_mode=backend.supports_structured_outputs,
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(
                backend=backend,
                invalid_response=result.content,
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
        backend = await self._backend_for_operation()

        if not evidence_windows:
            return {
                "answer": "I didn’t find grounded transcript evidence for that.",
                "citations": [],
                "confidence": "low",
                "answer_type": "insufficient_evidence",
                "reasoning_note": "No grounded transcript evidence was retrieved.",
                "follow_up_queries": [],
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
            "Return JSON only with keys: answer, citations, confidence, answer_type, reasoning_note, follow_up_queries. "
            "confidence must be one of high, medium, low. "
            "citations must be an array of integer window indexes. "
            "answer_type must be one of direct_answer, multi_recording, partial, insufficient_evidence. "
            "reasoning_note should be a short user-facing explanation, 1 sentence max. "
            "follow_up_queries should be an array with 0 to 3 short next-search suggestions. "
            "Keep the answer concise and factual."
        )
        user_prompt = (
            f"Question: {question.strip()}\n\n"
            "Evidence windows:\n"
            f"{chr(10).join(evidence_lines)}\n\n"
            "Return JSON only."
        )

        result = await self._summarize_with_timeout(
            backend=backend,
            transcript="",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            num_ctx=min(self._context_length(), 8192),
            json_mode=backend.supports_structured_outputs,
        )
        parsed = self._parse_json_response(result.content)
        if parsed is None:
            repair = await self._repair_json_response(backend=backend, invalid_response=result.content)
            parsed = self._parse_json_response(repair)
        if parsed is None:
            raise ValueError("Search answer model response was not valid JSON")

        answer = str(parsed.get("answer") or "").strip()
        confidence = str(parsed.get("confidence") or "low").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        answer_type = str(parsed.get("answer_type") or "partial").strip().lower()
        if answer_type not in {
            "direct_answer",
            "multi_recording",
            "partial",
            "insufficient_evidence",
        }:
            answer_type = "partial"
        reasoning_note = str(parsed.get("reasoning_note") or "").strip() or None

        citations: list[int] = []
        for raw in parsed.get("citations", []):
            try:
                citations.append(int(raw))
            except (TypeError, ValueError):
                continue

        follow_up_queries: list[str] = []
        for raw in parsed.get("follow_up_queries", []):
            normalized = " ".join(str(raw or "").strip().split())
            if normalized:
                follow_up_queries.append(normalized)
            if len(follow_up_queries) >= 3:
                break

        return {
            "answer": answer,
            "citations": citations,
            "confidence": confidence,
            "answer_type": answer_type,
            "reasoning_note": reasoning_note,
            "follow_up_queries": follow_up_queries,
        }

    async def _summarize_with_timeout(
        self,
        backend: SummarizationBackend,
        transcript: str,
        system_prompt: str,
        user_prompt: str,
        num_ctx: int | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> SummarizationResult:
        timeout_seconds = int(self._settings.summarization_timeout_seconds)
        if timeout_seconds <= 0:
            return await backend.summarize(
                transcript=transcript,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                num_ctx=num_ctx,
                json_mode=json_mode,
                max_output_tokens=max_output_tokens,
            )

        try:
            return await asyncio.wait_for(
                backend.summarize(
                    transcript=transcript,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    num_ctx=num_ctx,
                    json_mode=json_mode,
                    max_output_tokens=max_output_tokens,
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
        backend: SummarizationBackend,
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
            backend=backend,
            transcript="",
            system_prompt="You repair malformed JSON responses. Return JSON only.",
            user_prompt=repair_prompt,
            num_ctx=2048,
            json_mode=backend.supports_structured_outputs,
        )
        return repaired.content

    async def _backend_for_operation(self) -> SummarizationBackend:
        backend = await self._ensure_backend(self._selected_backend_type)
        self._active_backend = backend
        self._initialized = True
        return backend

    async def _ensure_backend(self, backend: SumBackendEnum) -> SummarizationBackend:
        backend = self._coerce_backend_type(backend)
        if backend not in self._backends:
            self._backends[backend] = self._create_backend(backend)
        backend_instance = self._backends[backend]
        if not backend_instance._initialized:
            await backend_instance.initialize()
        return backend_instance

    def _coerce_backend_type(self, backend: SumBackendEnum | str) -> SumBackendEnum:
        if isinstance(backend, SumBackendEnum):
            return backend
        return SumBackendEnum(str(backend).strip().lower())

    def _coerce_pipeline_strategy(
        self,
        strategy: PipelineStrategyEnum | str,
    ) -> PipelineStrategyEnum:
        if isinstance(strategy, PipelineStrategyEnum):
            return strategy
        return PipelineStrategyEnum(str(strategy).strip().lower())

    def _context_length(self) -> int:
        return int(self._settings.summarization_context_length)

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
