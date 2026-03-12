"""Shared in-process audio decode helpers for transcription pipelines."""

from pathlib import Path

import av
import numpy as np

TARGET_SAMPLE_RATE = 16000


def decode_audio_to_mono_float32(
    audio_path: str | Path,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> np.ndarray:
    """Decode audio into a 1D mono float32 waveform using PyAV."""
    resampler = av.audio.resampler.AudioResampler(
        format="fltp",
        layout="mono",
        rate=sample_rate,
    )
    chunks: list[np.ndarray] = []

    with av.open(str(audio_path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunk = out.to_ndarray()
                if chunk.size:
                    chunks.append(np.asarray(chunk, dtype=np.float32))

        for out in resampler.resample(None):
            chunk = out.to_ndarray()
            if chunk.size:
                chunks.append(np.asarray(chunk, dtype=np.float32))

    if not chunks:
        raise RuntimeError(f"No audio decoded from {audio_path}")

    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32, copy=False)


def decode_audio_clip_to_mono_float32(
    audio_path: str | Path,
    start_time: float,
    end_time: float,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> np.ndarray:
    """Decode only the requested clip window into a 1D mono float32 waveform."""
    clip_start = max(0.0, float(start_time))
    clip_end = max(clip_start, float(end_time))
    if clip_end <= clip_start:
        raise RuntimeError("Audio clip window is empty.")

    resampler = av.audio.resampler.AudioResampler(
        format="fltp",
        layout="mono",
        rate=sample_rate,
    )
    chunks: list[np.ndarray] = []
    first_frame_start: float | None = None

    with av.open(str(audio_path)) as container:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise RuntimeError(f"No audio stream found in {audio_path}")

        if clip_start > 0.0:
            try:
                if stream.time_base is not None:
                    seek_target = max(0, int(clip_start / float(stream.time_base)))
                    container.seek(seek_target, stream=stream, any_frame=False, backward=True)
            except Exception:
                # Fall back to decoding from the start when seeking is unavailable.
                pass

        for frame in container.decode(audio=stream.index):
            frame_start = float(frame.time or 0.0)
            frame_duration = float(frame.samples or 0) / float(frame.sample_rate or sample_rate)
            frame_end = frame_start + frame_duration
            if frame_end <= clip_start:
                continue
            if first_frame_start is None:
                first_frame_start = frame_start
            if frame_start >= clip_end:
                break
            for out in resampler.resample(frame):
                chunk = out.to_ndarray()
                if chunk.size:
                    chunks.append(np.asarray(chunk, dtype=np.float32))

        for out in resampler.resample(None):
            chunk = out.to_ndarray()
            if chunk.size:
                chunks.append(np.asarray(chunk, dtype=np.float32))

    if not chunks or first_frame_start is None:
        raise RuntimeError(f"No audio decoded from clip {audio_path}")

    audio = np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32, copy=False)
    offset_start = max(0, int(round((clip_start - first_frame_start) * sample_rate)))
    target_length = max(1, int(round((clip_end - clip_start) * sample_rate)))
    offset_end = min(audio.shape[-1], offset_start + target_length)
    clipped = audio[offset_start:offset_end]
    if clipped.size == 0:
        raise RuntimeError(f"No audio decoded from clip {audio_path}")
    return clipped


def decode_audio_to_waveform_dict(
    audio_path: str | Path,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> dict:
    """Decode audio into the waveform structure expected by pyannote."""
    import torch

    audio = decode_audio_to_mono_float32(audio_path, sample_rate=sample_rate)
    return {
        "waveform": torch.from_numpy(audio[None, :]),
        "sample_rate": sample_rate,
    }


def media_duration_seconds(audio_path: str | Path) -> float | None:
    """Read container duration in seconds without decoding the full waveform."""
    with av.open(str(audio_path)) as container:
        if container.duration is not None:
            return float(container.duration) / float(av.time_base)
        audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
        if audio_stream is not None and audio_stream.duration is not None and audio_stream.time_base is not None:
            return float(audio_stream.duration * audio_stream.time_base)
    return None


def duration_seconds_from_audio(audio: np.ndarray, sample_rate: int = TARGET_SAMPLE_RATE) -> float:
    """Compute duration from a mono waveform."""
    if audio.size == 0:
        return 0.0
    return float(audio.shape[-1]) / float(sample_rate)
