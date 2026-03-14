"""Topic-segmented summarization helpers for the MVP pipeline."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

from src.core.speaker_labels import humanize_transcript_speaker_labels

from .pipeline.preprocess import preprocess_transcript
from .prompts import (
    get_topic_segmented_pass1_prompts,
    get_topic_segmented_post_extract_label_prompt,
    get_topic_segmented_topic_identification_prompt,
)


_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "so",
    "that",
    "the",
    "their",
    "there",
    "they",
    "this",
    "to",
    "we",
    "what",
    "when",
    "with",
}
_TOPIC_SHIFT_PHRASES = (
    "another thing",
    "changing gears",
    "moving on",
    "next topic",
    "separately",
    "switching to",
)
_AGENDA_START_PHRASES = (
    "agenda",
    "today we need",
    "let's start with",
    "lets start with",
    "first item",
    "let's review",
    "lets review",
    "let's talk about",
    "lets talk about",
    "we need to talk about",
    "we need to review",
    "can you pull up",
    "jumping in",
    "diving in",
)
_BUSINESS_KEYWORDS = {
    "action",
    "account",
    "cadence",
    "customer",
    "dashboard",
    "data",
    "dealer",
    "decision",
    "filter",
    "fix",
    "forecast",
    "genesis",
    "goal",
    "inventory",
    "issue",
    "kia",
    "launch",
    "maintenance",
    "mapping",
    "metric",
    "milestone",
    "owner",
    "partner",
    "payout",
    "plan",
    "priority",
    "readiness",
    "release",
    "report",
    "reporting",
    "review",
    "risk",
    "rollout",
    "scope",
    "send",
    "system",
    "timeline",
    "update",
    "validate",
}
_ACTION_VERBS = {
    "align",
    "compare",
    "confirm",
    "decide",
    "document",
    "escalate",
    "fix",
    "investigate",
    "prepare",
    "review",
    "send",
    "share",
    "track",
    "update",
    "validate",
}
_SMALL_TALK_TERMS = {
    "coffee",
    "friday",
    "good morning",
    "good afternoon",
    "happy friday",
    "holiday",
    "how are you",
    "lunch",
    "morning",
    "patricks",
    "saint patrick",
    "st patrick",
    "st. patrick",
    "sports",
    "weather",
    "weekend",
}
_BUSINESS_FAMILY_TERMS = {
    "maintenance_reporting": {"maintenance", "report", "reporting", "readiness", "genesis", "kia", "data"},
    "dashboard_metrics": {"dashboard", "filter", "filters", "metric", "metrics", "mapping"},
    "rollout_timeline": {"rollout", "launch", "timeline", "readiness", "release", "scope"},
    "payout_process": {"payout", "cadence", "partner", "schedule", "timing"},
}
_ALLOWED_ACTION_STATUSES = {"open", "tentative", "done"}
_GENERIC_LABELS = {
    "discussion",
    "general discussion",
    "meeting discussion",
    "general update",
    "topic",
}
_LABEL_GENERIC_WORDS = {"discussion", "update", "meeting", "topic", "general"}
_LOOKAHEAD_TURNS = 3
_TAIL_TURNS_FOR_COMPARISON = 6
_LABEL_LEADING_VERBS = _ACTION_VERBS | {"discussed", "reviewed", "validated", "checking", "checked", "working"}


def _timestamp_label(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_/:-]+", text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    ]


def _keyword_overlap(left: list[str], right: list[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _window_tokens(turns: list) -> list[str]:
    tokens: list[str] = []
    for turn in turns:
        tokens.extend(_tokenize(str(getattr(turn, "normalized_text", "") or "")))
    return tokens


def _count_term_hits(text: str, terms: set[str] | tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _business_signal_score(turn) -> int:
    text = str(getattr(turn, "normalized_text", "") or "")
    tokens = set(_tokenize(text))
    score = 0
    score += 3 * _count_term_hits(text, _AGENDA_START_PHRASES)
    score += min(4, sum(1 for token in tokens if token in _BUSINESS_KEYWORDS))
    score += min(2, sum(1 for token in tokens if token in _ACTION_VERBS))
    if any(char.isdigit() for char in text):
        score += 1
    if "/" in text:
        score += 1
    return score


def _small_talk_score(turn) -> int:
    return _count_term_hits(str(getattr(turn, "normalized_text", "") or ""), _SMALL_TALK_TERMS)


def detect_business_start_index(turns: list) -> tuple[int, list[dict[str, str]], dict[str, Any]]:
    """Find the first turn that clearly starts agenda/business discussion."""
    if not turns:
        return 0, [], {"business_start_index": 0, "preamble_turn_ids": [], "preamble_turn_count": 0}

    max_scan = min(len(turns), 12)
    for index in range(max_scan):
        window = turns[index : index + 3]
        if not window:
            break
        first_business_offset = None
        agenda_hit = False
        for offset, turn in enumerate(window):
            text = str(getattr(turn, "normalized_text", "") or "")
            if _count_term_hits(text, _AGENDA_START_PHRASES) > 0 or _business_signal_score(turn) >= 2:
                if first_business_offset is None:
                    first_business_offset = offset
            if _count_term_hits(text, _AGENDA_START_PHRASES) > 0:
                agenda_hit = True
        business_turns = sum(1 for turn in window if _business_signal_score(turn) >= 2)
        small_talk_turns = sum(1 for turn in window if _small_talk_score(turn) > 0)
        if agenda_hit or (business_turns >= min(2, len(window)) and small_talk_turns < len(window)):
            start_index = index + (first_business_offset or 0)
            if index == 0 and small_talk_turns == 0 and business_turns >= min(2, len(window)):
                start_index = 0
            preamble_turns = turns[:start_index]
            warnings: list[dict[str, str]] = []
            if preamble_turns:
                warnings.append(
                    {
                        "code": "preamble_skipped",
                        "detail": "Skipped casual preamble before agenda or business discussion began.",
                    }
                )
            return start_index, warnings, {
                "business_start_index": start_index,
                "preamble_turn_ids": [str(getattr(turn, "turn_id", "")) for turn in preamble_turns if getattr(turn, "turn_id", None)],
                "preamble_turn_count": len(preamble_turns),
            }

    return 0, [{"code": "business_start_not_found", "detail": "Could not confidently detect a business-discussion start; used the full transcript."}], {
        "business_start_index": 0,
        "preamble_turn_ids": [],
        "preamble_turn_count": 0,
    }


def _top_keywords(turns: list, limit: int = 6) -> list[str]:
    counts: Counter[str] = Counter()
    for turn in turns:
        counts.update(_tokenize(str(getattr(turn, "normalized_text", "") or "")))
    return [word for word, _count in counts.most_common(limit)]


def _top_phrases_from_texts(texts: list[str], limit: int = 6) -> list[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        tokens = _tokenize(text)
        for ngram_size in (2, 3):
            for index in range(0, max(len(tokens) - ngram_size + 1, 0)):
                phrase_tokens = tokens[index : index + ngram_size]
                if not phrase_tokens:
                    continue
                if all(token in _LABEL_GENERIC_WORDS for token in phrase_tokens):
                    continue
                counts[" ".join(phrase_tokens)] += 1
    return [phrase for phrase, _count in counts.most_common(limit)]


def _top_phrases(turns: list, limit: int = 6) -> list[str]:
    return _top_phrases_from_texts([str(getattr(turn, "normalized_text", "") or "") for turn in turns], limit=limit)


def _business_families(turns: list) -> list[str]:
    tokens = set(_window_tokens(turns))
    matched: list[str] = []
    for family, keywords in _BUSINESS_FAMILY_TERMS.items():
        if tokens & keywords:
            matched.append(family)
    return matched


def _contains_topic_shift_phrase(turns: list) -> bool:
    return any(
        any(phrase in str(getattr(turn, "normalized_text", "") or "").lower() for phrase in _TOPIC_SHIFT_PHRASES)
        for turn in turns
    )


def _continuity_score(current_turns: list, next_turns: list, *, tail_turns: int = _TAIL_TURNS_FOR_COMPARISON) -> float:
    if not current_turns or not next_turns:
        return 1.0
    current_tail = current_turns[-tail_turns:]
    token_score = _keyword_overlap(_window_tokens(current_tail), _window_tokens(next_turns))
    keyword_score = _keyword_overlap(_top_keywords(current_tail, limit=6), _top_keywords(next_turns, limit=6))
    phrase_score = _keyword_overlap(_top_phrases(current_tail, limit=6), _top_phrases(next_turns, limit=6))
    family_score = _keyword_overlap(_business_families(current_tail), _business_families(next_turns))
    return (token_score + keyword_score + phrase_score + family_score) / 4.0


def _dominant_speaker(turns: list) -> tuple[str | None, float]:
    speakers = [
        str(getattr(turn, "speaker", "") or "").strip()
        for turn in turns
        if str(getattr(turn, "speaker", "") or "").strip()
    ]
    if not speakers:
        return None, 0.0
    counts = Counter(speakers)
    speaker, count = counts.most_common(1)[0]
    return speaker, count / max(len(speakers), 1)


def _speaker_shifted(current_turns: list, next_turns: list) -> bool:
    current_speaker, current_share = _dominant_speaker(current_turns[-_TAIL_TURNS_FOR_COMPARISON:])
    next_speaker, next_share = _dominant_speaker(next_turns)
    return bool(
        current_speaker
        and next_speaker
        and current_speaker != next_speaker
        and current_share >= 0.34
        and next_share >= 0.67
    )


def _format_topic_transcript(turns: list) -> str:
    lines: list[str] = []
    for turn in turns:
        timestamp = _timestamp_label(getattr(turn, "start_time", 0.0)) or "00:00"
        speaker = str(getattr(turn, "speaker", None) or "Unknown").strip() or "Unknown"
        text = str(getattr(turn, "normalized_text", "") or "").strip()
        if not text:
            continue
        lines.append(f"[{timestamp}] {speaker}: {text}")
    return "\n".join(lines)


def _timestamp_to_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    text = str(timestamp).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return float(hours * 3600 + minutes * 60 + seconds)
    return None


def _turn_index_at_or_after(turns: list, seconds: float | None) -> int | None:
    if seconds is None:
        return None
    for index, turn in enumerate(turns):
        if float(getattr(turn, "start_time", 0.0)) >= seconds - 0.5:
            return index
    return len(turns) - 1 if turns else None


def _turn_index_at_or_before(turns: list, seconds: float | None) -> int | None:
    if seconds is None:
        return None
    selected = None
    for index, turn in enumerate(turns):
        if float(getattr(turn, "start_time", 0.0)) <= seconds + 0.5:
            selected = index
        else:
            break
    return selected


def prepare_business_transcript(
    transcript: str,
) -> tuple[list, list[str], dict[str, str], list[dict[str, str]], dict[str, Any]]:
    """Apply speaker humanization and business-start filtering before topic work."""
    humanized_transcript, speaker_map = humanize_transcript_speaker_labels(transcript)
    all_turns = preprocess_transcript(humanized_transcript)
    participants = sorted(
        {
            str(getattr(turn, "speaker", "") or "").strip()
            for turn in all_turns
            if str(getattr(turn, "speaker", "") or "").strip()
        }
    )
    if not all_turns:
        return [], participants, speaker_map, [], {
            "business_start_index": 0,
            "preamble_turn_ids": [],
            "preamble_turn_count": 0,
            "topic_count": 0,
        }
    business_start_index, start_warnings, metadata = detect_business_start_index(all_turns)
    turns = all_turns[business_start_index:] or all_turns
    metadata = dict(metadata)
    metadata["topic_count"] = 0
    return turns, participants, speaker_map, list(start_warnings), metadata


def build_topic_identification_prompts(
    turns: list,
    participants: list[str],
    *,
    max_topics: int,
) -> tuple[str, str]:
    """Build the transcript-level business-topic identification prompt."""
    return get_topic_segmented_topic_identification_prompt(
        participants=participants,
        transcript=_format_topic_transcript(turns),
        max_topics=max_topics,
    )


def normalize_identified_topics(
    payload: dict[str, Any] | None,
    turns: list,
    *,
    max_topics: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize model-provided topic spans into validated extraction segments."""
    warnings: list[str] = []
    payload = payload if isinstance(payload, dict) else {}
    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, list):
        return [], ["invalid_topic_identification"]

    normalized_ranges: list[dict[str, Any]] = []
    for index, raw_topic in enumerate(raw_topics, start=1):
        if not isinstance(raw_topic, dict):
            warnings.append("invalid_topic_identification_item")
            continue
        topic_name = " ".join(str(raw_topic.get("topic_name") or "").strip().split())
        start_seconds = _timestamp_to_seconds(raw_topic.get("start_timestamp"))
        end_seconds = _timestamp_to_seconds(raw_topic.get("end_timestamp"))
        start_index = _turn_index_at_or_after(turns, start_seconds)
        end_index = _turn_index_at_or_before(turns, end_seconds)
        if start_index is None:
            warnings.append("topic_missing_start_timestamp")
            continue
        if end_index is None or end_index < start_index:
            end_index = start_index
        normalized_ranges.append(
            {
                "topic_id": str(raw_topic.get("topic_id") or f"topic_{index:03d}"),
                "label": topic_name or f"Topic {index}",
                "start_index": start_index,
                "end_index": end_index,
                "why_this_is_a_topic": " ".join(str(raw_topic.get("why_this_is_a_topic") or "").strip().split()),
            }
        )

    normalized_ranges.sort(key=lambda item: (int(item["start_index"]), int(item["end_index"])))
    clipped_ranges: list[dict[str, Any]] = []
    previous_end = -1
    for item in normalized_ranges:
        start_index = max(int(item["start_index"]), previous_end + 1)
        end_index = max(start_index, int(item["end_index"]))
        if start_index >= len(turns):
            warnings.append("topic_out_of_range_dropped")
            continue
        end_index = min(end_index, len(turns) - 1)
        clipped = dict(item)
        clipped["start_index"] = start_index
        clipped["end_index"] = end_index
        clipped_ranges.append(clipped)
        previous_end = end_index

    topics: list[dict[str, Any]] = []
    for index, item in enumerate(clipped_ranges[:max_topics], start=1):
        segment_turns = turns[int(item["start_index"]) : int(item["end_index"]) + 1]
        if not segment_turns:
            warnings.append("empty_identified_topic_dropped")
            continue
        topics.append(
            {
                "topic_id": str(item.get("topic_id") or f"topic_{index:03d}"),
                "label": str(item.get("label") or f"Topic {index}"),
                "start_timestamp": _timestamp_label(float(getattr(segment_turns[0], "start_time", 0.0))),
                "end_timestamp": _timestamp_label(float(getattr(segment_turns[-1], "end_time", 0.0))),
                "turn_ids": [str(getattr(turn, "turn_id", "")) for turn in segment_turns if getattr(turn, "turn_id", None)],
                "turn_count": len(segment_turns),
                "keywords": _top_keywords(segment_turns, limit=6),
                "transcript": _format_topic_transcript(segment_turns),
                "_turns": segment_turns,
                "why_this_is_a_topic": str(item.get("why_this_is_a_topic") or "").strip(),
            }
        )

    if len(clipped_ranges) > max_topics:
        warnings.append("topic_count_capped")
    if not topics:
        warnings.append("no_business_topics_found")
    return topics, warnings


