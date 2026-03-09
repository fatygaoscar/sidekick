"""Ranking and filtering for concise default meeting summaries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .types import ExtractedItem, StructuredItems, TopicSegment


_TYPE_WEIGHTS = {
    "decision": 1.00,
    "action": 0.95,
    "constraint": 0.75,
    "discussion_point": 0.55,
    "proposal": 0.55,
    "issue": 0.60,
    "question": 0.50,
    "observation": 0.35,
    "direction": 0.55,
}

_LOW_VALUE_PATTERNS = (
    "thanks everyone",
    "thank you",
    "good morning",
    "good afternoon",
    "how are you",
    "sounds good",
    "all right",
    "alright",
    "small joke",
    "funny",
    "just kidding",
)

_BAD_LABEL_WORDS = {
    "you",
    "yeah",
    "but",
    "not",
    "for",
    "with",
    "this",
    "that",
    "there",
    "they",
    "them",
    "your",
    "into",
    "have",
    "will",
}


@dataclass
class RankedTopic:
    key: str
    label: str
    score: float
    items: list[ExtractedItem] = field(default_factory=list)


@dataclass
class RenderSelection:
    summary_items: list[ExtractedItem]
    decisions: list[ExtractedItem]
    actions: list[ExtractedItem]
    discussion_topics: list[RankedTopic]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _type_weight(item_type: str) -> float:
    return _TYPE_WEIGHTS.get(item_type, 0.4)


def _compute_outcome_relevance(item: ExtractedItem) -> float:
    text = _normalize_text(" ".join([item.text, item.context or "", item.evidence.quote or ""]))
    score = 0.35

    if item.type in {"decision", "action"}:
        score += 0.4
    if item.type == "constraint":
        score += 0.22
    if item.type == "issue":
        score += 0.18
    if item.type == "question":
        score += 0.1

    if any(term in text for term in ("next step", "follow up", "owner", "due", "deadline", "rollout", "launch")):
        score += 0.16
    if any(term in text for term in ("decided", "agreed", "plan", "move forward", "approved")):
        score += 0.18
    if any(term in text for term in ("must", "cannot", "should not", "only if", "guardrail", "constraint")):
        score += 0.14
    if any(term in text for term in ("question", "should we", "what if", "?")):
        score += 0.05

    return max(0.0, min(1.0, score))


def _compute_render_score(item: ExtractedItem) -> float:
    score = (
        (0.35 * item.importance)
        + (0.30 * item.outcome_relevance)
        + (0.20 * item.confidence)
        + (0.15 * _type_weight(item.type))
    )
    if item.type == "action" and (item.owner or "TBD") == "TBD":
        score -= 0.08
    if item.type == "action" and item.status in {"completed", "done"}:
        score -= 0.14
    return round(max(0.0, min(1.0, score)), 4)


def _is_low_value(item: ExtractedItem) -> bool:
    text = _normalize_text(" ".join([item.text, item.context or "", item.evidence.quote or ""]))
    if not text or len(text) < 8:
        return True
    if any(pattern in text for pattern in _LOW_VALUE_PATTERNS):
        return True
    if item.type == "observation" and item.importance < 0.5 and item.confidence < 0.75:
        return True
    return False


def _dedupe(items: list[ExtractedItem]) -> list[ExtractedItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ExtractedItem] = []
    for item in sorted(items, key=lambda current: (current.render_score, current.importance), reverse=True):
        key = (item.type, _normalize_text(item.text))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _topic_key(item: ExtractedItem) -> str:
    if item.topic_key:
        return item.topic_key
    if item.topic_id:
        return item.topic_id
    if item.thread_id:
        return item.thread_id
    words = re.findall(r"[a-z0-9]+", item.text.lower())
    return "_".join(words[:2]) or "discussion"


def _topic_label(item: ExtractedItem, topics_by_id: dict[str, TopicSegment]) -> str:
    def derive_from_text(text: str) -> str:
        words = [
            word
            for word in re.findall(r"[a-z0-9]+", text.lower())
            if len(word) > 2 and word not in _BAD_LABEL_WORDS
        ]
        return " ".join(word.capitalize() for word in words[:3]) or "Discussion"

    if item.topic_id and item.topic_id in topics_by_id:
        label = topics_by_id[item.topic_id].label
        label_words = [word.lower() for word in re.findall(r"[a-z0-9]+", label)]
        if label_words and sum(word in _BAD_LABEL_WORDS for word in label_words) < max(1, len(label_words) // 2):
            return label
    if item.topic_key:
        derived = item.topic_key.replace("_", " ").title()
        label_words = [word.lower() for word in re.findall(r"[a-z0-9]+", derived)]
        if label_words and sum(word in _BAD_LABEL_WORDS for word in label_words) < max(1, len(label_words) // 2):
            return derived
    return derive_from_text(item.text)


def _prepare_items(items: list[ExtractedItem]) -> list[ExtractedItem]:
    prepared: list[ExtractedItem] = []
    for item in items:
        item.outcome_relevance = _compute_outcome_relevance(item)
        item.render_score = _compute_render_score(item)
        if _is_low_value(item):
            item.dropped = True
            item.validation_flags.append("low_value")
            continue
        prepared.append(item)
    return _dedupe(prepared)


def _compress_topic_items(ordered_items: list[ExtractedItem]) -> list[ExtractedItem]:
    if not ordered_items:
        return []

    chosen: list[ExtractedItem] = []

    def pick(predicate):
        for item in ordered_items:
            if predicate(item) and item not in chosen:
                chosen.append(item)
                return

    pick(lambda item: item.type in {"discussion_point", "issue"})
    pick(lambda item: item.type == "constraint")
    pick(lambda item: item.type == "question")

    for item in ordered_items:
        if item not in chosen:
            chosen.append(item)
        if len(chosen) >= 3:
            break
    return chosen[:3]


def select_for_render(items: StructuredItems, topics: list[TopicSegment]) -> RenderSelection:
    topics_by_id = {topic.topic_id: topic for topic in topics}
    prepared = _prepare_items(items.all_items())

    decisions = [item for item in prepared if item.type == "decision"][:5]
    actions = sorted(
        [item for item in prepared if item.type == "action" and item.status not in {"completed", "done"}],
        key=lambda item: (((item.owner or "TBD") != "TBD"), item.render_score),
        reverse=True,
    )[:6]

    discussion_candidates = [
        item
        for item in prepared
        if item.type in {"discussion_point", "proposal", "constraint", "issue", "question", "direction", "observation"}
    ]

    grouped: dict[str, list[ExtractedItem]] = {}
    for item in discussion_candidates:
        grouped.setdefault(_topic_key(item), []).append(item)

    ranked_topics: list[RankedTopic] = []
    for key, grouped_items in grouped.items():
        ordered_items = sorted(grouped_items, key=lambda item: item.render_score, reverse=True)
        score = sum(item.render_score for item in ordered_items[:2]) + min(len(ordered_items), 3) * 0.03
        ranked_topics.append(
            RankedTopic(
                key=key,
                label=_topic_label(ordered_items[0], topics_by_id),
                score=round(score, 4),
                items=_compress_topic_items(ordered_items),
            )
        )

    ranked_topics.sort(key=lambda topic: topic.score, reverse=True)
    ranked_topics = ranked_topics[:3]

    support_items = _dedupe([item for topic in ranked_topics for item in topic.items])
    summary_items: list[ExtractedItem] = []
    summary_items.extend(decisions[:2])
    if actions:
        summary_items.append(actions[0])
    for item in support_items:
        if item not in summary_items:
            summary_items.append(item)
        if len(summary_items) >= 3:
            break
    if not decisions and len(summary_items) < 3:
        for item in actions[1:]:
            if item not in summary_items:
                summary_items.append(item)
            if len(summary_items) >= 3:
                break
    summary_items = _dedupe(summary_items)[:3]

    return RenderSelection(
        summary_items=summary_items,
        decisions=decisions,
        actions=actions,
        discussion_topics=ranked_topics,
    )
