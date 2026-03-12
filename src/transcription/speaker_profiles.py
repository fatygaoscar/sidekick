"""Local speaker-profile embedding helpers."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from typing import Any

from .audio_decode import TARGET_SAMPLE_RATE, decode_audio_clip_to_mono_float32

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL_NAME = "pyannote/wespeaker-voxceleb-resnet34-LM"
_MATCH_THRESHOLD = 0.72
_MATCH_MARGIN = 0.04
MAX_PROFILE_EXAMPLES = 8
_EMBEDDING_INFERENCE = None
_EMBEDDING_DEVICE = None


def get_embedding_model_name() -> str:
    return _EMBEDDING_MODEL_NAME


def get_loaded_embedding_device() -> str | None:
    return _EMBEDDING_DEVICE


def preload_speaker_embedding_model(hf_token: str):
    global _EMBEDDING_INFERENCE, _EMBEDDING_DEVICE
    if _EMBEDDING_INFERENCE is not None:
        return _EMBEDDING_INFERENCE

    normalized_token = str(hf_token or "").strip()
    if not normalized_token:
        raise RuntimeError("HF_TOKEN missing or empty. Speaker embedding model cannot load.")

    from pyannote.audio import Inference, Model
    import torch

    model = Model.from_pretrained(_EMBEDDING_MODEL_NAME, token=normalized_token)
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    _EMBEDDING_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    _EMBEDDING_INFERENCE = Inference(model, window="whole", device=device)
    logger.info("speaker embedding model loaded: %s (%s)", _EMBEDDING_MODEL_NAME, _EMBEDDING_DEVICE)
    return _EMBEDDING_INFERENCE


def _normalize_embedding(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        return values
    return [value / norm for value in values]


def embedding_to_json(values: list[float]) -> str:
    return json.dumps([float(value) for value in values])


def embedding_from_json(value: str | None) -> list[float]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [float(item) for item in parsed]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


def aggregate_profile_embedding(examples: list[list[float]]) -> list[float]:
    usable = [example for example in examples if example]
    if not usable:
        return []
    dimension = len(usable[0])
    totals = [0.0] * dimension
    count = 0
    for example in usable:
        if len(example) != dimension:
            continue
        normalized = _normalize_embedding(example)
        for index, value in enumerate(normalized):
            totals[index] += value
        count += 1
    if count == 0:
        return []
    return _normalize_embedding([value / count for value in totals])


def _segment_duration(segment: Any) -> float:
    if isinstance(segment, dict):
        start = float(segment.get("start", segment.get("start_time", 0.0)) or 0.0)
        end = float(segment.get("end", segment.get("end_time", 0.0)) or 0.0)
        return max(0.0, end - start)
    return max(
        0.0,
        float(getattr(segment, "end", getattr(segment, "end_time", 0.0))) -
        float(getattr(segment, "start", getattr(segment, "start_time", 0.0)))
    )


def _segment_bounds(segment: Any) -> tuple[float, float]:
    if isinstance(segment, dict):
        return (
            float(segment.get("start", segment.get("start_time", 0.0)) or 0.0),
            float(segment.get("end", segment.get("end_time", 0.0)) or 0.0),
        )
    return (
        float(getattr(segment, "start", getattr(segment, "start_time", 0.0))),
        float(getattr(segment, "end", getattr(segment, "end_time", 0.0))),
    )


def choose_representative_segments(segments: list[Any], *, max_examples: int = 3) -> list[Any]:
    ordered = sorted(
        segments,
        key=lambda segment: (
            0 if 3.0 <= _segment_duration(segment) <= 8.0 else 1,
            -_segment_duration(segment),
            float(getattr(segment, "start", getattr(segment, "start_time", 0.0))),
        ),
    )
    chosen = [segment for segment in ordered if _segment_duration(segment) >= 2.0]
    if not chosen:
        chosen = ordered
    return chosen[:max_examples]


def choose_profile_example_candidate(
    segments: list[Any],
    *,
    existing_examples: list[Any] | None = None,
    max_candidates: int = 5,
    tolerance_seconds: float = 0.5,
) -> Any | None:
    candidates = choose_representative_segments(segments, max_examples=max_candidates)
    existing_examples = existing_examples or []

    for segment in candidates:
        start, end = _segment_bounds(segment)
        if _segment_duration(segment) < 2.0:
            continue
        already_used = False
        for example in existing_examples:
            example_start = float(getattr(example, "clip_start_seconds", 0.0) or 0.0)
            example_end = float(getattr(example, "clip_end_seconds", 0.0) or 0.0)
            if abs(example_start - start) <= tolerance_seconds and abs(example_end - end) <= tolerance_seconds:
                already_used = True
                break
        if not already_used:
            return segment
    return None


def extract_speaker_embedding(
    *,
    audio_path: str,
    start_time: float,
    end_time: float,
    hf_token: str,
) -> list[float]:
    inference = preload_speaker_embedding_model(hf_token)
    import torch

    clipped = decode_audio_clip_to_mono_float32(
        audio_path,
        start_time=float(start_time),
        end_time=float(end_time),
        sample_rate=TARGET_SAMPLE_RATE,
    )
    if clipped.size == 0:
        raise RuntimeError("Speaker embedding clip is empty.")

    waveform = torch.from_numpy(clipped[None, :])
    embedding = inference({"waveform": waveform, "sample_rate": TARGET_SAMPLE_RATE})
    if hasattr(embedding, "tolist"):
        embedding = embedding.tolist()
    if isinstance(embedding, list) and embedding and isinstance(embedding[0], list):
        embedding = embedding[0]
    return _normalize_embedding([float(value) for value in embedding or []])


def build_profile_example(
    *,
    audio_path: str,
    segments: list[Any],
    hf_token: str,
    existing_examples: list[Any] | None = None,
) -> dict[str, Any] | None:
    candidate = choose_profile_example_candidate(
        segments,
        existing_examples=existing_examples,
    )
    if candidate is None:
        return None

    start, end = _segment_bounds(candidate)
    embedding = extract_speaker_embedding(
        audio_path=audio_path,
        start_time=start,
        end_time=end,
        hf_token=hf_token,
    )
    if not embedding:
        return None
    return {
        "embedding": embedding,
        "segment": candidate,
    }


def build_cluster_embeddings(
    *,
    audio_path: str,
    segments: list[Any],
    hf_token: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for segment in segments:
        if isinstance(segment, dict):
            cluster = segment.get("speaker_cluster") or segment.get("speaker")
        else:
            cluster = getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)
        if cluster:
            grouped[str(cluster)].append(segment)

    cluster_embeddings: dict[str, dict[str, Any]] = {}
    for cluster, cluster_segments in grouped.items():
        representative = choose_representative_segments(cluster_segments)
        embeddings: list[list[float]] = []
        chosen_segment = None
        for segment in representative:
            if isinstance(segment, dict):
                start = float(segment.get("start", segment.get("start_time", 0.0)) or 0.0)
                end = float(segment.get("end", segment.get("end_time", 0.0)) or 0.0)
            else:
                start = float(getattr(segment, "start", getattr(segment, "start_time", 0.0)))
                end = float(getattr(segment, "end", getattr(segment, "end_time", 0.0)))
            if end - start < 2.0:
                continue
            try:
                embeddings.append(
                    extract_speaker_embedding(
                        audio_path=audio_path,
                        start_time=start,
                        end_time=end,
                        hf_token=hf_token,
                    )
                )
                chosen_segment = segment
            except Exception as exc:
                logger.warning("Failed to extract speaker embedding for %s: %s", cluster, exc)
        aggregated = aggregate_profile_embedding(embeddings)
        if not aggregated:
            continue
        cluster_embeddings[cluster] = {
            "embedding": aggregated,
            "segment": chosen_segment,
        }
    return cluster_embeddings


def match_cluster_embeddings(
    *,
    cluster_embeddings: dict[str, dict[str, Any]],
    profiles: list[Any],
    threshold: float = _MATCH_THRESHOLD,
    margin: float = _MATCH_MARGIN,
) -> dict[str, dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    for cluster, payload in cluster_embeddings.items():
        embedding = payload.get("embedding") or []
        scored: list[tuple[float, Any]] = []
        for profile in profiles:
            examples = getattr(profile, "examples", []) or []
            aggregated = aggregate_profile_embedding(
                [embedding_from_json(getattr(example, "embedding_vector_json", None)) for example in examples]
            )
            if not aggregated:
                continue
            scored.append((cosine_similarity(embedding, aggregated), profile))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            matches[cluster] = {"state": "none"}
            continue
        best_score, best_profile = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= threshold and (best_score - second_score) >= margin:
            matches[cluster] = {
                "state": "matched",
                "profile_id": str(getattr(best_profile, "id", "")),
                "profile_name": getattr(best_profile, "display_name", None),
                "score": float(best_score),
            }
        else:
            matches[cluster] = {
                "state": "suggested",
                "profile_id": str(getattr(best_profile, "id", "")),
                "profile_name": getattr(best_profile, "display_name", None),
                "score": float(best_score),
            }
    return matches


def apply_profile_matches_to_segments(
    *,
    segments: list[Any],
    matches: dict[str, dict[str, Any]],
) -> list[Any]:
    for segment in segments:
        cluster = getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None)
        if not cluster:
            continue
        match = matches.get(str(cluster)) or {}
        if match.get("state") != "matched" or not match.get("profile_name"):
            continue
        if isinstance(segment, dict):
            segment["speaker"] = str(match["profile_name"])
        else:
            setattr(segment, "speaker", str(match["profile_name"]))
    return segments


def match_segments_to_profiles(
    *,
    audio_path: str,
    segments: list[Any],
    hf_token: str,
    profiles: list[Any],
) -> dict[str, dict[str, Any]]:
    if not profiles:
        return {}
    cluster_embeddings = build_cluster_embeddings(
        audio_path=audio_path,
        segments=segments,
        hf_token=hf_token,
    )
    if not cluster_embeddings:
        return {}
    return match_cluster_embeddings(
        cluster_embeddings=cluster_embeddings,
        profiles=profiles,
    )
