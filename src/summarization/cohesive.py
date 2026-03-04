"""Shared cohesive summary generation for regular and pipeline paths."""

import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.summarization.pipeline.types import StructuredItems


LLMCallFunc = Callable[[str, str], Awaitable[str]]

_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]+\|>")
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


def _has_artifacts(text: str) -> bool:
    return bool(
        _SPECIAL_TOKEN_RE.search(text)
        or _ORPHAN_THINK_CLOSE_RE.search(text)
        or "self-correction" in text.lower()
    )


def _has_repetition(text: str) -> bool:
    normalized = [re.sub(r"\s+", " ", s).strip().lower() for s in re.split(r"[.!?]\s+", text)]
    counts: dict[str, int] = {}
    for sentence in normalized:
        if len(sentence) < 25:
            continue
        counts[sentence] = counts.get(sentence, 0) + 1
        if counts[sentence] >= 3:
            return True
    return False


def _build_structured_evidence(items: Optional["StructuredItems"]) -> str:
    if not items:
        return ""

    def lines(values: list[str]) -> str:
        return "\n".join(values) if values else "- None"

    actions = [
        f"- {i.text} (owner: {i.owner or '-'}, due: {i.due_date or '-'})"
        for i in items.actions
    ]
    decisions = [f"- {i.text} (rationale: {i.rationale or '-'})" for i in items.decisions]
    risks = [f"- {i.text} (impact: {i.impact or '-'}, mitigation: {i.mitigation or '-'})" for i in items.risks]

    return (
        "## Structured Evidence\n"
        f"### Actions\n{lines(actions)}\n\n"
        f"### Decisions\n{lines(decisions)}\n\n"
        f"### Risks\n{lines(risks)}\n"
    )


