"""Structured extraction for concise default summarization."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from .entities import extract_actors
from .types import EvidenceSpan, ExtractedItem, MeetingContextProfile, TopicSegment, TranscriptChunk, TranscriptTurn


LLMCallFunc = Callable[[str, str], Awaitable[str]]

_JSON_RE = re.compile(r"```json\s*(.*?)\s*```|(\{.*\}|\[.*\])", re.DOTALL | re.IGNORECASE)
_TYPE_FIELDS = {
    "decision": "decisions",
    "action": "actions",
    "constraint": "constraints",
    "issue": "issues",
    "question": "questions",
    "discussion_point": "discussion_points",
}


def _extract_json_payload(response: str) -> Any:
    match = _JSON_RE.search(response)
    if not match:
        return None
    json_str = match.group(1) or match.group(2)
    if not json_str:
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _default_status(item_type: str) -> str:
    return {
        "decision": "confirmed",
        "action": "open",
        "constraint": "active",
        "issue": "open",
        "question": "open",
        "discussion_point": "open",
    }.get(item_type, "open")


def _build_item(
    item_type: str,
    data: dict[str, Any],
    topic: TopicSegment | None,
    turns: list[TranscriptTurn],
) -> ExtractedItem | None:
    text = (
        data.get("text")
        or data.get("decision")
        or data.get("proposal")
        or data.get("action")
        or data.get("constraint")
        or data.get("issue")
        or data.get("question")
        or data.get("discussion_point")
        or ""
    ).strip()
    if not text:
        return None

    turn_ids = [str(turn_id) for turn_id in data.get("turn_ids", []) if turn_id]
    evidence_quote = str(data.get("quote") or data.get("evidence") or text).strip()
    source_turns = [turn for turn in turns if turn.turn_id in turn_ids] or turns[:1]
    start_time = source_turns[0].start_time if source_turns else (topic.start_time if topic else 0.0)
    end_time = source_turns[-1].end_time if source_turns else (topic.end_time if topic else start_time)

    speaker = data.get("speaker")
    if isinstance(speaker, str):
        speaker = speaker.strip() or None
    owner = data.get("owner")
    if isinstance(owner, str):
        owner = owner.strip() or None
    actors = [str(actor).strip() for actor in data.get("actors", []) if str(actor).strip()]
    if not actors:
        actors = [turn.speaker for turn in source_turns if turn.speaker]
    if not actors:
        actors = extract_actors(text, [])

    return ExtractedItem(
        id="",
        type=item_type,
        text=text,
        speaker=speaker,
        actors=actors,
        owner=owner,
        due_date=(data.get("due_date") or data.get("due") or data.get("deadline")),
        source_timestamp=data.get("timestamp"),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.82)))),
        importance=max(0.0, min(1.0, float(data.get("importance", 0.55)))),
        outcome_relevance=max(0.0, min(1.0, float(data.get("outcome_relevance", data.get("importance", 0.6))))),
        rationale=data.get("rationale"),
        impact=data.get("impact"),
        mitigation=data.get("mitigation"),
        context=data.get("context"),
        who_decides=data.get("who_decides"),
        status=str(data.get("status") or _default_status(item_type)).strip().lower(),
        topic_id=topic.topic_id if topic else None,
        thread_id=topic.thread_id if topic else None,
        topic_key=(topic.label.lower().replace(" ", "_") if topic else None),
        evidence=EvidenceSpan(
            turn_ids=turn_ids or ([source_turns[0].turn_id] if source_turns else []),
            start_time=start_time,
            end_time=end_time,
            quote=evidence_quote,
        ),
    )


def _parse_meeting_response(data: Any, topic: TopicSegment | None, turns: list[TranscriptTurn]) -> list[ExtractedItem]:
    items: list[ExtractedItem] = []
    if not isinstance(data, dict):
        return items
    for item_type, field in _TYPE_FIELDS.items():
        raw_items = data.get(field, [])
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = _build_item(item_type, raw_item, topic, turns)
            if item:
                items.append(item)
    return items


def _meeting_extraction_system_prompt() -> str:
    return """You extract only the highest-value candidate facts from meeting transcript segments.

Return valid JSON only. Do not write narrative.

