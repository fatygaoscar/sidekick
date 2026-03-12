"""Local pyannote speaker diarization helpers."""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict

from .audio_decode import TARGET_SAMPLE_RATE, decode_audio_to_mono_float32, decode_audio_to_waveform_dict

logger = logging.getLogger(__name__)

_TARGET_SR = TARGET_SAMPLE_RATE
_SPEAKER_LABEL_RE = re.compile(r"^SPEAKER_(\d+)$")
_REQUIRED_MODEL_NAME = "pyannote/speaker-diarization-community-1"
_PIPELINE = None
_PIPELINE_MODEL_NAME = None
_PIPELINE_DEVICE = None


def _load_pipeline(hf_token: str):
    global _PIPELINE, _PIPELINE_MODEL_NAME, _PIPELINE_DEVICE
    if _PIPELINE is not None:
        return _PIPELINE

    normalized_token = str(hf_token or "").strip()
    if not normalized_token:
        raise RuntimeError("HF_TOKEN missing or empty. community-1 diarization cannot load.")

    from pyannote.audio import Pipeline
    import torch

    try:
        pipeline = Pipeline.from_pretrained(_REQUIRED_MODEL_NAME, token=normalized_token)
    except Exception as exc:  # pragma: no cover - depends on local runtime/model access
        raise RuntimeError(f"Unable to load required diarization model {_REQUIRED_MODEL_NAME}: {exc}") from exc

    device = "cpu"
    if torch.cuda.is_available():
        pipeline = pipeline.to(torch.device("cuda"))
        device = "cuda"

    _PIPELINE = pipeline
    _PIPELINE_MODEL_NAME = _REQUIRED_MODEL_NAME
    _PIPELINE_DEVICE = device
    logger.info("pyannote diarization pipeline loaded: %s (%s)", _PIPELINE_MODEL_NAME, _PIPELINE_DEVICE)
    return _PIPELINE


def get_loaded_pipeline_model_name() -> str | None:
    return _PIPELINE_MODEL_NAME


def get_loaded_pipeline_device() -> str | None:
    return _PIPELINE_DEVICE


def get_required_pipeline_model_name() -> str:
    return _REQUIRED_MODEL_NAME


def preload_required_pipeline(hf_token: str) -> tuple[str, str | None]:
    _load_pipeline(hf_token)
    return _PIPELINE_MODEL_NAME or _REQUIRED_MODEL_NAME, _PIPELINE_DEVICE


def _get_pipeline_params(min_speakers: int | None, max_speakers: int | None) -> dict:
    params = {}
    if min_speakers is not None and max_speakers is not None and min_speakers == max_speakers:
        params["num_speakers"] = min_speakers
    else:
        if min_speakers is not None:
            params["min_speakers"] = min_speakers
        if max_speakers is not None:
            params["max_speakers"] = max_speakers
    return params


def _load_waveform(
    audio_path: str,
    *,
    start_offset: float = 0.0,
    end_offset: float | None = None,
    duration_limit: float | None = None,
) -> dict:
    """Load a full or partial waveform for pyannote."""
    normalized_start = max(0.0, float(start_offset or 0.0))
    normalized_end = float(end_offset) if end_offset is not None else None
    if duration_limit is not None and normalized_end is None:
        normalized_end = max(normalized_start, float(duration_limit))

    if normalized_start <= 0.0 and normalized_end is None:
        return decode_audio_to_waveform_dict(audio_path, sample_rate=_TARGET_SR)

    import torch

    audio = decode_audio_to_mono_float32(audio_path, sample_rate=_TARGET_SR)
    start_sample = int(round(normalized_start * _TARGET_SR))
    end_sample = int(round(normalized_end * _TARGET_SR)) if normalized_end is not None else audio.shape[-1]
    end_sample = min(audio.shape[-1], max(start_sample + 1, end_sample))
    sliced = audio[start_sample:end_sample]
    if sliced.size == 0:
        raise RuntimeError(f"No audio decoded from {audio_path} for window {normalized_start:.2f}-{normalized_end!r}")
    waveform = torch.from_numpy(sliced[None, :])
    return {"waveform": waveform, "sample_rate": _TARGET_SR}


