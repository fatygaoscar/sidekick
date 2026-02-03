"""Audio buffering for transcription."""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np


@dataclass
class AudioChunk:
    """A chunk of audio data with metadata."""

    data: np.ndarray
    timestamp: datetime = field(default_factory=datetime.utcnow)
    sample_rate: int = 16000
    is_speech: bool = True

    @property
    def duration_seconds(self) -> float:
        """Get duration of this chunk in seconds."""
        return len(self.data) / self.sample_rate


class AudioBuffer:
    """Ring buffer for accumulating audio data before transcription."""

    def __init__(
        self,
        sample_rate: int = 16000,
        min_duration: float = 0.5,
        max_duration: float = 30.0,
        silence_threshold: float = 1.0,
    ) -> None:
        """
        Initialize audio buffer.

        Args:
            sample_rate: Audio sample rate in Hz
            min_duration: Minimum buffer duration before processing (seconds)
            max_duration: Maximum buffer duration before forced processing (seconds)
            silence_threshold: Duration of silence before processing (seconds)
        """
        self._sample_rate = sample_rate
        self._min_duration = min_duration
        self._max_duration = max_duration
        self._silence_threshold = silence_threshold

        self._chunks: deque[AudioChunk] = deque()
        self._total_samples = 0
        self._silence_samples = 0
        self._lock = asyncio.Lock()

        self._session_start_time: datetime | None = None
        self._buffer_start_offset: float = 0.0

    @property
    def duration(self) -> float:
        """Get current buffer duration in seconds."""
        return self._total_samples / self._sample_rate

    @property
    def is_ready(self) -> bool:
        """Check if buffer has enough data for processing."""
        if self.duration >= self._max_duration:
            return True
        if self.duration >= self._min_duration:
            silence_duration = self._silence_samples / self._sample_rate
            if silence_duration >= self._silence_threshold:
                return True
        return False

    def set_session_start(self, start_time: datetime) -> None:
        """Set the session start time for offset calculation."""
        self._session_start_time = start_time

    async def add_chunk(self, data: np.ndarray, is_speech: bool = True) -> None:
        """Add an audio chunk to the buffer."""
        async with self._lock:
            chunk = AudioChunk(
                data=data,
                sample_rate=self._sample_rate,
                is_speech=is_speech,
            )
            self._chunks.append(chunk)
            self._total_samples += len(data)

            if is_speech:
                self._silence_samples = 0
            else:
                self._silence_samples += len(data)

    async def get_audio(self) -> tuple[np.ndarray, float, float] | None:
        """
        Get accumulated audio for processing.

        Returns:
            Tuple of (audio_data, start_offset, end_offset) or None if not ready
        """
        async with self._lock:
            if not self._chunks:
                return None

            # Concatenate all chunks
            audio_data = np.concatenate([c.data for c in self._chunks])

            # Calculate offsets relative to session start
            start_offset = self._buffer_start_offset
            end_offset = start_offset + (len(audio_data) / self._sample_rate)

            # Clear buffer and update offset
            self._chunks.clear()
            self._total_samples = 0
            self._silence_samples = 0
            self._buffer_start_offset = end_offset

            return audio_data, start_offset, end_offset

    async def clear(self) -> None:
        """Clear the buffer."""
        async with self._lock:
            self._chunks.clear()
            self._total_samples = 0
            self._silence_samples = 0

    def reset(self) -> None:
        """Reset buffer state for new session."""
        self._chunks.clear()
        self._total_samples = 0
        self._silence_samples = 0
        self._buffer_start_offset = 0.0
        self._session_start_time = None
