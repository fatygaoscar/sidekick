"""Transcript preprocessing for adaptive summarization."""

from __future__ import annotations

import re

from .types import TranscriptTurn


_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?:(?P<speaker>[^:]+):\s*)?(?P<text>.*)$"
)


def _timestamp_to_seconds(timestamp: str) -> float:
    parts = [int(part) for part in timestamp.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return float(hours * 3600 + minutes * 60 + seconds)
    minutes, seconds = parts
    return float(minutes * 60 + seconds)


def preprocess_transcript(transcript: str) -> list[TranscriptTurn]:
    """Convert transcript text into normalized turns without losing evidence."""
    turns: list[TranscriptTurn] = []
    lines = [line.rstrip() for line in transcript.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        match = _LINE_RE.match(line)
        if match:
            ts = match.group("ts")
            speaker = (match.group("speaker") or "").strip() or None
            text = (match.group("text") or "").strip()
            start_time = _timestamp_to_seconds(ts)
        else:
            ts = "00:00"
            speaker = None
            text = line.strip()
            start_time = 0.0

        next_start = start_time
        if index + 1 < len(lines):
            next_match = _LINE_RE.match(lines[index + 1])
            if next_match:
                next_start = _timestamp_to_seconds(next_match.group("ts"))
        end_time = max(start_time, next_start)

        normalized_text = re.sub(r"\s+", " ", text).strip()
        turns.append(
            TranscriptTurn(
                turn_id=f"turn_{index + 1:04d}",
                speaker_raw=speaker,
                speaker=speaker,
                speaker_cluster=speaker if speaker and speaker.startswith("SPEAKER_") else None,
                start_time=start_time,
                end_time=end_time,
                raw_text=text,
                normalized_text=normalized_text,
            )
        )

    for index, turn in enumerate(turns[:-1]):
        turn.end_time = max(turn.start_time, turns[index + 1].start_time)
    if turns:
        turns[-1].end_time = max(turns[-1].start_time, turns[-1].end_time)

    return turns
