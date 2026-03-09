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


def duration_seconds_from_audio(audio: np.ndarray, sample_rate: int = TARGET_SAMPLE_RATE) -> float:
    """Compute duration from a mono waveform."""
    if audio.size == 0:
        return 0.0
    return float(audio.shape[-1]) / float(sample_rate)