def _should_split_segment(
    current_turns: list,
    remaining_turns: list,
    *,
    target_turns_per_topic: int,
    hard_max_turns_per_topic: int,
    large_gap_seconds: float,
    lookahead_turns: int,
) -> bool:
    if not current_turns or not remaining_turns:
        return False
    next_turn = remaining_turns[0]
    if len(current_turns) >= hard_max_turns_per_topic:
        return True

    gap = max(0.0, float(getattr(next_turn, "start_time", 0.0)) - float(getattr(current_turns[-1], "end_time", 0.0)))
    if gap >= large_gap_seconds:
        return True

    next_window = remaining_turns[:lookahead_turns]
    if _contains_topic_shift_phrase(next_window):
        return True

    if len(current_turns) < target_turns_per_topic:
        return False

    continuity = _continuity_score(current_turns, next_window)
    speaker_shift = _speaker_shifted(current_turns, next_window)
    family_overlap = _keyword_overlap(_business_families(current_turns[-_TAIL_TURNS_FOR_COMPARISON:]), _business_families(next_window))
    over_target = len(current_turns) >= target_turns_per_topic + 3

    if continuity <= 0.14 and family_overlap <= 0.20 and (speaker_shift or over_target or len(next_window) >= 2):
        return True
    if over_target and continuity <= 0.24 and family_overlap <= 0.20:
        return True
    return False