def _build_context(
    transcript: str,
    items: Optional["StructuredItems"],
    context_length: int,
    max_context_ratio: float = 0.75,
    compressed_pack_max_chars: int = 9000,
) -> tuple[str, str]:
    approx_char_budget = max(2200, int(context_length * 3.2 * max_context_ratio))
    if len(transcript) <= approx_char_budget:
        return transcript, "full_transcript"

    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    timestamped = [line for line in lines if re.match(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]", line)]
    snippets = timestamped[:8] + timestamped[-8:] if timestamped else lines[:12] + lines[-12:]

    pack = (
        f"{_build_structured_evidence(items)}\n"
        "## Transcript Snippets\n"
        f"{'\n'.join(snippets[:20])}"
    ).strip()
    return pack[:compressed_pack_max_chars], "compressed_pack"


_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")


def _split_into_chunks(
    transcript: str, chunk_chars: int = 8000, overlap_chars: int = 400
) -> list[str]:
    """Split transcript into overlapping chunks at natural timestamp boundaries."""
    if len(transcript) <= chunk_chars:
        return [transcript]

    chunks: list[str] = []
    start = 0

    while start < len(transcript):
        end = min(start + chunk_chars, len(transcript))

        if end < len(transcript):
            # Split at the last timestamp before the target boundary
            matches = list(_TIMESTAMP_RE.finditer(transcript, start, end))
            if matches:
                split_pos = matches[-1].start()
                if split_pos > start:
                    end = split_pos

        chunks.append(transcript[start:end])

        if end >= len(transcript):
            break

        # Next chunk overlaps by overlap_chars to avoid losing cross-boundary context
        start = max(end - overlap_chars, start + 1)

    return chunks


async def _extract_chunk_prose(
    llm_call: LLMCallFunc, chunk: str, idx: int, total: int, perspective: Optional[str] = None
) -> str:
    """Extract key information from one transcript chunk as unconstrained prose."""
    system = (
        "You are extracting key information from a meeting transcript segment. "
        "Be accurate and concise."
    )
    perspective_note = ""
    if perspective and perspective.strip():
        perspective_note = f"\nNote: pay particular attention to items involving or relevant to {perspective.strip()}."
    user = (
        f"Segment {idx + 1} of {total}.\n\n"
        "From ONLY what is explicitly stated in this segment:\n"
        "- Key discussion points and outcomes\n"
        "- Decisions made (attribute to speaker if named)\n"
        "- Action items: owner name, what they will do, what project/system/feature it relates to, and deadline if stated. Use exact names and terms from the transcript.\n"
        "- Questions raised or issues flagged\n"
        f"{perspective_note}\n"
        "Use exact names, numbers, and dates from the transcript.\n"
        "Do not infer or add context beyond what's stated.\n"
        "Maximum 200 words.\n\n"
        f"Transcript:\n{chunk}"
    )
    return await llm_call(system, user)


def _build_system_prompt(
    template: str,
    perspective: Optional[str] = None,
    attendees: Optional[str] = None,
) -> str:
    focus = ""
    if perspective and perspective.strip():
        p = perspective.strip()
        focus = (
            f"This summary is from the perspective of {p}. Lead with what matters most to them, "
            "then cover the broader meeting context.\n\n"
        )
    attendees_note = ""
    if attendees and attendees.strip():
        attendees_note = (
            f"The attendees in this meeting were: {attendees.strip()}. "
            "The transcript uses SPEAKER_XX labels — use conversation context to identify who is who "
            "and use their real names throughout your summary.\n\n"
        )
    return (
        f"{focus}"
        f"{attendees_note}"
        f"You are an expert meeting summarizer for a {template} meeting.\n"
        "Write clear, cohesive output like a strong human executive assistant.\n"
        "Avoid formulaic language and repetitive phrasing.\n"
        "Do not include internal IDs (A-001, D-001, etc.).\n"
        "Extract only information explicitly stated in the transcript. "
        "Do not infer, speculate, or add context beyond what was said. "
        "If a speaker or detail is unclear, note it as unclear rather than guessing."
    )


def _build_pass1_prompt(
    template: str,
    template_contract: str,
    context_text: str,
    context_mode: str,
    custom_instructions: Optional[str],
    include_structured_tables: bool = False,
) -> str:
    custom_block = ""
    if custom_instructions and custom_instructions.strip():
        custom_block = f"\n## Additional User Instructions\n{custom_instructions.strip()}\n"

    extra_tables_block = ""
    if include_structured_tables:
        extra_tables_block = (
            "\nIn addition to the template's standard sections, add:\n"
            "- A '## Key Decisions' table with columns: | Decision | Context |\n"
            "- A '## Questions & Blockers' section listing unresolved questions or blockers raised\n"
        )

    return f"""## Style Contract
{template_contract}
{custom_block}
## Source Context ({context_mode})
{context_text}

Follow the exact section structure defined in the Style Contract above.
Use the same section headers (##) as specified in the Style Contract.
For Action Items, always use a markdown table with columns: | Owner | Action | Due |
Each action item must be self-explanatory: include the owner, what they will do, and what system/project/feature it relates to. Include deadline if stated.
Owner must come directly from the transcript: use the speaker's real name, or their SPEAKER_XX label if names are unresolved. Use "TBD" only when no speaker attribution exists at all. Never guess a name from context.
Be specific: use actual names, exact terms, concrete details, dates, and numbers from the context.
Omit filler, pleasantries, and off-topic chatter.{extra_tables_block}
"""


def _build_pass2_prompt(template: str, draft: str) -> str:
    return f"""You are editing a meeting summary draft for clarity, scannability, and Obsidian compatibility.

Requirements:
- **Preserve Structure**: Keep all ## headers and section flow.
- **Scannability**: Maximize use of bullet points (`-`). Break up any prose paragraphs into logical bullets.
- **Hierarchy**: Use nested bullets (2-space indent) for supporting details.
- **Spacing**: Ensure blank lines between headers and content, and between different topics.
- **Accuracy**: Preserve all specific details (names, dates, numbers).
- **Conciseness**: Remove repetition and filler.
- **Tables**: Ensure Action Items is a clean markdown table.

Draft:
{draft}

Return ONLY the final edited Obsidian-compatible output."""


_SPEAKER_LABEL_RE = re.compile(r"\bSPEAKER_\d+\b")


async def _resolve_speaker_map(
    llm_call: LLMCallFunc,
    transcript: str,
    attendees: str,
) -> dict[str, str]:
    """Identify which SPEAKER_XX label corresponds to which attendee.

    Uses only the first portion of the transcript where name references are
    most common (direct address, introductions, etc.).
    Returns a mapping like {"SPEAKER_00": "Oscar", "SPEAKER_01": "Pam"}.
    """
    labels = sorted(set(_SPEAKER_LABEL_RE.findall(transcript)))
    if not labels:
        return {}

    # Use first ~5000 chars for identification, plus lines where attendee names appear
    beginning = transcript[:5000]
    names = [n.strip() for n in attendees.split(",") if n.strip()]
    name_lines = [
        line for line in transcript.splitlines()
        if any(n.lower() in line.lower() for n in names)
    ]
    name_context = "\n".join(name_lines[:40]) if name_lines else ""
    sample = beginning
    if name_context and name_context not in beginning:
        sample = f"{beginning}\n\n[Lines containing attendee names throughout transcript:]\n{name_context}"

    system = (
        "You are identifying which speaker label corresponds to which person. "
        "Be precise and conservative — only assign a name when you are confident."
    )
    user = (
        f"Attendees: {attendees.strip()}\n"
        f"Speaker labels present: {', '.join(labels)}\n\n"
        "IMPORTANT: If a speaker says someone else's name (e.g. 'Thanks Pam', 'Hey Oscar'), "
        "that identifies who is being ADDRESSED, not who is speaking. "
        "Only assign a name to a label when that label is clearly identified AS that person "
        "(e.g. they introduce themselves, are introduced by someone else, or context is unambiguous).\n\n"
        "Look for clues: self-introduction ('I'm Oscar'), being introduced ('Oscar, you're up'), "
        "or unmistakable context. When unsure, return null.\n\n"
        "Return ONLY a JSON object mapping each label to a name, or null if unsure.\n"
        'Example: {"SPEAKER_00": "Oscar", "SPEAKER_01": "Pam", "SPEAKER_02": null}\n\n'
        f"Transcript sample:\n{sample}"
    )

    try:
        raw = await llm_call(system, user)
        # Extract JSON object from response
        match = re.search(r"\{[^{}]+\}", raw, re.DOTALL)
        if not match:
            return {}
        import json
        mapping = json.loads(match.group())
        return {k: v for k, v in mapping.items() if isinstance(v, str) and v.strip()}
    except Exception:
        return {}


def _apply_speaker_map(transcript: str, speaker_map: dict[str, str]) -> str:
    if not speaker_map:
        return transcript

    def replace(m: re.Match) -> str:
        return speaker_map.get(m.group(), m.group())

    return _SPEAKER_LABEL_RE.sub(replace, transcript)


def _needs_retry(text: str, items: Optional["StructuredItems"]) -> bool:
    if _has_artifacts(text) or _has_repetition(text):
        return True
    if items and items.actions and "## Action Items" not in text:
        return True
    return False


async def generate_cohesive_summary(
    llm_call: LLMCallFunc,
    transcript: str,
    template: str,
    template_contract: str,
    perspective: Optional[str] = None,
    attendees: Optional[str] = None,
    structured_items: Optional["StructuredItems"] = None,
    context_length: int = 4096,
    custom_instructions: Optional[str] = None,
    include_structured_tables: bool = False,
) -> tuple[str, str, int, str, dict[str, str]]:
    """Generate summary with mandatory two-pass flow.

    Returns:
        (final_summary, context_mode, passes_used, style_profile, speaker_map)
    """
    # Resolve SPEAKER_XX labels to real names before any summarization pass.
    speaker_map: dict[str, str] = {}
    if attendees and attendees.strip() and _SPEAKER_LABEL_RE.search(transcript):
        speaker_map = await _resolve_speaker_map(llm_call, transcript, attendees)
        logger.info("cohesive: speaker_map resolved: %s", speaker_map)
        if speaker_map:
            transcript = _apply_speaker_map(transcript, speaker_map)

    context_text, context_mode = _build_context(
        transcript=transcript,
        items=structured_items,
        context_length=context_length,
    )
    logger.info("cohesive: transcript=%d chars, context_mode=%s", len(transcript), context_mode)

    if context_mode == "compressed_pack":
        chunks = _split_into_chunks(transcript, chunk_chars=8000, overlap_chars=400)
        if len(chunks) > 1:
            extractions = []
            for i, chunk in enumerate(chunks):
                prose = await _extract_chunk_prose(llm_call, chunk, i, len(chunks), perspective=perspective)
                extractions.append(f"[Segment {i + 1}/{len(chunks)}]\n{prose.strip()}")
            context_text = "\n\n".join(extractions)
            context_mode = "chunked_extraction"

    pass1_system = _build_system_prompt(template, perspective=perspective, attendees=attendees)
    pass1_user = _build_pass1_prompt(
        template=template,
        template_contract=template_contract,
        context_text=context_text,
        context_mode=context_mode,
        custom_instructions=custom_instructions,
        include_structured_tables=include_structured_tables,
    )
    draft = await llm_call(pass1_system, pass1_user)

    # Skip Pass 2 (editorial polish) for very short transcripts to significantly speed up processing.
    # The first pass is usually high quality for short inputs.
    if len(transcript) < 3000 and not _needs_retry(draft, structured_items):
        return draft.strip(), context_mode, 1, "narrative_first_v1", speaker_map

    pass2_system = (
        f"{pass1_system}\n\nYou are now in editorial rewrite mode. Output polished final content."
    )
    pass2_user = _build_pass2_prompt(template, draft)
    final = await llm_call(pass2_system, pass2_user)
    passes_used = 2

    if _needs_retry(final, structured_items):
        retry_user = (
            f"{pass2_user}\n\nRetry constraints:\n"
            "- Remove all control tokens or model-thought remnants\n"
            "- Ensure section headers exist exactly once\n"
            "- Tighten wording and avoid repeated points"
        )
        final = await llm_call(pass2_system, retry_user)
        passes_used = 3

    return final.strip(), context_mode, passes_used, "narrative_first_v1", speaker_map