def diarize(
    audio_path: str,
    hf_token: str,
    duration_limit: float | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    start_offset: float = 0.0,
    end_offset: float | None = None,
) -> list[tuple[float, float, str]]:
    """Return (start_sec, end_sec, speaker_label) for every speaker turn."""
    pipeline = _load_pipeline(hf_token)
    normalized_start = max(0.0, float(start_offset or 0.0))
    normalized_end = float(end_offset) if end_offset is not None else None
    if duration_limit is not None and normalized_end is None:
        normalized_end = max(normalized_start, float(duration_limit))

    audio = _load_waveform(
        audio_path,
        start_offset=normalized_start,
        end_offset=normalized_end,
        duration_limit=duration_limit,
    )
    pipeline_kwargs = _get_pipeline_params(min_speakers, max_speakers)
    if pipeline_kwargs:
        logger.info("diarize: applying speaker constraints: %s", pipeline_kwargs)

    result = pipeline(audio, **pipeline_kwargs)
    spans = [
        (float(seg.start) + normalized_start, float(seg.end) + normalized_start, spk)
        for seg, _, spk in result.exclusive_speaker_diarization.itertracks(yield_label=True)
    ]

    counts = Counter(spk for _, _, spk in spans)
    logger.info(
        "diarize: model=%s spans=%d speakers=%d labels=%s",
        get_loaded_pipeline_model_name(),
        len(spans),
        len(counts),
        ", ".join(f"{spk}={n}" for spk, n in sorted(counts.items())),
    )
    return spans


def _speaker_label_index(label: str) -> int | None:
    match = _SPEAKER_LABEL_RE.match(str(label or "").strip())
    if not match:
        return None
    return int(match.group(1))


def _next_speaker_label(existing_labels: set[str]) -> str:
    next_index = max((_speaker_label_index(label) or -1) for label in existing_labels) + 1
    return f"SPEAKER_{next_index:02d}"


def _post_label_activity(
    spans: list[tuple[float, float, str]],
    *,
    start_at: float,
) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for start, end, label in spans:
        clipped_start = max(float(start), float(start_at))
        clipped_end = float(end)
        if clipped_end > clipped_start:
            totals[label] += clipped_end - clipped_start
    return totals


def _overlap_pairs(
    pre_spans: list[tuple[float, float, str]],
    post_spans: list[tuple[float, float, str]],
    *,
    overlap_start: float,
    overlap_end: float,
) -> tuple[list[tuple[float, str, str]], dict[str, float], dict[tuple[str, str], float]]:
    pairs: list[tuple[float, str, str]] = []
    post_overlap_totals: dict[str, float] = defaultdict(float)
    pair_totals: dict[tuple[str, str], float] = defaultdict(float)

    for ps, pe, plabel in pre_spans:
        pre_start = max(float(ps), overlap_start)
        pre_end = min(float(pe), overlap_end)
        if pre_end <= pre_start:
            continue
        for qs, qe, qlabel in post_spans:
            post_start = max(float(qs), overlap_start)
            post_end = min(float(qe), overlap_end)
            if post_end <= post_start:
                continue
            overlap = max(0.0, min(pre_end, post_end) - max(pre_start, post_start))
            if overlap <= 0.0:
                continue
            pairs.append((overlap, plabel, qlabel))
            post_overlap_totals[qlabel] += overlap
            pair_totals[(plabel, qlabel)] += overlap
    return pairs, dict(post_overlap_totals), dict(pair_totals)


def _best_existing_label_for_post(
    post_label: str,
    *,
    pre_labels: set[str],
    pair_totals: dict[tuple[str, str], float],
    activity_by_post_label: dict[str, float],
) -> str | None:
    best_label = None
    best_score = -1.0
    for pre_label in pre_labels:
        score = pair_totals.get((pre_label, post_label), 0.0)
        if score > best_score:
            best_score = score
            best_label = pre_label
    if best_label is not None and best_score > 0.0:
        return best_label
    if not pre_labels:
        return None
    dominant_pre = sorted(pre_labels)[0]
    if activity_by_post_label.get(post_label, 0.0) > 0.0:
        return dominant_pre
    return dominant_pre


