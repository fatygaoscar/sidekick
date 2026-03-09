"""Cached speaker clip extraction helpers."""

from __future__ import annotations

import logging
import re
import shutil
import wave
from pathlib import Path

from config.settings import get_settings


logger = logging.getLogger(__name__)

_CLIP_SAMPLE_RATE = 16000
_CLIP_CHANNELS = 1
_CLIP_SAMPLE_WIDTH_BYTES = 2


def _sanitize_speaker_key(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return normalized.strip("._-") or "speaker"


def _clip_ms(value: float) -> int:
    return max(0, int(round(float(value) * 1000.0)))


def get_speaker_clip_dir(session_id: str, transcript_version_id: str | None = None) -> Path:
    version_key = transcript_version_id or "latest"
    path = get_settings().data_dir / "audio" / "clips" / session_id / version_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_speaker_clip_path(
    session_id: str,
    transcript_version_id: str | None,
    speaker_key: str,
    start_time: float,
    end_time: float,
) -> Path:
    clip_dir = get_speaker_clip_dir(session_id, transcript_version_id)
    safe_key = _sanitize_speaker_key(speaker_key)
    start_ms = _clip_ms(start_time)
    end_ms = max(start_ms + 1, _clip_ms(end_time))
    return clip_dir / f"{safe_key}-{start_ms}-{end_ms}.wav"


def cleanup_speaker_clip_cache(session_id: str) -> None:
    clip_dir = get_settings().data_dir / "audio" / "clips" / session_id
    if clip_dir.exists():
        shutil.rmtree(clip_dir)


def _decode_clip_samples(audio_path: Path, start_time: float, end_time: float):
    import av
    import numpy as np

    clip_start = max(0.0, float(start_time))
    clip_end = max(clip_start + 0.05, float(end_time))
    seek_time = max(0.0, clip_start - 0.75)

    with av.open(str(audio_path)) as container:
        stream = next((candidate for candidate in container.streams if candidate.type == "audio"), None)
        if stream is None:
            raise RuntimeError(f"No audio stream found in {audio_path}")

        resampler = av.audio.resampler.AudioResampler(
            format="s16",
            layout="mono",
            rate=_CLIP_SAMPLE_RATE,
        )

        if stream.time_base:
            try:
                container.seek(
                    int(seek_time / float(stream.time_base)),
                    stream=stream,
                    backward=True,
                    any_frame=False,
                )
            except Exception:
                logger.warning(
                    "speaker_clip: seek failed, falling back to sequential decode",
                    extra={"audio_path": str(audio_path), "start_time": clip_start},
                )

        clip_chunks = []
        next_frame_start = None
        reached_end = False

        for frame in container.decode(stream):
            frame_start = (
                float(frame.time)
                if frame.time is not None
                else (next_frame_start if next_frame_start is not None else 0.0)
            )

            for out in resampler.resample(frame):
                samples = np.asarray(out.to_ndarray())
                if samples.ndim == 2:
                    samples = samples[0]
                samples = np.ascontiguousarray(samples, dtype=np.int16)
                if samples.size == 0:
                    continue

                if out.pts is not None and out.time_base is not None:
                    out_start = float(out.pts * out.time_base)
                elif getattr(out, "time", None) is not None:
                    out_start = float(out.time)
                else:
                    out_start = frame_start

                duration = samples.size / float(out.sample_rate or _CLIP_SAMPLE_RATE)
                out_end = out_start + duration
                next_frame_start = out_end

                if out_end <= clip_start:
                    continue
                if out_start >= clip_end:
                    reached_end = True
                    break

                start_index = max(0, int(round((clip_start - out_start) * _CLIP_SAMPLE_RATE)))
                end_index = min(samples.size, int(round((clip_end - out_start) * _CLIP_SAMPLE_RATE)))
                if end_index > start_index:
                    clip_chunks.append(samples[start_index:end_index])
                if out_end >= clip_end:
                    reached_end = True
                    break

            if reached_end:
                break

        if not clip_chunks:
            raise RuntimeError(
                f"No audio samples decoded for clip {clip_start:.2f}-{clip_end:.2f} from {audio_path}"
            )

        return np.concatenate(clip_chunks)


def ensure_speaker_clip(
    audio_path: Path,
    session_id: str,
    transcript_version_id: str | None,
    speaker_key: str,
    start_time: float,
    end_time: float,
) -> Path:
    clip_path = get_speaker_clip_path(
        session_id,
        transcript_version_id,
        speaker_key,
        start_time,
        end_time,
    )
    if clip_path.exists() and clip_path.stat().st_size > 0:
        logger.info(
            "speaker_clip: cache hit | session=%s speaker=%s path=%s",
            session_id,
            speaker_key,
            clip_path,
        )
        return clip_path

    samples = _decode_clip_samples(audio_path, start_time, end_time)
    temp_path = clip_path.with_suffix(".tmp")
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(temp_path), "wb") as wav_file:
        wav_file.setnchannels(_CLIP_CHANNELS)
        wav_file.setsampwidth(_CLIP_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(_CLIP_SAMPLE_RATE)
        wav_file.writeframes(samples.tobytes())
    temp_path.replace(clip_path)

    logger.info(
        "speaker_clip: generated | session=%s speaker=%s start=%.2f end=%.2f path=%s",
        session_id,
        speaker_key,
        start_time,
        end_time,
        clip_path,
    )
    return clip_path
