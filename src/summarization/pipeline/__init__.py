"""Multi-stage meeting processing pipeline.

This package provides a pipeline for extracting structured information from
meeting transcripts in multiple passes:

1. Chunking: Split transcript into manageable time-based chunks
2. Extraction: Extract items (actions, decisions, risks, questions) per chunk
3. Merging: Deduplicate items across chunks
4. Structuring: Validate and assign IDs to items
5. Narration: Generate narrative summary referencing structured items
"""

from .types import ExtractedItem, PipelineResult, TranscriptChunk
from .chunker import chunk_transcript
from .extraction import extract_items_from_chunk
from .merger import merge_items
from .structurer import structure_items
from .narrator import generate_narrative
from .pipeline import run_pipeline

__all__ = [
    "ExtractedItem",
    "PipelineResult",
    "TranscriptChunk",
    "chunk_transcript",
    "extract_items_from_chunk",
    "merge_items",
    "structure_items",
    "generate_narrative",
    "run_pipeline",
]