def reconcile_windowed_speaker_labels(
    pre_spans: list[tuple[float, float, str]],
    post_spans: list[tuple[float, float, str]],
    *,
    join_offset: float,
    expected_speaker_count: int | None = None,
    repair_reason: str | None = None,
    overlap_buffer_seconds: float = 20.0,
    min_overlap_seconds: float = 1.0,
    min_overlap_ratio: float = 0.35,
) -> list[tuple[float, float, str]]:
    """Map post-window speaker labels onto pre-window labels with a capped new-speaker budget."""
    overlap_start = max(0.0, float(join_offset) - float(overlap_buffer_seconds))
    overlap_end = float(join_offset) + float(overlap_buffer_seconds)

    pre_labels = {label for _, _, label in pre_spans}
    post_labels = {label for _, _, label in post_spans}
    pairs, post_overlap_totals, pair_totals = _overlap_pairs(
        pre_spans,
        post_spans,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
    )
    mapped_pre: set[str] = set()
    mapped_post: set[str] = set()
    mapping: dict[str, str] = {}

    for overlap, pre_label, post_label in sorted(pairs, key=lambda item: item[0], reverse=True):
        if pre_label in mapped_pre or post_label in mapped_post:
            continue
        post_total = max(0.0, post_overlap_totals.get(post_label, 0.0))
        if overlap < min_overlap_seconds:
            continue
        if post_total > 0.0 and (overlap / post_total) < min_overlap_ratio:
            continue
        mapping[post_label] = pre_label
        mapped_pre.add(pre_label)
        mapped_post.add(post_label)

    unmatched_post_labels = [label for label in sorted(post_labels) if label not in mapping]
    activity_by_post_label = _post_label_activity(post_spans, start_at=join_offset)

    allow_new_labels = None
    if repair_reason == "missing_speaker" and expected_speaker_count is not None:
        allow_new_labels = max(0, int(expected_speaker_count) - len(pre_labels))

    existing_labels = set(pre_labels)
    if allow_new_labels:
        ranked_unmatched = sorted(
            unmatched_post_labels,
            key=lambda label: (activity_by_post_label.get(label, 0.0), label),
            reverse=True,
        )
        for post_label in ranked_unmatched[:allow_new_labels]:
            new_label = _next_speaker_label(existing_labels)
            existing_labels.add(new_label)
            mapping[post_label] = new_label

    for post_label in unmatched_post_labels:
        if post_label in mapping:
            continue
        fold_target = _best_existing_label_for_post(
            post_label,
            pre_labels=existing_labels,
            pair_totals=pair_totals,
            activity_by_post_label=activity_by_post_label,
        )
        if fold_target is None:
            fold_target = _next_speaker_label(existing_labels)
            existing_labels.add(fold_target)
        mapping[post_label] = fold_target

    remapped = [(start, end, mapping.get(label, label)) for start, end, label in post_spans]
    final_label_count = len({label for _, _, label in remapped})
    if expected_speaker_count is not None and final_label_count > int(expected_speaker_count):
        raise RuntimeError(
            f"windowed repair exceeded expected speaker count: expected={expected_speaker_count} actual={final_label_count}"
        )
    return remapped


def merge_windowed_spans(
    pre_spans: list[tuple[float, float, str]],
    post_spans: list[tuple[float, float, str]],
    *,
    join_offset: float,
) -> list[tuple[float, float, str]]:
    """Take pre-window spans before join and post-window spans at/after join."""
    merged: list[tuple[float, float, str]] = []
    join = float(join_offset)

    for start, end, label in pre_spans:
        clipped_end = min(float(end), join)
        if clipped_end > float(start):
            merged.append((float(start), clipped_end, label))

    for start, end, label in post_spans:
        clipped_start = max(float(start), join)
        if float(end) > clipped_start:
            merged.append((clipped_start, float(end), label))

    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    return merged


def assign_speaker(
    start: float,
    end: float,
    spans: list[tuple[float, float, str]],
    min_overlap: float = 0.03,
    min_overlap_ratio: float = 0.0,
) -> str | None:
    """Return the speaker label with maximum temporal overlap with [start, end]."""
    segment_duration = max(0.0, float(end) - float(start))
    if segment_duration <= 0.0:
        return None

    best_label = None
    best_overlap = 0.0
    for span_start, span_end, speaker_label in spans:
        overlap = max(0.0, min(float(end), float(span_end)) - max(float(start), float(span_start)))
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = speaker_label

    if best_overlap < float(min_overlap):
        return None
    if min_overlap_ratio > 0.0 and (best_overlap / segment_duration) < float(min_overlap_ratio):
        return None
    return best_label
