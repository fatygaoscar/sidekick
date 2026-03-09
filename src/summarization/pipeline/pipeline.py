"""Concise extract -> rank -> filter -> render pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from typing import Awaitable, Callable, Optional

from .classifier import classify_items
from .entities import normalize_entities
from .extraction import extract_items_from_topic_segment
from .merger import merge_items, validate_owners
from .preprocess import preprocess_transcript
from .ranking import select_for_render
from .render_meeting import render_selection
from .structurer import assign_ids
from .types import MeetingContextProfile, PipelineResult, TopicSegment
from .validation import validate_items, validated_to_structured


LLMCallFunc = Callable[[str, str], Awaitable[str]]
ProgressCallback = Callable[[str, str, float], Awaitable[None] | None]
logger = logging.getLogger(__name__)


PROGRESS_WEIGHTS = {
    "preprocess": 0.10,
    "segmentation": 0.10,
    "extraction": 0.45,
    "validation": 0.15,
    "ranking": 0.10,
    "rendering": 0.10,
}

_STOPWORDS = {
    "the",
    "and",
    "that",
    "this",
    "with",
    "from",
    "have",
    "will",
    "need",
    "about",
    "there",
    "they",
    "them",
    "their",
    "into",
    "just",
    "because",
    "could",
    "should",
    "would",
    "what",
    "when",
    "where",
    "which",
    "while",
    "then",
    "than",
    "also",
    "like",
    "been",
    "were",
}


async def _emit_progress(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    progress: float,
) -> None:
    if callback:
        result = callback(stage, message, progress)
        if asyncio.iscoroutine(result):
            await result


def _cumulative_progress(stage: str, stage_progress: float) -> float:
    cumulative = 0.0
    for name, weight in PROGRESS_WEIGHTS.items():
        if name == stage:
            return cumulative + (weight * stage_progress)
        cumulative += weight
    return 1.0


def _default_context_profile(template: str) -> MeetingContextProfile:
    render_profile = "action_focused_v1" if template == "working_session" else "concise_default_v1"
    return MeetingContextProfile(
        ui_summary_mode=template or "auto",
        primary_mode="general_business",
        priority_weights={},
        render_profile=render_profile,
        mode_confidence=1.0,
        reasoning_summary="Default concise summarization path",
    )


def _route_blocks(transcript: str, turns: list) -> dict[str, object]:
    transcript_chars = len(transcript)
    turn_count = len(turns)
    if transcript_chars <= 3200 or turn_count <= 18:
        return {"routing_mode": "single_pass", "block_count": 1}
    if transcript_chars <= 9000 and turn_count <= 60:
        return {"routing_mode": "two_block", "block_count": 2}
    return {"routing_mode": "three_block", "block_count": 3}


def _top_keywords(turns: list) -> list[str]:
    counter: Counter[str] = Counter()
    for turn in turns:
        counter.update(
            token
            for token in re.findall(r"[a-z0-9]+", turn.normalized_text.lower())
            if len(token) > 2 and token not in _STOPWORDS
        )
    return [word for word, _ in counter.most_common(3)] or ["discussion"]


def _coarse_blocks(turns: list, block_count: int) -> list[list]:
    if not turns:
        return []
    if block_count <= 1:
        return [turns]

    size = max(1, len(turns) // block_count)
    blocks: list[list] = []
    start = 0
    for block_index in range(block_count):
        if block_index == block_count - 1:
            blocks.append(turns[start:])
            break
        end = min(len(turns), start + size)
        best_end = end
        best_gap = -1.0
        for candidate in range(max(start + 1, end - 3), min(len(turns) - 1, end + 3) + 1):
            gap = turns[candidate].start_time - turns[candidate - 1].end_time
            if gap > best_gap:
                best_gap = gap
                best_end = candidate
        blocks.append(turns[start:best_end])
        start = best_end
    return [block for block in blocks if block]


def _topic_type(block: list) -> str:
    text = " ".join(turn.normalized_text.lower() for turn in block)
    if any(term in text for term in ("bug", "issue", "error", "broken", "incident", "blocker")):
        return "bug_or_issue"
    if any(term in text for term in ("rollout", "launch", "release", "deploy", "timeline", "dependency")):
        return "rollout_or_process"
    if any(term in text for term in ("design", "logic", "proposal", "option", "approach", "recommendation")):
        return "design_or_solution"
    return "general_business"


def _build_topics(turns: list, block_count: int) -> list[TopicSegment]:
    topics: list[TopicSegment] = []
    for index, block in enumerate(_coarse_blocks(turns, block_count), start=1):
        keywords = _top_keywords(block)
        label = " ".join(word.capitalize() for word in keywords[:3]) or "Discussion"
        topics.append(
            TopicSegment(
                topic_id=f"topic_{index:03d}",
                thread_id=f"block_{index:03d}",
                label=label,
                topic_type=_topic_type(block),
                start_time=block[0].start_time,
                end_time=block[-1].end_time,
                turn_ids=[turn.turn_id for turn in block],
                reentry_index=0,
                priority_tags=[],
                confidence=0.9,
            )
        )
    return topics


async def run_pipeline(
    transcript: str,
    template: str,
    llm_call: LLMCallFunc,
    backend_name: str,
    model_name: str,
    progress_callback: Optional[ProgressCallback] = None,
    min_confidence: float = 0.5,
    perspective: Optional[str] = None,
    min_coverage: float = 0.9,
    narrative_strategy: str = "concise_select_render",
    template_prompt: Optional[str] = None,
    llm_context_length: int = 4096,
) -> PipelineResult:
    """Run the concise default transcript summarization pipeline."""
    del perspective, min_coverage, template_prompt, llm_context_length

    timings_ms: dict[str, float] = {}

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "preprocess", "Preparing transcript", _cumulative_progress("preprocess", 0.0))
    turns = preprocess_transcript(transcript)
    normalized_turns, entities = normalize_entities(turns)
    participants = sorted({turn.speaker for turn in normalized_turns if turn.speaker})
    context_profile = _default_context_profile(template)
    timings_ms["preprocessing"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    await _emit_progress(
        progress_callback,
        "preprocess",
        f"Prepared {len(normalized_turns)} transcript turns",
        _cumulative_progress("preprocess", 1.0),
    )

    routing_metadata = _route_blocks(transcript, normalized_turns)

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "segmentation", "Building coarse extraction blocks", _cumulative_progress("segmentation", 0.0))
    topics = _build_topics(normalized_turns, int(routing_metadata["block_count"]))
    timings_ms["segmentation"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    await _emit_progress(
        progress_callback,
        "segmentation",
        f"Prepared {len(topics)} extraction blocks",
        _cumulative_progress("segmentation", 1.0),
    )

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "extraction", "Extracting candidate items", _cumulative_progress("extraction", 0.0))
    turn_lookup = {turn.turn_id: turn for turn in normalized_turns}
    candidate_items = []
    extraction_errors = 0
    for index, topic in enumerate(topics):
        topic_turns = [turn_lookup[turn_id] for turn_id in topic.turn_ids if turn_id in turn_lookup]
        try:
            topic_items = await extract_items_from_topic_segment(topic, topic_turns, llm_call, context_profile)
            candidate_items.extend(topic_items)
        except Exception:
            extraction_errors += 1
            logger.exception("concise extraction failed for topic %s", topic.topic_id)
        await _emit_progress(
            progress_callback,
            "extraction",
            f"Processing block {index + 1}/{max(len(topics), 1)}",
            _cumulative_progress("extraction", (index + 1) / max(len(topics), 1)),
        )
    timings_ms["extraction"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    if topics and extraction_errors == len(topics):
        raise RuntimeError("All concise extraction blocks failed.")

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "validation", "Validating and filtering items", _cumulative_progress("validation", 0.0))
    classified_items = classify_items(candidate_items, participants)
    merged_items = validate_owners(merge_items(classified_items), participants)
    merged_items = assign_ids(merged_items)
    validated_items, validation_warnings = validate_items(merged_items)
    validated_items = [item for item in validated_items if item.confidence >= min_confidence and not item.dropped]
    structured_items = validated_to_structured(validated_items)
    timings_ms["validation"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    await _emit_progress(
        progress_callback,
        "validation",
        f"Validated {len(structured_items.all_items())} items",
        _cumulative_progress("validation", 1.0),
    )

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "ranking", "Ranking and filtering for concise output", _cumulative_progress("ranking", 0.0))
    selection = select_for_render(structured_items, topics)
    timings_ms["ranking"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    await _emit_progress(progress_callback, "ranking", "Selected top items for rendering", _cumulative_progress("ranking", 1.0))

    stage_started = time.perf_counter()
    await _emit_progress(progress_callback, "rendering", "Rendering final notes", _cumulative_progress("rendering", 0.0))
    narrative = render_selection(selection, context_profile)
    timings_ms["rendering"] = round((time.perf_counter() - stage_started) * 1000.0, 2)
    await _emit_progress(progress_callback, "rendering", "Rendering complete", _cumulative_progress("rendering", 1.0))

    routing_metadata.update(
        {
            "topic_count": len(topics),
            "extraction_calls": len(topics),
            "transcript_chars": len(transcript),
            "turn_count": len(normalized_turns),
            "speaker_count": len(participants),
            "default_concise_mode": True,
        }
    )
    logger.info(
        "concise summary routing mode=%s blocks=%s extraction_calls=%s timings_ms=%s",
        routing_metadata["routing_mode"],
        len(topics),
        len(topics),
        timings_ms,
    )

    return PipelineResult(
        narrative=narrative,
        items=structured_items,
        participants=participants,
        template=template,
        context_profile=context_profile,
        topics=topics,
        turns=normalized_turns,
        entities=entities,
        candidate_items=candidate_items,
        validation_warnings=validation_warnings,
        chunk_count=len(topics),
        total_items_extracted=len(candidate_items),
        items_after_dedup=len(merged_items),
        coverage_score=1.0 if structured_items.all_items() else 0.0,
        backend=backend_name,
        model=model_name,
        narrative_context_mode="concise_default_v1",
        narrative_strategy=narrative_strategy,
        narrative_passes=1,
        style_profile=context_profile.render_profile,
        timings_ms=timings_ms,
        routing_metadata=routing_metadata,
    )


def build_markdown_output(
    result: PipelineResult,
    template_label: str,
    recorded_at: str,
    exported_at: str,
    tz_label: str,
    duration_str: str,
    transcript: str,
    perspective: Optional[str] = None,
    include_structured_tables: bool = False,
) -> str:
    """Backward-compatible helper retained for older callers."""
    del template_label, recorded_at, exported_at, tz_label, duration_str, transcript, perspective, include_structured_tables
    return result.narrative
