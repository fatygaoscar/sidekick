"""Pipeline orchestrator for multi-stage meeting processing.

Coordinates chunking, extraction, merging, structuring, and narrative
generation with progress tracking.
"""

import asyncio
from typing import Callable, Awaitable, Optional

from .types import PipelineResult, StructuredItems, TranscriptChunk
from .chunker import chunk_transcript, estimate_chunk_count
from .extraction import extract_items_from_chunk, extract_participants
from .merger import merge_items, validate_owners
from .structurer import structure_items, items_to_markdown_tables
from .narrator import generate_narrative


# Type aliases
LLMCallFunc = Callable[[str, str], Awaitable[str]]
ProgressCallback = Callable[[str, str, float], Awaitable[None] | None]


# Progress weights by stage (must sum to 1.0)
PROGRESS_WEIGHTS = {
    "chunking": 0.02,
    "extraction": 0.38,
    "merging": 0.03,
    "structuring": 0.12,
    "narrative": 0.35,
    "coverage": 0.05,
    "finalizing": 0.05,
}


async def _emit_progress(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    progress: float,
) -> None:
    """Emit progress update if callback provided."""
    if callback:
        result = callback(stage, message, progress)
        if asyncio.iscoroutine(result):
            await result


def _calculate_cumulative_progress(stage: str, stage_progress: float) -> float:
    """Calculate cumulative progress based on stage and stage progress.

    Args:
        stage: Current pipeline stage
        stage_progress: Progress within current stage (0-1)

    Returns:
        Overall cumulative progress (0-1)
    """
    stages = list(PROGRESS_WEIGHTS.keys())
    cumulative = 0.0

    for s in stages:
        if s == stage:
            return cumulative + (PROGRESS_WEIGHTS[s] * stage_progress)
        cumulative += PROGRESS_WEIGHTS[s]

    return cumulative


async def run_pipeline(
    transcript: str,
    template: str,
    llm_call: LLMCallFunc,
    backend_name: str,
    model_name: str,
    progress_callback: Optional[ProgressCallback] = None,
    min_confidence: float = 0.5,
    min_coverage: float = 0.9,
) -> PipelineResult:
    """Run the full multi-stage pipeline.

    Args:
        transcript: Full transcript text with timestamps
        template: Template type (meeting, working_session, etc.)
        llm_call: Async function to call the LLM
        backend_name: Name of the backend for metadata
        model_name: Name of the model for metadata
        progress_callback: Optional callback for progress updates
        min_confidence: Minimum confidence threshold for items
        min_coverage: Minimum coverage score before patching

    Returns:
        PipelineResult with narrative, structured items, and metadata
    """
    # Stage 1: Chunking
    await _emit_progress(
        progress_callback,
        "chunking",
        "Analyzing transcript structure",
        _calculate_cumulative_progress("chunking", 0.0),
    )

    chunks = chunk_transcript(transcript)
    chunk_count = len(chunks)

    await _emit_progress(
        progress_callback,
        "chunking",
        f"Split into {chunk_count} chunks",
        _calculate_cumulative_progress("chunking", 1.0),
    )

    # Stage 2: Extraction (per chunk)
    await _emit_progress(
        progress_callback,
        "extraction",
        "Extracting items from transcript",
        _calculate_cumulative_progress("extraction", 0.0),
    )

    all_items = []
    extraction_errors = 0

    for i, chunk in enumerate(chunks):
        chunk_progress = i / chunk_count
        await _emit_progress(
            progress_callback,
            "extraction",
            f"Processing chunk {i + 1}/{chunk_count}",
            _calculate_cumulative_progress("extraction", chunk_progress),
        )

        try:
            chunk_items = await extract_items_from_chunk(chunk, llm_call)
            all_items.extend(chunk_items)
        except Exception:
            extraction_errors += 1
            # Continue with other chunks

    total_extracted = len(all_items)

    await _emit_progress(
        progress_callback,
        "extraction",
        f"Extracted {total_extracted} items",
        _calculate_cumulative_progress("extraction", 1.0),
    )

    # Extract participants for owner validation
    participants = extract_participants(transcript)

    # Stage 3: Merging
    await _emit_progress(
        progress_callback,
        "merging",
        "Deduplicating items",
        _calculate_cumulative_progress("merging", 0.0),
    )

    merged = merge_items(all_items)
    merged = validate_owners(merged, participants)
    items_after_dedup = len(merged)

    await _emit_progress(
        progress_callback,
        "merging",
        f"Merged to {items_after_dedup} unique items",
        _calculate_cumulative_progress("merging", 1.0),
    )

    # Stage 4: Structuring
    await _emit_progress(
        progress_callback,
        "structuring",
        "Validating and organizing items",
        _calculate_cumulative_progress("structuring", 0.0),
    )

    structured, warnings = structure_items(merged, min_confidence=min_confidence)

    await _emit_progress(
        progress_callback,
        "structuring",
        f"Structured {len(structured.all_items())} items",
        _calculate_cumulative_progress("structuring", 1.0),
    )

    # Stage 5: Narrative generation
    await _emit_progress(
        progress_callback,
        "narrative",
        "Generating summary narrative",
        _calculate_cumulative_progress("narrative", 0.0),
    )

    narrative, coverage = await generate_narrative(
        items=structured,
        template=template,
        transcript=transcript,
        llm_call=llm_call,
        min_coverage=min_coverage,
    )

    await _emit_progress(
        progress_callback,
        "narrative",
        "Narrative complete",
        _calculate_cumulative_progress("narrative", 1.0),
    )

    # Stage 6: Coverage check / patch (if needed)
    await _emit_progress(
        progress_callback,
        "coverage",
        f"Coverage: {coverage:.0%}",
        _calculate_cumulative_progress("coverage", 1.0),
    )

    # Stage 7: Finalizing
    await _emit_progress(
        progress_callback,
        "finalizing",
        "Finalizing results",
        _calculate_cumulative_progress("finalizing", 0.5),
    )

    result = PipelineResult(
        narrative=narrative,
        items=structured,
        participants=participants,
        template=template,
        chunk_count=chunk_count,
        total_items_extracted=total_extracted,
        items_after_dedup=items_after_dedup,
        coverage_score=coverage,
        backend=backend_name,
        model=model_name,
    )

    await _emit_progress(
        progress_callback,
        "finalizing",
        "Pipeline complete",
        1.0,
    )

    return result


def build_markdown_output(
    result: PipelineResult,
    template_label: str,
    recorded_at: str,
    exported_at: str,
    tz_label: str,
    duration_str: str,
    transcript: str,
) -> str:
    """Build final markdown output from pipeline result.

    Args:
        result: Pipeline result
        template_label: Display name for template
        recorded_at: Formatted recording date
        exported_at: Formatted export date
        tz_label: Timezone label
        duration_str: Formatted duration
        transcript: Full transcript for inclusion

    Returns:
        Complete markdown document
    """
    # Build the tables section
    tables_md = items_to_markdown_tables(result.items)

    markdown_content = f"""**Template**: {template_label}
**Recorded**: {recorded_at} ({tz_label})
**Exported**: {exported_at} ({tz_label})
**Duration**: {duration_str}

---

{result.narrative}

---

## Extracted Items

{tables_md}

---

<details>
<summary>Full Transcript</summary>

{transcript}

</details>
"""

    return markdown_content
