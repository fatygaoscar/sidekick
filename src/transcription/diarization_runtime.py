"""Shared readiness state for the required local diarization model."""

from __future__ import annotations

import logging
from typing import Any

from .diarize import (
    get_loaded_pipeline_device,
    get_loaded_pipeline_model_name,
    get_required_pipeline_model_name,
    preload_required_pipeline,
)

logger = logging.getLogger(__name__)

_STATE: dict[str, Any] = {
    "enabled": False,
    "ready": False,
    "model": get_required_pipeline_model_name(),
    "device": None,
    "error": None,
}


def preload_diarization_runtime(*, enabled: bool, hf_token: str) -> dict[str, Any]:
    """Preload the required diarization model and cache readiness state."""
    global _STATE

    if not enabled:
        _STATE = {
            "enabled": False,
            "ready": False,
            "model": get_required_pipeline_model_name(),
            "device": None,
            "error": None,
        }
        return dict(_STATE)

    try:
        model, device = preload_required_pipeline(hf_token)
        _STATE = {
            "enabled": True,
            "ready": True,
            "model": model,
            "device": device,
            "error": None,
        }
    except Exception as exc:
        logger.warning("Required diarization runtime unavailable: %s", exc)
        _STATE = {
            "enabled": True,
            "ready": False,
            "model": get_loaded_pipeline_model_name() or get_required_pipeline_model_name(),
            "device": get_loaded_pipeline_device(),
            "error": str(exc),
        }
    return dict(_STATE)


def diarization_runtime_state() -> dict[str, Any]:
    return dict(_STATE)


def ensure_diarization_ready(*, enabled: bool, hf_token: str) -> dict[str, Any]:
    """Return readiness state or raise a clear runtime error."""
    state = preload_diarization_runtime(enabled=enabled, hf_token=hf_token)
    if state.get("enabled") and not state.get("ready"):
        raise RuntimeError(str(state.get("error") or "Required diarization model is unavailable."))
    return state