def _merge_to_topic_cap(
    segments: list[list],
    *,
    max_topics: int,
    tiny_topic_max_turns: int,
) -> tuple[list[list], list[dict[str, str]]]:
    warnings: list[dict[str, str]] = []
    working = list(segments)
    while len(working) > max_topics and len(working) >= 2:
        merge_index = 0
        best_score: tuple[float, int, int] | None = None
        for index in range(len(working) - 1):
            left = working[index]
            right = working[index + 1]
            overlap = _continuity_score(left, right)
            tiny_bonus = 0 if min(len(left), len(right)) <= tiny_topic_max_turns else 1
            combined_size = len(left) + len(right)
            score = (-overlap, tiny_bonus, combined_size)
            if best_score is None or score < best_score:
                best_score = score
                merge_index = index
        working[merge_index : merge_index + 2] = [working[merge_index] + working[merge_index + 1]]
        warnings.append({"code": "topic_cap_merge", "detail": "Merged adjacent topics to stay within the configured topic cap."})
    return working, warnings


def segment_transcript(
    transcript: str,
    *,
    target_turns_per_topic: int = 10,
    hard_max_turns_per_topic: int = 16,
    tiny_topic_max_turns: int = 2,
    large_gap_seconds: float = 90.0,
    max_topics: int = 6,
    lookahead_turns: int = _LOOKAHEAD_TURNS,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str], dict[str, str], dict[str, Any]]:
    """Split a transcript into coarse topic segments and return lightweight metadata."""
    turns, participants, speaker_map, start_warnings, metadata = prepare_business_transcript(transcript)
    if not turns:
        return [], [], participants, speaker_map, metadata

    raw_segments: list[list] = []
    current_turns = [turns[0]]
    for index, turn in enumerate(turns[1:], start=1):
        if _should_split_segment(
            current_turns,
            turns[index:],
            target_turns_per_topic=target_turns_per_topic,
            hard_max_turns_per_topic=hard_max_turns_per_topic,
            large_gap_seconds=large_gap_seconds,
            lookahead_turns=lookahead_turns,
        ):
            raw_segments.append(current_turns)
            current_turns = [turn]
        else:
            current_turns.append(turn)
    if current_turns:
        raw_segments.append(current_turns)

    warnings: list[dict[str, str]] = list(start_warnings)
    merged_segments: list[list] = []
    for segment in raw_segments:
        if len(segment) <= tiny_topic_max_turns and merged_segments:
            merged_segments[-1].extend(segment)
            warnings.append({"code": "sparse_topic_merged", "detail": "Merged a tiny topic fragment into the previous topic."})
            continue
        merged_segments.append(segment)
    if len(merged_segments) > 1 and len(merged_segments[0]) <= tiny_topic_max_turns:
        merged_segments[1] = merged_segments[0] + merged_segments[1]
        merged_segments = merged_segments[1:]
        warnings.append({"code": "sparse_topic_merged", "detail": "Merged a tiny leading topic fragment into the following topic."})

    merged_segments, cap_warnings = _merge_to_topic_cap(
        merged_segments,
        max_topics=max_topics,
        tiny_topic_max_turns=tiny_topic_max_turns,
    )
    warnings.extend(cap_warnings)

    topics: list[dict[str, Any]] = []
    for index, segment_turns in enumerate(merged_segments, start=1):
        topics.append(
            {
                "topic_id": f"topic_{index:03d}",
                "label": f"Topic {index}",
                "start_timestamp": _timestamp_label(float(getattr(segment_turns[0], "start_time", 0.0))),
                "end_timestamp": _timestamp_label(float(getattr(segment_turns[-1], "end_time", 0.0))),
                "turn_ids": [str(getattr(turn, "turn_id", "")) for turn in segment_turns if getattr(turn, "turn_id", None)],
                "turn_count": len(segment_turns),
                "keywords": _top_keywords(segment_turns, limit=6),
                "transcript": _format_topic_transcript(segment_turns),
                "_turns": segment_turns,
            }
        )

    metadata = dict(metadata)
    metadata["topic_count"] = len(topics)
    return topics, warnings, participants, speaker_map, metadata


