#!/usr/bin/env python3
"""Shared helpers for benchmark scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings  # noqa: E402


def estimate_tokens(text: str) -> int:
    """Return a rough token estimate for budgeting and reporting."""
    return max(1, int(len(text) / 4))


def split_model_name(model: str) -> tuple[str, str]:
    """Split `family:tag` into (`family`, `tag`)."""
    if ":" not in model:
        return model, ""
    name, tag = model.split(":", 1)
    return name, tag


def ollama_run_config(model: str, num_ctx: int | None = None) -> dict[str, Any]:
    """Build a normalized benchmark config record for Ollama runs."""
    settings = get_settings()
    model_name, model_tag = split_model_name(model)
    return {
        "model_name": model_name,
        "model_tag": model_tag,
        "model_full": model,
        "runtime": "ollama",
        "num_ctx": int(num_ctx or settings.ollama_context_length),
        "temperature": float(settings.ollama_temperature),
        "top_p": float(settings.ollama_top_p),
        "top_k": int(settings.ollama_top_k),
        "repeat_penalty": float(settings.ollama_repeat_penalty),
        "seed": settings.ollama_seed,
    }


def ollama_options(model: str, num_ctx: int | None = None) -> dict[str, Any]:
    """Return Ollama options for benchmarking, mirroring app defaults."""
    settings = get_settings()
    options: dict[str, Any] = {
        "num_ctx": int(num_ctx or settings.ollama_context_length),
        "num_gpu": int(settings.ollama_num_gpu),
        "temperature": float(settings.ollama_temperature),
        "top_p": float(settings.ollama_top_p),
        "top_k": int(settings.ollama_top_k),
        "repeat_penalty": float(settings.ollama_repeat_penalty),
    }
    if not settings.ollama_think:
        options["think"] = False
    if settings.ollama_seed is not None:
        options["seed"] = settings.ollama_seed
    return options


def prompt_budget_record(system_prompt: str, user_prompt: str) -> dict[str, int]:
    """Return prompt size metadata for reports."""
    system_tokens = estimate_tokens(system_prompt)
    user_tokens = estimate_tokens(user_prompt)
    return {
        "system_prompt_chars": len(system_prompt),
        "user_prompt_chars": len(user_prompt),
        "system_prompt_estimated_tokens": system_tokens,
        "user_prompt_estimated_tokens": user_tokens,
        "prompt_estimated_tokens": system_tokens + user_tokens,
    }