Use this shared schema:
{
  "decisions": [],
  "actions": [],
  "constraints": [],
  "issues": [],
  "questions": [],
  "discussion_points": []
}

Rules:
- A decision requires explicit agreement, approval, commitment, or adopted direction.
- An action is a concrete follow-up task. If owner is unclear, set owner to null.
- A constraint is a limit, dependency, sequencing rule, or guardrail.
- An issue is a bug, problem, defect, blocker, or operational failure.
- A question is unresolved.
- A discussion_point is a high-signal discussion item that materially affects meeting outcomes, tradeoffs, or next steps.

Additional rules:
- Do not try to extract every valid fact from the meeting.
- Ignore filler, greetings, jokes, personal anecdotes, and repeated framing.
- Prefer only items that materially affect decisions, actions, constraints, or unresolved tradeoffs.
- Preserve exact dates and numbers.
- Preserve speaker exactly when stated.
- Separate speaker, actors, and owner.
- Never guess owner.
- If uncertain whether something is a decision, do not classify it as a decision.
"""


def _meeting_extraction_user_prompt(
    topic: TopicSegment,
    turns: list[TranscriptTurn],
    context_profile: MeetingContextProfile,
) -> str:
    transcript_text = "\n".join(
        f"[{int(turn.start_time // 60):02d}:{int(turn.start_time % 60):02d}] {turn.speaker or 'Unknown'}: {turn.normalized_text}"
        for turn in turns
    )
    return f"""User-facing summary mode: {context_profile.ui_summary_mode}
Summary mode: {context_profile.ui_summary_mode}

Topic:
- label: {topic.label}
- topic_type: {topic.topic_type}
- thread_id: {topic.thread_id}
- reentry_index: {topic.reentry_index}

Return extracted items for this topic segment only.
For every item, include:
- text
- speaker
- actors
- owner
- timestamp
- turn_ids
- confidence
- importance
- outcome_relevance
- status
- context or rationale when relevant

Transcript topic segment:
{transcript_text}
"""


async def extract_items_from_topic_segment(
    topic: TopicSegment,
    turns: list[TranscriptTurn],
    llm_call: LLMCallFunc,
    context_profile: MeetingContextProfile,
) -> list[ExtractedItem]:
    system_prompt = _meeting_extraction_system_prompt()
    user_prompt = _meeting_extraction_user_prompt(topic, turns, context_profile)
    raw = await llm_call(system_prompt, user_prompt)
    parsed = _extract_json_payload(raw)
    items = _parse_meeting_response(parsed, topic, turns)
    return items


async def extract_items_from_chunk(
    chunk: TranscriptChunk,
    llm_call: LLMCallFunc,
    template: str = "meeting",
) -> list[ExtractedItem]:
    """Compatibility wrapper used by existing tests."""
    turns = [
        TranscriptTurn(
            turn_id=f"chunk_turn_{chunk.index}_1",
            speaker_raw=None,
            speaker=None,
            speaker_cluster=None,
            start_time=chunk.start_time,
            end_time=chunk.end_time,
            raw_text=chunk.text,
            normalized_text=chunk.text,
        )
    ]
    topic = TopicSegment(
        topic_id=f"topic_{chunk.index + 1:03d}",
        thread_id=f"thread_{chunk.index + 1:03d}",
        label="General chunk discussion",
        topic_type="general_business",
        start_time=chunk.start_time,
        end_time=chunk.end_time,
        turn_ids=[turns[0].turn_id],
        confidence=0.5,
    )
    profile = MeetingContextProfile(
        ui_summary_mode=template,
        primary_mode="general_business",
        priority_weights={},
    )
    system_prompt = _meeting_extraction_system_prompt()
    user_prompt = _meeting_extraction_user_prompt(topic, turns, profile)
    raw = await llm_call(system_prompt, user_prompt)
    parsed = _extract_json_payload(raw)
    return _parse_meeting_response(parsed, topic, turns)


def extract_participants(transcript: str) -> list[str]:
    """Extract participant names conservatively from transcript."""
    participants: set[str] = set()
    for line in transcript.splitlines():
        match = re.match(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*([^:]+):", line.strip())
        if match:
            speaker = match.group(1).strip()
            if speaker:
                participants.add(speaker)
    return sorted(participants)
