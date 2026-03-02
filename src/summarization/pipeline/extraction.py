"""Per-chunk item extraction for the pipeline.

Extracts actions, decisions, risks, questions, and follow-ups from
transcript chunks using LLM-based extraction with structured output.
"""

import json
import re
from typing import Any, Callable, Awaitable, Optional

from .types import ExtractedItem, TranscriptChunk


# Type alias for the LLM call function
LLMCallFunc = Callable[[str, str], Awaitable[str]]


def _parse_extraction_response(response: str, chunk_index: int) -> list[ExtractedItem]:
    """Parse LLM extraction response into ExtractedItem objects.

    The response should contain JSON with extracted items.

    Args:
        response: Raw LLM response text
        chunk_index: Index of the source chunk (for ID generation)

    Returns:
        List of extracted items
    """
    items: list[ExtractedItem] = []

    # Try to extract JSON from response
    json_match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON object/array
        json_match = re.search(r"(\{.*\}|\[.*\])", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_str = response.strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # If JSON parsing fails, return empty list
        return items

    # Handle both single object and array responses
    if isinstance(data, dict):
        # Response might have items grouped by type
        for item_type in ["actions", "decisions", "risks", "questions", "followups"]:
            if item_type in data:
                type_items = data[item_type]
                if isinstance(type_items, list):
                    for item_data in type_items:
                        item = _parse_single_item(item_data, item_type.rstrip("s"), chunk_index)
                        if item:
                            items.append(item)
    elif isinstance(data, list):
        # Direct list of items
        for item_data in data:
            item_type = item_data.get("type", "action")
            item = _parse_single_item(item_data, item_type, chunk_index)
            if item:
                items.append(item)

    return items


def _parse_single_item(
    data: dict[str, Any], item_type: str, chunk_index: int
) -> Optional[ExtractedItem]:
    """Parse a single item from JSON data.

    Args:
        data: JSON data for single item
        item_type: Type of item (action, decision, risk, question, followup)
        chunk_index: Source chunk index for ID generation

    Returns:
        ExtractedItem or None if invalid
    """
    text = data.get("text", "").strip()
    if not text:
        return None

    # Normalize type
    type_map = {
        "action": "action",
        "action_item": "action",
        "task": "action",
        "decision": "decision",
        "risk": "risk",
        "concern": "risk",
        "question": "question",
        "open_question": "question",
        "followup": "followup",
        "follow_up": "followup",
        "follow-up": "followup",
    }
    normalized_type = type_map.get(item_type.lower(), "action")

    return ExtractedItem(
        id="",  # Will be assigned during structuring
        type=normalized_type,
        text=text,
        owner=data.get("owner"),
        due_date=data.get("due_date") or data.get("due") or data.get("deadline"),
        blocking=data.get("blocking") or data.get("blocks"),
        source_timestamp=data.get("timestamp") or data.get("source_timestamp"),
        confidence=float(data.get("confidence", 0.8)),
        rationale=data.get("rationale") or data.get("reasoning"),
        impact=data.get("impact"),
        mitigation=data.get("mitigation"),
        context=data.get("context"),
        who_decides=data.get("who_decides") or data.get("decider"),
        timeline=data.get("timeline"),
        status=data.get("status", "open"),
    )


def get_extraction_system_prompt() -> str:
    """Get the system prompt for extraction pass."""
    return """You are an expert meeting analyst. Your task is to extract structured information from meeting transcript chunks.

Extract the following types of items:

1. **Actions**: Tasks to be done, with owner and deadline if mentioned
2. **Decisions**: Choices made, with rationale if discussed
3. **Risks**: Concerns or potential problems raised
4. **Questions**: Unresolved questions needing answers
5. **Follow-ups**: Items to revisit or check on later

Guidelines:
- Only extract items that are EXPLICITLY stated or strongly implied
- Do NOT hallucinate or infer items not supported by the text
- Attribute owners only when names are clearly mentioned
- Include timestamps when the item can be tied to a specific moment
- Set confidence lower (0.5-0.7) for implied items, higher (0.8-1.0) for explicit ones
- For risks, identify potential impact and any mentioned mitigation
- For questions, note who needs to decide if mentioned

Output Format:
Return a JSON object with arrays for each type. Empty arrays are fine.

```json
{
  "actions": [
    {
      "text": "The task description",
      "owner": "Person Name or null",
      "due_date": "Date if mentioned or null",
      "timestamp": "[MM:SS] where discussed",
      "confidence": 0.9
    }
  ],
  "decisions": [
    {
      "text": "What was decided",
      "rationale": "Why, if discussed",
      "owner": "Who made the decision",
      "timestamp": "[MM:SS]",
      "confidence": 0.9
    }
  ],
  "risks": [
    {
      "text": "The risk or concern",
      "impact": "Potential impact",
      "mitigation": "Any mentioned mitigation",
      "timestamp": "[MM:SS]",
      "confidence": 0.8
    }
  ],
  "questions": [
    {
      "text": "The unresolved question",
      "context": "Why it matters",
      "who_decides": "Who needs to answer",
      "timestamp": "[MM:SS]",
      "confidence": 0.9
    }
  ],
  "followups": [
    {
      "text": "Item to follow up on",
      "owner": "Who should follow up",
      "timeline": "When to follow up",
      "timestamp": "[MM:SS]",
      "confidence": 0.8
    }
  ]
}
```"""


def get_extraction_user_prompt(chunk: TranscriptChunk) -> str:
    """Get the user prompt for extraction pass.

    Args:
        chunk: Transcript chunk to process

    Returns:
        User prompt with chunk context
    """
    return f"""Extract all actions, decisions, risks, questions, and follow-ups from this transcript segment.

**Segment**: {chunk.start_timestamp} to {chunk.end_timestamp} (Chunk {chunk.index + 1})

---
TRANSCRIPT SEGMENT:
{chunk.text}
---

Remember:
- Only extract what is explicitly stated or strongly implied
- Use the timestamp range to contextualize when items occur
- Set appropriate confidence levels
- Return valid JSON"""


async def extract_items_from_chunk(
    chunk: TranscriptChunk,
    llm_call: LLMCallFunc,
) -> list[ExtractedItem]:
    """Extract items from a single transcript chunk.

    Args:
        chunk: Transcript chunk to process
        llm_call: Async function to call the LLM (system_prompt, user_prompt) -> response

    Returns:
        List of extracted items (without IDs assigned yet)
    """
    system_prompt = get_extraction_system_prompt()
    user_prompt = get_extraction_user_prompt(chunk)

    try:
        response = await llm_call(system_prompt, user_prompt)
        items = _parse_extraction_response(response, chunk.index)

        # Add source timestamp info if not already present
        for item in items:
            if not item.source_timestamp:
                item.source_timestamp = chunk.start_timestamp

        return items

    except Exception:
        # On error, return empty list - will be retried or logged
        return []


def extract_participants(transcript: str) -> list[str]:
    """Extract participant names from transcript.

    Looks for common patterns indicating speaker names:
    - "Name:" at start of line
    - "[Name]" speaker tags
    - Attribution patterns like "said John" or "John said"

    Args:
        transcript: Full transcript text

    Returns:
        List of unique participant names
    """
    participants: set[str] = set()

    # Pattern: Name: at start of line (common transcription format)
    speaker_pattern = re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*:", re.MULTILINE)
    for match in speaker_pattern.finditer(transcript):
        name = match.group(1).strip()
        if len(name) > 1 and name.lower() not in {"note", "important", "action", "decision"}:
            participants.add(name)

    # Pattern: [Name] speaker tags
    tag_pattern = re.compile(r"\[([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\]")
    for match in tag_pattern.finditer(transcript):
        name = match.group(1).strip()
        # Exclude timestamp patterns
        if not re.match(r"\d+:\d+", name):
            participants.add(name)

    # Pattern: "said Name" or "Name said/mentioned/noted"
    attribution_pattern = re.compile(
        r"(?:said|mentioned|noted|suggested|proposed|asked)\s+([A-Z][a-z]+)|"
        r"([A-Z][a-z]+)\s+(?:said|mentioned|noted|suggested|proposed|asked)"
    )
    for match in attribution_pattern.finditer(transcript):
        name = match.group(1) or match.group(2)
        if name:
            participants.add(name.strip())

    return sorted(list(participants))