def build_topic_extraction_prompts(topic: dict[str, Any], participants: list[str]) -> tuple[str, str]:
    """Build the strict JSON extraction prompt for one topic block."""
    return get_topic_segmented_pass1_prompts(
        participants=participants,
        topic_label=str(topic.get("topic_id") or topic.get("label") or "Topic"),
        topic_text=str(topic.get("transcript") or ""),
    )


def _string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = " ".join(str(raw or "").strip().split())
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _normalize_dict_items(
    value: object,
    *,
    required_key: str,
    allowed_keys: dict[str, str | None],
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get(required_key) or "").strip().split())
        if not text:
            continue
        normalized: dict[str, Any] = {required_key: text}
        for source_key, target_key in allowed_keys.items():
            if source_key == required_key:
                continue
            normalized_key = target_key or source_key
            raw_value = raw.get(source_key)
            if raw_value is None:
                normalized[normalized_key] = None
                continue
            value_text = " ".join(str(raw_value).strip().split())
            normalized[normalized_key] = value_text or None
        key = (text.lower(), str(normalized.get("owner") or "").lower() or None)
        if key in seen:
            continue
        seen.add(key)
        items.append(normalized)
        if len(items) >= limit:
            break
    return items


def normalize_topic_payload(topic: dict[str, Any], payload: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Normalize one extracted topic payload into the MVP schema."""
    payload = payload if isinstance(payload, dict) else {}
    warnings = _string_list(payload.get("warnings"), limit=8)

    action_items = _normalize_dict_items(
        payload.get("action_items"),
        required_key="text",
        allowed_keys={
            "text": "text",
            "owner": "owner",
            "due_date": "due_date",
            "timestamp": "timestamp",
            "status": "status",
        },
        limit=8,
    )
    normalized_actions: list[dict[str, Any]] = []
    for action in action_items:
        status = str(action.get("status") or "open").strip().lower()
        if status not in _ALLOWED_ACTION_STATUSES:
            status = "open"
        normalized_actions.append(
            {
                "text": action.get("text"),
                "owner": action.get("owner"),
                "due_date": action.get("due_date"),
                "timestamp": action.get("timestamp"),
                "status": status,
            }
        )
    if not normalized_actions:
        warnings.append("no_actionable_content_found")

    normalized = {
        "topic_id": str(topic.get("topic_id") or ""),
        "label": str(topic.get("label") or "Discussion").strip() or "Discussion",
        "start_timestamp": topic.get("start_timestamp"),
        "end_timestamp": topic.get("end_timestamp"),
        "summary": _string_list(payload.get("summary"), limit=3),
        "decisions": _normalize_dict_items(
            payload.get("decisions"),
            required_key="text",
            allowed_keys={"text": "text", "owner": "owner", "timestamp": "timestamp"},
            limit=5,
        ),
        "action_items": normalized_actions,
        "milestones": _normalize_dict_items(
            payload.get("milestones"),
            required_key="text",
            allowed_keys={"text": "text", "date": "date", "owner": "owner"},
            limit=5,
        ),
        "unresolved_questions": _normalize_dict_items(
            payload.get("unresolved_questions"),
            required_key="text",
            allowed_keys={"text": "text", "owner": "owner"},
            limit=5,
        ),
    }
    if not normalized["summary"]:
        if normalized["decisions"]:
            normalized["summary"] = [str(normalized["decisions"][0]["text"])]
        elif normalized["action_items"]:
            normalized["summary"] = [str(normalized["action_items"][0]["text"])]
        elif normalized["unresolved_questions"]:
            normalized["summary"] = [str(normalized["unresolved_questions"][0]["text"])]
    return normalized, warnings


def _normalize_label_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("`", " ").replace("*", " ").replace('"', " ").replace("'", " ")
    text = re.sub(r"^[\-\d\.\)\(:\s]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .,:;|-")
    return text


def _title_case_label(label: str) -> str:
    words: list[str] = []
    for word in label.split():
        if "/" in word or any(char.isupper() for char in word[1:]):
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _label_text_candidates(topic: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    texts.extend(_string_list(topic.get("summary"), limit=3))
    texts.extend(str(item.get("text") or "").strip() for item in (topic.get("decisions") or []) if isinstance(item, dict))
    texts.extend(str(item.get("text") or "").strip() for item in (topic.get("action_items") or []) if isinstance(item, dict))
    texts.extend(str(item.get("text") or "").strip() for item in (topic.get("unresolved_questions") or []) if isinstance(item, dict))
    texts.extend(str(item.get("text") or "").strip() for item in (topic.get("milestones") or []) if isinstance(item, dict))
    return [text for text in texts if text]


def _extract_label_phrases_from_topic(topic: dict[str, Any]) -> list[str]:
    texts = _label_text_candidates(topic)
    counts: Counter[str] = Counter()
    for phrase in _top_phrases_from_texts(texts, limit=12):
        phrase_tokens = phrase.split()
        if phrase_tokens and phrase_tokens[0] in _ACTION_VERBS and len(phrase_tokens) > 1:
            phrase = " ".join(phrase_tokens[1:])
        if phrase:
            counts[phrase] += 1
    return [phrase for phrase, _count in counts.most_common(8)]


def _structured_named_phrase_candidates(texts: list[str]) -> list[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    pattern = re.compile(r"\b[A-Z][A-Za-z]+(?:/[A-Z][A-Za-z]+)?(?:\s+[a-z][a-z0-9/-]+){0,5}")
    for text in texts:
        for match in pattern.finditer(text):
            phrase = match.group(0).strip(" .,:;|-")
            words = phrase.split()
            while words and words[0].lower() in _LABEL_LEADING_VERBS:
                words = words[1:]
            cleaned = " ".join(words).strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            counts[lowered] += 1
            display[lowered] = cleaned
    ranked = sorted(
        counts.items(),
        key=lambda item: (
            -(1 if "/" in item[0] or "kia" in item[0] or "genesis" in item[0] else 0),
            -item[1],
            -len(item[0].split()),
        ),
    )
    return [display[key] for key, _count in ranked[:6]]


def fallback_label_from_extracted_topic(topic: dict[str, Any], index: int) -> str:
    """Build a deterministic fallback label from extracted content, not transcript keywords."""
    texts = _label_text_candidates(topic)
    for phrase in _structured_named_phrase_candidates(texts):
        lowered = phrase.lower()
        if lowered in _GENERIC_LABELS:
            continue
        if any(term in lowered for term in _SMALL_TALK_TERMS):
            continue
        return _title_case_label(phrase)

    for phrase in _extract_label_phrases_from_topic(topic):
        lowered = phrase.lower()
        if lowered in _GENERIC_LABELS:
            continue
        if any(term in lowered for term in _SMALL_TALK_TERMS):
            continue
        return _title_case_label(phrase)

    counts: Counter[str] = Counter()
    for text in _label_text_candidates(topic):
        counts.update(_tokenize(text))
    ranked = []
    for token, _count in counts.most_common(6):
        if token in _LABEL_GENERIC_WORDS:
            continue
        if any(term in token for term in _SMALL_TALK_TERMS):
            continue
        ranked.append(token)
        if len(ranked) >= 3:
            break
    if ranked:
        return _title_case_label(" ".join(ranked))
    return f"Discussion {index}"


def _looks_like_business_label(label: str) -> bool:
    lowered = label.lower().strip()
    if not lowered or lowered in _GENERIC_LABELS:
        return False
    if len(lowered.split()) > 7:
        return False
    if any(term in lowered for term in _SMALL_TALK_TERMS):
        return False
    generic_ratio = 0.0
    words = lowered.split()
    if words:
        generic_ratio = sum(1 for word in words if word in _LABEL_GENERIC_WORDS) / len(words)
    return generic_ratio < 0.6


def finalize_topic_label(
    raw_label: str | None,
    *,
    fallback_label: str,
    existing_labels: set[str] | None = None,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    candidate = _normalize_label_text(raw_label or "")
    if candidate:
        if len(candidate.split()) > 7:
            candidate = " ".join(candidate.split()[:7])
        if not _looks_like_business_label(candidate):
            warnings.append("label_rejected_as_generic")
            candidate = ""
    if not candidate:
        candidate = _normalize_label_text(fallback_label)
        warnings.append("generic_label_fallback")
    candidate = _title_case_label(candidate) if candidate else fallback_label
    existing = existing_labels or set()
    if candidate.lower() in existing:
        warnings.append("duplicate_label_disambiguated")
        candidate = f"{candidate} Topic"
    return candidate or fallback_label, warnings


def build_topic_label_prompts(topic: dict[str, Any], participants: list[str]) -> tuple[str, str]:
    """Build the constrained label-generation prompt from extracted topic content."""
    start_timestamp = str(topic.get("start_timestamp") or "00:00")
    end_timestamp = str(topic.get("end_timestamp") or start_timestamp)
    return get_topic_segmented_post_extract_label_prompt(
        participants=participants,
        topic_time_range=f"{start_timestamp} - {end_timestamp}",
        summary_lines=_string_list(topic.get("summary"), limit=3),
        decision_lines=[str(item.get("text") or "").strip() for item in (topic.get("decisions") or []) if isinstance(item, dict)],
        action_lines=[
            " ".join(part for part in [str(item.get("owner") or "").strip(), str(item.get("text") or "").strip()] if part)
            for item in (topic.get("action_items") or [])
            if isinstance(item, dict)
        ],
        question_lines=[str(item.get("text") or "").strip() for item in (topic.get("unresolved_questions") or []) if isinstance(item, dict)],
        milestone_lines=[str(item.get("text") or "").strip() for item in (topic.get("milestones") or []) if isinstance(item, dict)],
    )


def evaluate_topic_quality(topic: dict[str, Any], source_turns: list) -> dict[str, object]:
    """Evaluate lightweight topic quality checks for workflow metadata."""
    label = str(topic.get("label") or "").strip()
    summary_lines = _string_list(topic.get("summary"), limit=3)
    decisions = [str(item.get("text") or "").strip() for item in (topic.get("decisions") or []) if isinstance(item, dict)]
    actions = [str(item.get("text") or "").strip() for item in (topic.get("action_items") or []) if isinstance(item, dict)]
    questions = [str(item.get("text") or "").strip() for item in (topic.get("unresolved_questions") or []) if isinstance(item, dict)]
    warnings: list[str] = []

    checks = {
        "business_label": "pass",
        "coherence": "pass",
        "filler_leakage": "pass",
        "action_alignment": "pass",
    }

    if not _looks_like_business_label(label):
        checks["business_label"] = "warn"
        warnings.append("non_business_label")

    chatter_turns = [
        turn
        for turn in source_turns
        if _small_talk_score(turn) > 0 and _business_signal_score(turn) <= 1
    ]
    if source_turns and (len(chatter_turns) / max(len(source_turns), 1)) > 0.20:
        checks["filler_leakage"] = "warn"
        warnings.append("filler_leakage")

    if len(source_turns) >= 4:
        midpoint = max(1, len(source_turns) // 2)
        coherence = _continuity_score(source_turns[:midpoint], source_turns[midpoint:])
        if coherence < 0.14:
            checks["coherence"] = "warn"
            warnings.append("low_topic_coherence")

    if actions:
        action_tokens: list[str] = []
        for text in actions:
            action_tokens.extend(_tokenize(text))
        topic_tokens: list[str] = []
        for text in summary_lines + decisions + questions:
            topic_tokens.extend(_tokenize(text))
        if topic_tokens and _keyword_overlap(action_tokens, topic_tokens) < 0.12:
            checks["action_alignment"] = "warn"
            warnings.append("action_topic_mismatch")

    return {
        "topic_id": str(topic.get("topic_id") or ""),
        "label": label,
        "checks": checks,
        "warnings": warnings,
    }


def build_meeting_summary(topics: list[dict[str, Any]]) -> list[str]:
    """Build a small top-level outcome-oriented summary from topic data."""
    bullets: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        candidate = None
        decisions = list(topic.get("decisions") or [])
        actions = list(topic.get("action_items") or [])
        summary_lines = list(topic.get("summary") or [])
        if decisions:
            candidate = str(decisions[0].get("text") or "").strip()
        elif actions:
            owner = str(actions[0].get("owner") or "TBD").strip() or "TBD"
            action_text = str(actions[0].get("text") or "").strip()
            candidate = f"{owner} to {action_text}" if action_text else None
        elif summary_lines:
            candidate = str(summary_lines[0]).strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        bullets.append(candidate)
        if len(bullets) >= 3:
            break
    return bullets or ["No material outcomes or follow-up actions were identified."]


def build_topic_segmented_summary(
    *,
    participants: list[str],
    topics: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the final topic-segmented JSON artifact."""
    return {
        "schema_version": "topic_segmented_summary_v1",
        "participants": [
            {
                "name": participant,
                "speaker_label": None,
            }
            for participant in participants
        ],
        "meeting_summary": build_meeting_summary(topics),
        "topics": topics,
        "warnings": warnings,
    }


def render_topic_segmented_markdown(payload: dict[str, Any]) -> str:
    """Render deterministic topic-segmented markdown for Obsidian."""
    summary_lines = _string_list(payload.get("meeting_summary"), limit=3)
    topics = payload.get("topics") if isinstance(payload.get("topics"), list) else []
    lines: list[str] = ["## Summary", ""]
    lines.extend(f"- {line}" for line in (summary_lines or ["No material outcomes or follow-up actions were identified."]))

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        label = str(topic.get("label") or "Discussion").strip() or "Discussion"
        topic_summary = _string_list(topic.get("summary"), limit=3)
        action_items = topic.get("action_items") if isinstance(topic.get("action_items"), list) else []
        decisions = topic.get("decisions") if isinstance(topic.get("decisions"), list) else []
        milestones = topic.get("milestones") if isinstance(topic.get("milestones"), list) else []
        questions = topic.get("unresolved_questions") if isinstance(topic.get("unresolved_questions"), list) else []

        lines.extend(["", f"## Topic: {label}", "", "### Summary", ""])
        lines.extend(f"- {line}" for line in (topic_summary or ["No high-signal summary points were extracted for this topic."]))

        lines.extend(["", "### Action Items", ""])
        if action_items:
            lines.extend(["| Owner | Action | Due |", "|-------|--------|-----|"])
            for action in action_items:
                if not isinstance(action, dict):
                    continue
                owner = str(action.get("owner") or "TBD").strip() or "TBD"
                text = str(action.get("text") or "").strip()
                due = str(action.get("due_date") or "").strip()
                lines.append(f"| {owner} | {text} | {due} |")
        else:
            lines.append("None.")

        if decisions:
            lines.extend(["", "### Decisions", ""])
            for decision in decisions:
                if not isinstance(decision, dict):
                    continue
                lines.append(f"- {str(decision.get('text') or '').strip()}")

        if milestones:
            lines.extend(["", "### Milestones", ""])
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                detail_parts = [str(milestone.get("text") or "").strip()]
                owner = str(milestone.get("owner") or "").strip()
                date = str(milestone.get("date") or "").strip()
                if owner:
                    detail_parts.append(f"owner: {owner}")
                if date:
                    detail_parts.append(f"date: {date}")
                lines.append(f"- {'; '.join(part for part in detail_parts if part)}")

        if questions:
            lines.extend(["", "### Unresolved Questions", ""])
            for question in questions:
                if not isinstance(question, dict):
                    continue
                question_text = str(question.get("text") or "").strip()
                owner = str(question.get("owner") or "").strip()
                if owner:
                    lines.append(f"- {question_text} (owner: {owner})")
                else:
                    lines.append(f"- {question_text}")

    return "\n".join(lines).strip()
