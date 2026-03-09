"""Heuristic context inference for adaptive summarization."""

from __future__ import annotations

from collections import Counter
from typing import Awaitable, Callable

from .types import MeetingContextProfile, TranscriptTurn


LLMCallFunc = Callable[[str, str], Awaitable[str]]

_MODE_KEYWORDS = {
    "bug_triage": {
        "bug",
        "issue",
        "error",
        "broken",
        "failure",
        "failed",
        "incident",
        "regression",
        "crash",
        "hotfix",
        "defect",
        "blocker",
        "reproduce",
        "root cause",
    },
    "product_design": {
        "design",
        "ux",
        "flow",
        "logic",
        "proposal",
        "option",
        "recommendation",
        "experience",
        "variant",
        "behavior",
        "sell down",
    },
    "leadership_review": {
        "strategy",
        "strategic",
        "leadership",
        "priority",
        "roadmap",
        "budget",
        "headcount",
        "alignment",
        "executive",
        "organization",
    },
    "account_or_dealer": {
        "dealer",
        "customer",
        "account",
        "merchant",
        "retailer",
        "partner",
        "client",
        "renewal",
        "contract",
        "account team",
    },
    "rollout_or_process": {
        "rollout",
        "launch",
        "release",
        "deploy",
        "deployment",
        "timeline",
        "sequence",
        "handoff",
        "dependency",
        "approval",
        "operational",
        "process",
        "owner",
    },
    "working_session": {
        "api",
        "schema",
        "query",
        "table",
        "endpoint",
        "code",
        "function",
        "refactor",
        "branch",
        "test",
        "implementation",
        "debug",
    },
}

_ACTION_TERMS = {
    "will",
    "follow up",
    "next step",
    "owner",
    "send",
    "draft",
    "prepare",
    "assign",
    "need to",
    "action item",
}
_CONSTRAINT_TERMS = {
    "must",
    "cannot",
    "can't",
    "should not",
    "only if",
    "need to avoid",
    "constraint",
    "guardrail",
    "depends on",
}
_QUESTION_TERMS = {
    "should we",
    "what if",
    "how do we",
    "question",
    "do we want",
    "can we",
}
_DECISION_TERMS = {
    "agreed",
    "we decided",
    "we will",
    "we'll",
    "move forward",
    "approved",
    "the plan is",
}

_MODE_PRIORITY_WEIGHTS = {
    "general_business": {
        "decisions": 0.85,
        "proposals": 0.75,
        "questions": 0.70,
        "actions": 0.80,
        "constraints": 0.78,
        "issues": 0.60,
        "directions": 0.65,
        "observations": 0.40,
    },
    "bug_triage": {
        "decisions": 0.65,
        "proposals": 0.45,
        "questions": 0.55,
        "actions": 0.95,
        "constraints": 0.70,
        "issues": 0.98,
        "directions": 0.30,
        "observations": 0.25,
    },
    "product_design": {
        "decisions": 0.95,
        "proposals": 0.90,
        "questions": 0.80,
        "actions": 0.75,
        "constraints": 0.85,
        "issues": 0.45,
        "directions": 0.70,
        "observations": 0.35,
    },
    "leadership_review": {
        "decisions": 0.90,
        "proposals": 0.70,
        "questions": 0.65,
        "actions": 0.68,
        "constraints": 0.88,
        "issues": 0.52,
        "directions": 0.85,
        "observations": 0.42,
    },
    "account_or_dealer": {
        "decisions": 0.82,
        "proposals": 0.72,
        "questions": 0.74,
        "actions": 0.84,
        "constraints": 0.86,
        "issues": 0.58,
        "directions": 0.68,
        "observations": 0.38,
    },
    "rollout_or_process": {
        "decisions": 0.78,
        "proposals": 0.60,
        "questions": 0.62,
        "actions": 0.92,
        "constraints": 0.90,
        "issues": 0.72,
        "directions": 0.55,
        "observations": 0.30,
    },
    "working_session": {
        "decisions": 0.72,
        "proposals": 0.80,
        "questions": 0.78,
        "actions": 0.82,
        "constraints": 0.72,
        "issues": 0.76,
        "directions": 0.48,
        "observations": 0.44,
    },
}

