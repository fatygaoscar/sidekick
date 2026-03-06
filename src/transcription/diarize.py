"""Speaker diarization via pyannote.audio 3.1."""
import logging

logger = logging.getLogger(__name__)
_pipeline = None

_TARGET_SR = 16000


def _load_pipeline(hf_token: str):
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    from pyannote.audio import Pipeline
    import torch
    _pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    if torch.cuda.is_available():
        _pipeline = _pipeline.to(torch.device("cuda"))
    logger.info("pyannote diarization pipeline loaded")
    return _pipeline


def _get_pipeline_params(min_speakers: int | None, max_speakers: int | None) -> dict:
    """Build kwargs for pyannote pipeline based on speaker constraints."""
    params = {}
    if min_speakers is not None and max_speakers is not None and min_speakers == max_speakers:
        params["num_speakers"] = min_speakers
    else:
        if min_speakers is not None:
            params["min_speakers"] = min_speakers
        if max_speakers is not None:
            params["max_speakers"] = max_speakers
    return params


def _load_waveform(audio_path: str, duration_limit: float | None = None) -> dict:
    """Load audio via PyAV (bundled FFmpeg) into a pyannote-compatible waveform dict.

    Avoids the system-FFmpeg dependency that torchcodec requires.
    """
    import av
    import numpy as np
    import torch

    resampler = av.audio.resampler.AudioResampler(
        format="fltp", layout="mono", rate=_TARGET_SR
    )
    chunks = []
    total_samples = 0
    
    with av.open(audio_path) as container:
        for frame in container.decode(audio=0):
            # If duration limit is reached, stop decoding
            if duration_limit is not None and frame.time > duration_limit:
                break
                
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
        
        # Flush resampler unless we hit the limit
        if duration_limit is None or frame.time <= duration_limit:
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray())

    if not chunks:
        raise RuntimeError(f"No audio decoded from {audio_path}")

    waveform = torch.from_numpy(np.concatenate(chunks, axis=1))
    return {"waveform": waveform, "sample_rate": _TARGET_SR}


def diarize(
    audio_path: str,
    hf_token: str,
    duration_limit: float | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list[tuple[float, float, str]]:
    """Return (start_sec, end_sec, speaker_label) for every speaker turn.

    Args:
        audio_path: Path to audio file
        hf_token: HuggingFace token
        duration_limit: Optional limit in seconds to stop processing (saves time on long silences)
        min_speakers: Minimum number of speakers to detect
        max_speakers: Maximum number of speakers to detect
    """
    pipeline = _load_pipeline(hf_token)
    audio = _load_waveform(audio_path, duration_limit=duration_limit)
    
    # Build pipeline kwargs based on speaker constraints
    pipeline_kwargs = _get_pipeline_params(min_speakers, max_speakers)
    
    # Log speaker constraints for debugging
    if pipeline_kwargs:
        logger.info("diarize: applying speaker constraints: %s", pipeline_kwargs)
    
    result = pipeline(audio, **pipeline_kwargs)
    spans = [(seg.start, seg.end, spk) for seg, _, spk in result.exclusive_speaker_diarization.itertracks(yield_label=True)]

    # Log diarization results for debugging speaker mapping issues
    from collections import Counter
    counts = Counter(spk for _, _, spk in spans)
    logger.info(
        "diarize: %d spans, %d unique speakers: %s",
        len(spans),
        len(counts),
        ", ".join(f"{spk}={n}" for spk, n in sorted(counts.items())),
    )
    return spans


def assign_speaker(
    start: float,
    end: float,
    spans: list[tuple[float, float, str]],
    min_overlap: float = 0.1,
) -> str | None:
    """Return speaker label with maximum temporal overlap with [start, end].
    
    Args:
        start: Segment start time in seconds
        end: Segment end time in seconds
        spans: List of (start, end, speaker) tuples from diarization
        min_overlap: Minimum overlap in seconds to consider (default 0.1s)
    
    Returns:
        Speaker label if overlap exceeds min_overlap threshold, None otherwise
    """
    segment_duration = end - start
    if segment_duration <= 0:
        return None
    
    best, best_overlap = None, 0.0
    for s, e, spk in spans:
        overlap = max(0.0, min(end, e) - max(start, s))
        if overlap > best_overlap:
            best_overlap, best = overlap, spk
    
    # Only assign speaker if overlap exceeds minimum threshold
    if best_overlap < min_overlap:
        return None
    
    # Additional check: overlap must be at least 30% of segment duration
    overlap_ratio = best_overlap / segment_duration
    if overlap_ratio < 0.3:
        return None
    
    return best
