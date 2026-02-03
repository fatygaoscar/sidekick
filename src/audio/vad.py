"""Voice Activity Detection using webrtcvad."""

import numpy as np
import webrtcvad


class VoiceActivityDetector:
    """Voice Activity Detection using WebRTC VAD."""

    VALID_FRAME_DURATIONS = [10, 20, 30]  # ms
    VALID_SAMPLE_RATES = [8000, 16000, 32000, 48000]

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 30,
        aggressiveness: int = 2,
    ) -> None:
        """
        Initialize VAD.

        Args:
            sample_rate: Audio sample rate (8000, 16000, 32000, or 48000)
            frame_duration_ms: Frame duration (10, 20, or 30 ms)
            aggressiveness: VAD aggressiveness (0-3, higher = more aggressive filtering)
        """
        if sample_rate not in self.VALID_SAMPLE_RATES:
            raise ValueError(f"Sample rate must be one of {self.VALID_SAMPLE_RATES}")
        if frame_duration_ms not in self.VALID_FRAME_DURATIONS:
            raise ValueError(f"Frame duration must be one of {self.VALID_FRAME_DURATIONS}")
        if not 0 <= aggressiveness <= 3:
            raise ValueError("Aggressiveness must be between 0 and 3")

        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._aggressiveness = aggressiveness

        self._vad = webrtcvad.Vad(aggressiveness)
        self._frame_size = int(sample_rate * frame_duration_ms / 1000)

    @property
    def frame_size(self) -> int:
        """Get the frame size in samples."""
        return self._frame_size

    def is_speech(self, audio_frame: np.ndarray) -> bool:
        """
        Check if an audio frame contains speech.

        Args:
            audio_frame: Audio data as numpy array (must be correct frame size)

        Returns:
            True if speech is detected
        """
        if len(audio_frame) != self._frame_size:
            raise ValueError(f"Frame must be {self._frame_size} samples, got {len(audio_frame)}")

        # Convert to 16-bit PCM bytes
        audio_bytes = (audio_frame * 32767).astype(np.int16).tobytes()

        return self._vad.is_speech(audio_bytes, self._sample_rate)

    def process_chunk(self, audio_chunk: np.ndarray) -> list[tuple[np.ndarray, bool]]:
        """
        Process an audio chunk and return frames with speech detection.

        Args:
            audio_chunk: Audio data as numpy array

        Returns:
            List of (frame, is_speech) tuples
        """
        results = []

        # Process complete frames
        num_frames = len(audio_chunk) // self._frame_size
        for i in range(num_frames):
            start = i * self._frame_size
            end = start + self._frame_size
            frame = audio_chunk[start:end]
            is_speech = self.is_speech(frame)
            results.append((frame, is_speech))

        return results

    def get_speech_ratio(self, audio_chunk: np.ndarray) -> float:
        """
        Get the ratio of speech frames in an audio chunk.

        Args:
            audio_chunk: Audio data as numpy array

        Returns:
            Ratio of speech frames (0.0 to 1.0)
        """
        results = self.process_chunk(audio_chunk)
        if not results:
            return 0.0

        speech_frames = sum(1 for _, is_speech in results if is_speech)
        return speech_frames / len(results)


class SpeechSegmenter:
    """Segments continuous audio into speech/non-speech regions."""

    def __init__(
        self,
        vad: VoiceActivityDetector,
        speech_pad_ms: int = 300,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 500,
    ) -> None:
        """
        Initialize segmenter.

        Args:
            vad: Voice activity detector instance
            speech_pad_ms: Padding around speech segments
            min_speech_duration_ms: Minimum speech segment duration
            min_silence_duration_ms: Minimum silence to trigger segment end
        """
        self._vad = vad
        self._speech_pad_ms = speech_pad_ms
        self._min_speech_duration_ms = min_speech_duration_ms
        self._min_silence_duration_ms = min_silence_duration_ms

        self._is_speaking = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._frame_duration_ms = 30  # Assuming 30ms frames

    def process_frame(self, frame: np.ndarray) -> str | None:
        """
        Process a frame and return state change if any.

        Args:
            frame: Audio frame

        Returns:
            "speech_start", "speech_end", or None
        """
        is_speech = self._vad.is_speech(frame)

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0

            if not self._is_speaking:
                speech_duration = self._speech_frames * self._frame_duration_ms
                if speech_duration >= self._min_speech_duration_ms:
                    self._is_speaking = True
                    return "speech_start"
        else:
            self._silence_frames += 1

            if self._is_speaking:
                silence_duration = self._silence_frames * self._frame_duration_ms
                if silence_duration >= self._min_silence_duration_ms:
                    self._is_speaking = False
                    self._speech_frames = 0
                    return "speech_end"

        return None

    def reset(self) -> None:
        """Reset segmenter state."""
        self._is_speaking = False
        self._speech_frames = 0
        self._silence_frames = 0