_UI_PROFILE_MAP = {
    "auto": "default_business_v1",
    "meeting": "default_business_v1",
    "strategic_review": "detail_focused_v1",
    "working_session": "detail_focused_v1",
    "custom": "detail_focused_v1",
}

_UI_WEIGHT_BIAS = {
    "strategic_review": {"decisions": 0.04, "constraints": 0.04, "directions": 0.06},
    "working_session": {"proposals": 0.04, "questions": 0.04, "issues": 0.04},
    "custom": {"observations": 0.05, "questions": 0.03},
}


def _count_terms(text: str, terms: set[str]) -> int:
    return sum(text.count(term) for term in terms)


def _blend_weights(primary_mode: str, secondary_modes: list[str], ui_summary_mode: str) -> dict[str, float]:
    base = dict(_MODE_PRIORITY_WEIGHTS["general_business"])
    primary = _MODE_PRIORITY_WEIGHTS.get(primary_mode, _MODE_PRIORITY_WEIGHTS["general_business"])
    for key, value in primary.items():
        base[key] = (base[key] * 0.20) + (value * 0.80)

    if secondary_modes:
        secondary_weight = min(0.25, 0.12 * len(secondary_modes))
        for mode in secondary_modes:
            overlay = _MODE_PRIORITY_WEIGHTS.get(mode)
            if not overlay:
                continue
            for key, value in overlay.items():
                base[key] = base[key] * (1.0 - secondary_weight) + (value * secondary_weight)

    for key, delta in _UI_WEIGHT_BIAS.get(ui_summary_mode, {}).items():
        base[key] = max(0.0, min(1.0, base.get(key, 0.5) + delta))

    return {key: round(value, 3) for key, value in base.items()}


async def infer_context_profile(
    turns: list[TranscriptTurn],
    ui_summary_mode: str,
    llm_call: LLMCallFunc | None = None,
) -> MeetingContextProfile:
    """Infer internal meeting context heuristically without model calls."""
    del llm_call

    transcript_text = " ".join(turn.normalized_text.lower() for turn in turns)
    speaker_count = len({turn.speaker for turn in turns if turn.speaker})
    action_density = _count_terms(transcript_text, _ACTION_TERMS)
    decision_density = _count_terms(transcript_text, _DECISION_TERMS)
    question_density = _count_terms(transcript_text, _QUESTION_TERMS)
    constraint_density = _count_terms(transcript_text, _CONSTRAINT_TERMS)

    scores: Counter[str] = Counter({"general_business": 1.0})
    for mode, terms in _MODE_KEYWORDS.items():
        scores[mode] += _count_terms(transcript_text, terms)

    scores["rollout_or_process"] += action_density * 0.4
    scores["product_design"] += question_density * 0.25
    scores["leadership_review"] += decision_density * 0.3
    scores["general_business"] += max(1, speaker_count) * 0.05

    ordered = scores.most_common()
    primary_mode, primary_score = ordered[0]
    secondary_modes = [
        mode
        for mode, score in ordered[1:]
        if score >= max(1.25, primary_score * 0.45)
    ][:2]

    total_score = sum(score for _, score in ordered) or 1.0
    next_score = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = max(primary_score - next_score, 0.0)
    mode_confidence = min(0.95, max(0.35, (primary_score / total_score) + (margin / max(primary_score, 1.0)) * 0.25))

    priority_weights = _blend_weights(primary_mode, secondary_modes, ui_summary_mode)
    render_profile = _UI_PROFILE_MAP.get(ui_summary_mode, "default_business_v1")

    reasoning_parts = [f"Primary heuristic mode: {primary_mode.replace('_', ' ')}"]
    if secondary_modes:
        reasoning_parts.append(
            "secondary "
            + ", ".join(mode.replace("_", " ") for mode in secondary_modes)
        )
    if constraint_density:
        reasoning_parts.append("constraint-heavy")
    if action_density:
        reasoning_parts.append("action language present")

    return MeetingContextProfile(
        ui_summary_mode=ui_summary_mode or "auto",
        primary_mode=primary_mode,
        secondary_modes=secondary_modes,
        priority_weights=priority_weights,
        render_profile=render_profile,
        mode_confidence=round(mode_confidence, 3),
        reasoning_summary=", ".join(reasoning_parts),
    )
