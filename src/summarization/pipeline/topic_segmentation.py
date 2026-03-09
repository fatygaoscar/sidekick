"""Heuristic topic segmentation with topic re-entry support."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Awaitable, Callable

from .types import MeetingContextProfile, TopicSegment, TranscriptTurn


LLMCallFunc = Callable[[str, str], Awaitable[str]]

_STOPWORDS = {
    "the",
    "and",
    "that",
    "this",
    "with",
    "from",
    "have",
    "will",
    "need",
    "about",
    "there",
    "they",
    "them",
    "their",
    "into",
    "just",
    "because",
    "could",
    "should",
    "would",
    "what",
    "when",
    "where",
    "which",
    "while",
    "then",
    "than",
    "also",
    "like",
    "been",
    "were",
    "your",
    "you're",
    "we're",
    "going",
    "really",
    "maybe",
}

_TOPIC_KEYWORDS = {
    "bug_or_issue": {
        "bug",
        "issue",
        "error",
        "broken",
        "failure",
        "failed",
        "incident",
        "regression",
        "crash",
        "defect",
        "blocker",
        "root cause",
    },
    "design_or_solution": {
        "design",
        "logic",
        "recommendation",
        "option",
        "proposal",
        "approach",
        "flow",
        "behavior",
        "sell down",
        "return option",
    },
    "strategy_or_direction": {
        "strategy",
        "strategic",
        "priority",
        "roadmap",
        "direction",
        "positioning",
        "budget",
        "goal",
    },
    "rollout_or_process": {
        "rollout",
        "launch",
        "release",
        "deployment",
        "deploy",
        "timeline",
        "handoff",
        "sequence",
        "process",
        "approval",
        "dependency",
    },
    "account_context": {
        "dealer",
        "customer",
        "account",
        "merchant",
        "retailer",
        "partner",
        "client",
    },
    "metrics_or_analysis": {
        "metric",
        "kpi",
        "inventory",
        "percent",
        "rate",
        "data",
        "analysis",
        "forecast",
    },
    "operational_constraint": {
        "must",
        "cannot",
        "can't",
        "should not",
        "only if",
        "guardrail",
        "constraint",
        "need to avoid",
    },
}


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _count_terms(text: str, terms: set[str]) -> int:
    return sum(text.count(term) for term in terms)


def _turn_topic_type(text: str, default_type: str) -> str:
    scores = {
        topic_type: _count_terms(text, terms)
        for topic_type, terms in _TOPIC_KEYWORDS.items()
    }
    best_type, best_score = max(scores.items(), key=lambda item: item[1], default=(default_type, 0))
    return best_type if best_score > 0 else default_type


def _segment_keywords(turns: list[TranscriptTurn]) -> list[str]:
    counts: Counter[str] = Counter()
    for turn in turns:
        counts.update(_tokenize(turn.normalized_text))
    ranked = [word for word, _ in counts.most_common(4)]
    return ranked[:3] or ["discussion"]


def _segment_label(topic_type: str, keywords: list[str]) -> str:
    if not keywords:
        return "General discussion"
    label = " ".join(word.capitalize() for word in keywords[:3])
    return label if label else topic_type.replace("_", " ").capitalize()


def _thread_id(topic_type: str, keywords: list[str]) -> str:
    base = f"{topic_type}_{'_'.join(keywords[:2])}".strip("_")
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_")
    return base or f"{topic_type}_general"


def _thread_family(topic_type: str) -> str:
    if topic_type in {"design_or_solution", "metrics_or_analysis"}:
        return "design_metrics"
    if topic_type in {"rollout_or_process", "operational_constraint"}:
        return "ops_constraints"
    return topic_type


def _keyword_overlap(words1: list[str], words2: list[str]) -> float:
    set1 = set(words1)
    set2 = set(words2)
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


def _build_segment(
    index: int,
    turns: list[TranscriptTurn],
    topic_type: str,
    thread_id: str,
    reentry_index: int,
) -> TopicSegment:
    keywords = _segment_keywords(turns)
    label = _segment_label(topic_type, keywords)
    priority_tags = []
    if topic_type in {"design_or_solution", "strategy_or_direction"}:
        priority_tags.append("decision_candidate")
    if topic_type == "rollout_or_process":
        priority_tags.append("dependency")
    if topic_type == "operational_constraint":
        priority_tags.append("constraint_candidate")
    return TopicSegment(
        topic_id=f"topic_{index:03d}",
        thread_id=thread_id,
        label=label,
        topic_type=topic_type,
        start_time=turns[0].start_time,
        end_time=turns[-1].end_time,
        turn_ids=[turn.turn_id for turn in turns],
        reentry_index=reentry_index,
        priority_tags=priority_tags,
        confidence=0.74,
    )


def _initial_segments(turns: list[TranscriptTurn], default_type: str) -> list[tuple[str, list[TranscriptTurn]]]:
    if not turns:
        return []
    segments: list[tuple[str, list[TranscriptTurn]]] = []
    current_type = _turn_topic_type(turns[0].normalized_text.lower(), default_type)
    current_turns = [turns[0]]

    for turn in turns[1:]:
        turn_type = _turn_topic_type(turn.normalized_text.lower(), default_type)
        time_gap = turn.start_time - current_turns[-1].end_time
        current_keywords = _segment_keywords(current_turns)
        turn_keywords = _segment_keywords([turn])
        similarity = _keyword_overlap(current_keywords, turn_keywords)
        should_split = (
            time_gap > 120.0
            or (turn_type != current_type and len(current_turns) >= 1 and similarity < 0.2)
            or len(current_turns) >= 12
        )
        if should_split:
            segments.append((current_type, current_turns))
            current_type = turn_type
            current_turns = [turn]
        else:
            current_turns.append(turn)
            if turn_type != current_type and similarity >= 0.2:
                current_type = default_type if current_type == default_type else current_type
    if current_turns:
        segments.append((current_type, current_turns))
    return segments


def _merge_adjacent(segments: list[tuple[str, list[TranscriptTurn]]]) -> list[tuple[str, list[TranscriptTurn]]]:
    if not segments:
        return []
    merged: list[tuple[str, list[TranscriptTurn]]] = [segments[0]]
    for topic_type, turns in segments[1:]:
        prev_type, prev_turns = merged[-1]
        overlap = _keyword_overlap(_segment_keywords(prev_turns), _segment_keywords(turns))
        if topic_type == prev_type or overlap >= 0.35:
            merged[-1] = (prev_type if prev_type != "general_business" else topic_type, prev_turns + turns)
        else:
            merged.append((topic_type, turns))
    return merged


def _importance_score(topic_type: str, turns: list[TranscriptTurn], context_profile: MeetingContextProfile) -> float:
    type_to_weight_key = {
        "bug_or_issue": "issues",
        "design_or_solution": "proposals",
        "strategy_or_direction": "directions",
        "rollout_or_process": "actions",
        "account_context": "constraints",
        "metrics_or_analysis": "observations",
        "operational_constraint": "constraints",
        "general_business": "observations",
    }
    weight_key = type_to_weight_key.get(topic_type, "observations")
    text = " ".join(turn.normalized_text.lower() for turn in turns)
    evidence_density = max(1.0, len(turns) / 3.0)
    keyword_density = len(_segment_keywords(turns)) / 3.0
    return context_profile.priority_weights.get(weight_key, 0.5) * evidence_density * keyword_density * (1.0 + min(len(text) / 1500.0, 0.5))


def _merge_to_cap(
    segments: list[tuple[str, list[TranscriptTurn]]],
    context_profile: MeetingContextProfile,
    max_topics: int,
) -> list[tuple[str, list[TranscriptTurn]]]:
    if len(segments) <= max_topics:
        return segments
    working = list(segments)
    while len(working) > max_topics:
        merge_index = 0
        best_cost = None
        for index in range(len(working) - 1):
            left_type, left_turns = working[index]
            right_type, right_turns = working[index + 1]
            overlap = _keyword_overlap(_segment_keywords(left_turns), _segment_keywords(right_turns))
            priority = _importance_score(left_type, left_turns, context_profile) + _importance_score(
                right_type, right_turns, context_profile
            )
            type_penalty = 0.15 if left_type != right_type else 0.0
            cost = priority - overlap + type_penalty
            if best_cost is None or cost < best_cost:
                best_cost = cost
                merge_index = index
        left_type, left_turns = working[merge_index]
        right_type, right_turns = working[merge_index + 1]
        merged_type = left_type if left_type == right_type else context_profile.primary_mode.replace("_or_", "_")
        if merged_type not in _TOPIC_KEYWORDS and merged_type != "general_business":
            merged_type = "general_business"
        working[merge_index : merge_index + 2] = [(merged_type, left_turns + right_turns)]
    return working


async def segment_topics(
    turns: list[TranscriptTurn],
    context_profile: MeetingContextProfile,
    llm_call: LLMCallFunc | None = None,
    max_topics: int = 6,
) -> list[TopicSegment]:
    """Segment transcript heuristically and preserve topic re-entry via thread ids."""
    del llm_call
    if not turns:
        return []

    default_topic_type = {
        "bug_triage": "bug_or_issue",
        "product_design": "design_or_solution",
        "leadership_review": "strategy_or_direction",
        "account_or_dealer": "account_context",
        "rollout_or_process": "rollout_or_process",
        "working_session": "design_or_solution",
    }.get(context_profile.primary_mode, "general_business")

    segments = _initial_segments(turns, default_topic_type)
    segments = _merge_adjacent(segments)
    segments = _merge_to_cap(segments, context_profile, max_topics=max_topics)

    reentry_counts: dict[str, int] = defaultdict(int)
    prior_threads: list[tuple[str, str, list[str]]] = []
    topic_segments: list[TopicSegment] = []

    for index, (topic_type, segment_turns) in enumerate(segments, start=1):
        keywords = _segment_keywords(segment_turns)
        thread_id = _thread_id(topic_type, keywords)
        for existing_thread_id, existing_type, existing_keywords in prior_threads:
            if _thread_family(existing_type) != _thread_family(topic_type):
                continue
            if _keyword_overlap(existing_keywords, keywords) >= 0.4:
                thread_id = existing_thread_id
                break
        reentry_index = reentry_counts[thread_id]
        reentry_counts[thread_id] += 1
        topic_segments.append(
            _build_segment(index, segment_turns, topic_type, thread_id, reentry_index)
        )
        prior_threads.append((thread_id, topic_type, keywords))

    return topic_segments
