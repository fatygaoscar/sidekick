"""Audio capture from microphone (server-side, optional)."""

import asyncio
from typing import AsyncIterator, Callable

import numpy as np
import sounddevice as sd

from config.settings import get_settings

from .buffer import AudioBuffer
from .vad import VoiceActivityDetector


class AudioCapture:
    """
    Server-side audio capture from microphone.

    Note: In the primary architecture, audio comes from the browser via WebSocket.
    This class is for server-side capture scenarios (e.g., CLI mode, testing).
    """

    def __init__(
        self,
        sample_rate: int | None = None,
        channels: int | None = None,
        chunk_duration_ms: int = 30,
    ) -> None:
        """
        Initialize audio capture.

        Args:
            sample_rate: Audio sample rate (default from settings)
            channels: Number of channels (default from settings)
            chunk_duration_ms: Duration of each chunk in milliseconds
        """
        settings = get_settings()
        self._sample_rate = sample_rate or settings.audio_sample_rate
        self._channels = channels or settings.audio_channels
        self._chunk_duration_ms = chunk_duration_ms
        self._chunk_size = int(self._sample_rate * chunk_duration_ms / 1000)

        self._is_running = False
        self._stream: sd.InputStream | None = None
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

    @property
    def sample_rate(self) -> int:
        """Get the sample rate."""
        return self._sample_rate

    @property
    def is_running(self) -> bool:
        """Check if capture is running."""
        return self._is_running

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """Callback for audio stream."""
        if status:
            print(f"Audio callback status: {status}")

        # Convert to mono float32
        audio = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        audio = audio.astype(np.float32)

        # Put in queue (non-blocking)
        try:
            self._queue.put_nowait(audio.copy())
        except asyncio.QueueFull:
            pass  # Drop frame if queue is full

    async def start(self) -> None:
        """Start audio capture."""
        if self._is_running:
            return

        self._stream = sd.InputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            dtype=np.float32,
            blocksize=self._chunk_size,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._is_running = True

    async def stop(self) -> None:
        """Stop audio capture."""
        if not self._is_running:
            return

        self._is_running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        # Clear queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def read_chunk(self, timeout: float = 1.0) -> np.ndarray | None:
        """Read a single audio chunk."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def stream(self) -> AsyncIterator[np.ndarray]:
        """Stream audio chunks."""
        while self._is_running:
            chunk = await self.read_chunk()
            if chunk is not None:
                yield chunk


class AudioProcessor:
    """Processes audio chunks with VAD and buffering."""

    def __init__(
        self,
        sample_rate: int = 16000,
        vad_aggressiveness: int = 2,
        on_ready: Callable[[np.ndarray, float, float], None] | None = None,
    ) -> None:
        """
        Initialize audio processor.

        Args:
            sample_rate: Audio sample rate
            vad_aggressiveness: VAD aggressiveness level (0-3)
            on_ready: Callback when buffer is ready for transcription
        """
        self._sample_rate = sample_rate
        self._vad = VoiceActivityDetector(
            sample_rate=sample_rate,
            aggressiveness=vad_aggressiveness,
        )
        self._buffer = AudioBuffer(sample_rate=sample_rate)
        self._on_ready = on_ready

    @property
    def buffer(self) -> AudioBuffer:
        """Get the audio buffer."""
        return self._buffer

    async def process_chunk(self, audio_chunk: np.ndarray) -> None:
        """
        Process an audio chunk through VAD and buffer.

        Args:
            audio_chunk: Audio data as numpy array
        """
        # Get speech ratio for the chunk
        speech_ratio = self._vad.get_speech_ratio(audio_chunk)
        is_speech = speech_ratio > 0.3  # At least 30% speech

        # Add to buffer
        await self._buffer.add_chunk(audio_chunk, is_speech=is_speech)

        # Check if buffer is ready
        if self._buffer.is_ready:
            result = await self._buffer.get_audio()
            if result and self._on_ready:
                audio_data, start_offset, end_offset = result
                self._on_ready(audio_data, start_offset, end_offset)

    def reset(self) -> None:
        """Reset processor state."""
        self._buffer.reset()
