"""Transcript chunking for pipeline processing.

Splits transcripts into time-based chunks (8-12 minutes) for parallel processing.
Uses transcript timestamps to determine chunk boundaries.
"""

import re
from typing import Optional

from .types import TranscriptChunk


# Target chunk duration in seconds (8-12 minutes)
MIN_CHUNK_DURATION = 8 * 60  # 8 minutes
TARGET_CHUNK_DURATION = 10 * 60  # 10 minutes
MAX_CHUNK_DURATION = 12 * 60  # 12 minutes

# Timestamp pattern: [MM:SS] or [HH:MM:SS]
TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")


def _parse_timestamp(ts: str) -> float:
    """Parse timestamp string to seconds.

    Args:
        ts: Timestamp in format [MM:SS] or [HH:MM:SS]

    Returns:
        Time in seconds
    """
    match = TIMESTAMP_PATTERN.match(ts)
    if not match:
        return 0.0

    groups = match.groups()
    if groups[2] is not None:
        # HH:MM:SS format
        hours = int(groups[0])
        minutes = int(groups[1])
        seconds = int(groups[2])
        return hours * 3600 + minutes * 60 + seconds
    else:
        # MM:SS format
        minutes = int(groups[0])
        seconds = int(groups[1])
        return minutes * 60 + seconds


def _format_timestamp(seconds: float) -> str:
    """Format seconds as timestamp string.

    Args:
        seconds: Time in seconds

    Returns:
        Formatted string [MM:SS] or [HH:MM:SS]
    """
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    if hours > 0:
        return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
    else:
        return f"[{minutes:02d}:{secs:02d}]"


def _extract_lines_with_timestamps(transcript: str) -> list[tuple[float, str]]:
    """Extract lines with their timestamps from transcript.

    Args:
        transcript: Full transcript text with timestamps

    Returns:
        List of (timestamp_seconds, line_text) tuples
    """
    lines = []
    current_time = 0.0

    for line in transcript.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Try to extract timestamp from line start
        match = TIMESTAMP_PATTERN.match(line)
        if match:
            current_time = _parse_timestamp(match.group(0))

        lines.append((current_time, line))

    return lines


def chunk_transcript(
    transcript: str,
    min_duration: float = MIN_CHUNK_DURATION,
    target_duration: float = TARGET_CHUNK_DURATION,
    max_duration: float = MAX_CHUNK_DURATION,
) -> list[TranscriptChunk]:
    """Split transcript into time-based chunks.

    Chunks are split at natural boundaries (between lines) aiming for
    the target duration. Very short transcripts (<min_duration) are
    returned as a single chunk.

    Args:
        transcript: Full transcript text with timestamps
        min_duration: Minimum chunk duration in seconds
        target_duration: Target chunk duration in seconds
        max_duration: Maximum chunk duration in seconds

    Returns:
        List of TranscriptChunk objects
    """
    lines = _extract_lines_with_timestamps(transcript)

    if not lines:
        return []

    # Get total duration
    total_duration = lines[-1][0] if lines else 0

    # If transcript is shorter than min_duration, return as single chunk
    if total_duration <= min_duration:
        return [
            TranscriptChunk(
                index=0,
                text=transcript.strip(),
                start_time=0.0,
                end_time=total_duration,
                start_timestamp=_format_timestamp(0),
                end_timestamp=_format_timestamp(total_duration),
            )
        ]

    chunks: list[TranscriptChunk] = []
    chunk_lines: list[str] = []
    chunk_start_time = 0.0
    chunk_index = 0

    for i, (line_time, line_text) in enumerate(lines):
        chunk_duration = line_time - chunk_start_time

        # Check if we should start a new chunk
        should_split = False

        if chunk_duration >= target_duration:
            # We've hit target duration, split here
            should_split = True
        elif chunk_duration >= max_duration:
            # We've exceeded max, must split
            should_split = True

        if should_split and chunk_lines:
            # Create chunk from accumulated lines
            chunk_text = "\n".join(chunk_lines)
            chunk_end_time = lines[i - 1][0] if i > 0 else line_time

            chunks.append(
                TranscriptChunk(
                    index=chunk_index,
                    text=chunk_text,
                    start_time=chunk_start_time,
                    end_time=chunk_end_time,
                    start_timestamp=_format_timestamp(chunk_start_time),
                    end_timestamp=_format_timestamp(chunk_end_time),
                )
            )

            # Reset for next chunk
            chunk_index += 1
            chunk_lines = []
            chunk_start_time = line_time

        chunk_lines.append(line_text)

    # Don't forget the last chunk
    if chunk_lines:
        chunk_text = "\n".join(chunk_lines)
        chunk_end_time = lines[-1][0] if lines else chunk_start_time

        chunks.append(
            TranscriptChunk(
                index=chunk_index,
                text=chunk_text,
                start_time=chunk_start_time,
                end_time=chunk_end_time,
                start_timestamp=_format_timestamp(chunk_start_time),
                end_timestamp=_format_timestamp(chunk_end_time),
            )
        )

    return chunks


def estimate_chunk_count(transcript: str) -> int:
    """Estimate how many chunks a transcript will be split into.

    Useful for progress tracking before actual chunking.

    Args:
        transcript: Full transcript text

    Returns:
        Estimated number of chunks
    """
    lines = _extract_lines_with_timestamps(transcript)
    if not lines:
        return 0

    total_duration = lines[-1][0]
    if total_duration <= MIN_CHUNK_DURATION:
        return 1

    # Estimate based on target duration
    return max(1, int(total_duration / TARGET_CHUNK_DURATION) + 1)
