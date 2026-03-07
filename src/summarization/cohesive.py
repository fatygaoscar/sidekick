"""Shared cohesive summary generation for regular and pipeline paths."""

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from src.core.log_utils import pipeline_step
from src.core.speaker_labels import humanize_transcript_speaker_labels

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.summarization.pipeline.types import StructuredItems


LLMCallFunc = Callable[[str, str], Awaitable[str]]

_SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]+\|>")
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

CHUNK_PROMPT_VERSION = "chunk_extract_v2"
CHUNK_SCHEMA_VERSION = "chunk_record_v1"
DEFAULT_CHUNK_CHARS = 8000
DEFAULT_CHUNK_OVERLAP_RATIO = 0.12
MIN_CHUNK_OVERLAP_CHARS = 400
MAX_CHUNK_OVERLAP_CHARS = 1200


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


def _estimate_tokens(text: str) -> int:
    """Rough token estimate used for budgeting/debugging."""
    return max(1, int(len(text) / 4))


def _extract_json_block(text: str) -> str | None:
    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        return fenced.group(1).strip()

    raw = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if raw:
        return raw.group(1).strip()

    return None


def _split_into_chunks(
    transcript: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Split transcript into overlapping chunks at timestamp boundaries.

    The splitter preserves line integrity and prefers boundaries at the start of
    a timestamped speaker turn. Overlap defaults to ~12% to preserve continuity
    for decisions and action items that span chunk edges.
    """
    if overlap_chars is None:
        overlap_chars = min(
            MAX_CHUNK_OVERLAP_CHARS,
            max(MIN_CHUNK_OVERLAP_CHARS, int(chunk_chars * DEFAULT_CHUNK_OVERLAP_RATIO)),
        )

    if len(transcript) <= chunk_chars:
        return [{
            "index": 0,
            "text": transcript,
            "start_char": 0,
            "end_char": len(transcript),
            "overlap_chars": 0,
            "estimated_tokens": _estimate_tokens(transcript),
            "speaker_turns": max(1, len([ln for ln in transcript.splitlines() if ln.strip()])),
        }]

    chunks: list[dict[str, Any]] = []
    start = 0
    chunk_index = 0

    while start < len(transcript):
        end = min(start + chunk_chars, len(transcript))

        if end < len(transcript):
            matches = list(_TIMESTAMP_RE.finditer(transcript, start + 1, end))
            if matches:
                split_pos = matches[-1].start()
                if split_pos > start:
                    end = split_pos

        chunk_text = transcript[start:end]
        chunks.append({
            "index": chunk_index,
            "text": chunk_text,
            "start_char": start,
            "end_char": end,
            "overlap_chars": 0 if chunk_index == 0 else min(overlap_chars, end - start),
            "estimated_tokens": _estimate_tokens(chunk_text),
            "speaker_turns": max(1, len([ln for ln in chunk_text.splitlines() if ln.strip()])),
        })

        if end >= len(transcript):
            break

        start = max(end - overlap_chars, start + 1)
        chunk_index += 1

    return chunks


def _empty_chunk_record() -> dict[str, Any]:
    return {
        "segment_summary": "",
        "discussion_points": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "risks": [],
        "unresolved_items": [],
        "notable_quotes_or_context": [],
    }


def _validate_chunk_record(data: dict[str, Any]) -> tuple[bool, str | None]:
    required = {
        "segment_summary": str,
        "discussion_points": list,
        "decisions": list,
        "action_items": list,
        "open_questions": list,
        "risks": list,
        "unresolved_items": list,
        "notable_quotes_or_context": list,
    }

    for key, expected_type in required.items():
        if key not in data:
            return False, f"missing_key:{key}"
        if not isinstance(data[key], expected_type):
            return False, f"wrong_type:{key}"
    return True, None


def _chunk_record_semantically_incomplete(data: dict[str, Any], chunk_text: str) -> bool:
    extracted_count = sum(
        len(data.get(key, []))
        for key in (
            "discussion_points",
            "decisions",
            "action_items",
            "open_questions",
            "risks",
            "unresolved_items",
            "notable_quotes_or_context",
        )
    )
    if extracted_count > 0:
        return False

    cue_re = re.compile(r"\b(decid|should|will|need to|follow up|question|risk|blocker)\b|\?")
    return bool(cue_re.search(chunk_text.lower()))


def _render_chunk_record(record: dict[str, Any], idx: int, total: int) -> str:
    return (
        f"## Segment {idx + 1}/{total}\n"
        f"Segment Summary: {record.get('segment_summary', '').strip() or '-'}\n\n"
        f"Discussion Points:\n{json.dumps(record.get('discussion_points', []), ensure_ascii=True, indent=2)}\n\n"
        f"Decisions:\n{json.dumps(record.get('decisions', []), ensure_ascii=True, indent=2)}\n\n"
        f"Action Items:\n{json.dumps(record.get('action_items', []), ensure_ascii=True, indent=2)}\n\n"
        f"Open Questions:\n{json.dumps(record.get('open_questions', []), ensure_ascii=True, indent=2)}\n\n"
        f"Risks:\n{json.dumps(record.get('risks', []), ensure_ascii=True, indent=2)}\n\n"
        f"Unresolved Items:\n{json.dumps(record.get('unresolved_items', []), ensure_ascii=True, indent=2)}\n\n"
        "Notable Quotes Or Context:\n"
        f"{json.dumps(record.get('notable_quotes_or_context', []), ensure_ascii=True, indent=2)}"
    )


async def _extract_chunk_record(
    llm_call: LLMCallFunc,
    chunk: dict[str, Any],
    total: int,
    perspective: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract a schema-constrained fact record for one transcript chunk."""
    idx = int(chunk["index"])
    chunk_text = str(chunk["text"])
    perspective_note = ""
    if perspective and perspective.strip():
        perspective_note = (
            f"\nPerspective focus: pay extra attention to implications for {perspective.strip()}, "
            "but do not drop information that matters to the rest of the meeting."
        )

    system = (
        "You are extracting structured evidence from a meeting transcript segment.\n"
        "Return factual records, not polished prose.\n"
        "Do not omit tentative decisions, ambiguous ownership, or unresolved items.\n"
        "When ownership is unclear, preserve the exact speaker label or set owner to null.\n"
        "Return ONLY valid JSON matching the requested schema."
    )
    user = (
        f"Segment {idx + 1} of {total}.\n"
        f"Schema version: {CHUNK_SCHEMA_VERSION}.{perspective_note}\n\n"
        "Extract these top-level fields exactly:\n"
        "- segment_summary: short factual recap of this segment only\n"
        "- discussion_points: list of {timestamp, speaker, point, status}\n"
        "- decisions: list of {timestamp, speaker, decision, rationale, status}\n"
        "- action_items: list of {timestamp, speaker, owner, action, due, status}\n"
        "- open_questions: list of {timestamp, speaker, question, context, owner}\n"
        "- risks: list of {timestamp, speaker, risk, impact, mitigation}\n"
        "- unresolved_items: list of {timestamp, speaker, item, reason, owner}\n"
        "- notable_quotes_or_context: list of {timestamp, speaker, quote_or_context}\n\n"
        "Rules:\n"
        "- preserve timestamps and speaker labels exactly when present\n"
        "- include tentative/ambiguous items with status='tentative' or owner=null\n"
        "- prefer facts and compact records over narrative prose\n"
        "- do not infer missing names, dates, or decisions\n\n"
        f"Transcript:\n{chunk_text}"
    )

    diagnostics: dict[str, Any] = {
        "chunk_index": idx,
        "parse_status": "ok",
        "schema_status": "ok",
        "semantic_status": "ok",
        "attempts": 1,
        "estimated_prompt_tokens": _estimate_tokens(system) + _estimate_tokens(user),
        "estimated_chunk_tokens": int(chunk.get("estimated_tokens", _estimate_tokens(chunk_text))),
    }

    raw = await llm_call(system, user)
    json_str = _extract_json_block(raw)
    if not json_str:
        diagnostics["parse_status"] = "json_parse_failure"
        repair_prompt = (
            f"Rewrite the following response as valid JSON only using schema {CHUNK_SCHEMA_VERSION}.\n\n"
            f"Transcript:\n{chunk_text}\n\n"
            f"Previous response:\n{raw}"
        )
        diagnostics["attempts"] = 2
        diagnostics["estimated_prompt_tokens"] += _estimate_tokens(repair_prompt)
        raw = await llm_call(system, repair_prompt)
        json_str = _extract_json_block(raw)

    data: dict[str, Any] | None = None
    if json_str:
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                data = parsed
            else:
                diagnostics["schema_status"] = "schema_failure"
        except json.JSONDecodeError:
            diagnostics["parse_status"] = "json_parse_failure"

    if data is None:
        return _empty_chunk_record(), diagnostics

    is_valid, schema_issue = _validate_chunk_record(data)
    if not is_valid:
        diagnostics["schema_status"] = f"schema_failure:{schema_issue}"
        repair_prompt = (
            f"Fix this JSON to match schema {CHUNK_SCHEMA_VERSION} exactly.\n\n"
            f"Transcript:\n{chunk_text}\n\n"
            f"Broken JSON:\n{json.dumps(data, ensure_ascii=True)}"
        )
        diagnostics["attempts"] = max(int(diagnostics["attempts"]), 2)
        diagnostics["estimated_prompt_tokens"] += _estimate_tokens(repair_prompt)
        raw = await llm_call(system, repair_prompt)
        json_str = _extract_json_block(raw)
        if json_str:
            try:
                repaired = json.loads(json_str)
                if isinstance(repaired, dict):
                    data = repaired
                    is_valid, schema_issue = _validate_chunk_record(data)
                    if is_valid:
                        diagnostics["schema_status"] = "ok"
            except json.JSONDecodeError:
                diagnostics["parse_status"] = "json_parse_failure"

    if not is_valid:
        return _empty_chunk_record(), diagnostics

    if _chunk_record_semantically_incomplete(data, chunk_text):
        diagnostics["semantic_status"] = "semantically_incomplete"
        retry_prompt = (
            f"{user}\n\nRetry instructions:\n"
            "- Do not return empty arrays if this segment contains tentative decisions, ambiguous owners, or open loops.\n"
            "- Preserve uncertain ownership as null rather than omitting the item.\n"
            "- Include at least the strongest discussion points and any action/decision/question/risk cues present."
        )
        diagnostics["attempts"] = max(int(diagnostics["attempts"]), 2)
        diagnostics["estimated_prompt_tokens"] += _estimate_tokens(retry_prompt)
        raw = await llm_call(system, retry_prompt)
        json_str = _extract_json_block(raw)
        if json_str:
            try:
                retried = json.loads(json_str)
                if isinstance(retried, dict):
                    retry_valid, retry_issue = _validate_chunk_record(retried)
                    if retry_valid:
                        data = retried
                        diagnostics["semantic_status"] = "ok"
                        diagnostics["schema_status"] = "ok"
                    else:
                        diagnostics["schema_status"] = f"schema_failure:{retry_issue}"
            except json.JSONDecodeError:
                diagnostics["parse_status"] = "json_parse_failure"

    return data, diagnostics


def _build_system_prompt(
    template: str,
    perspective: Optional[str] = None,
) -> str:
    focus = ""
    if perspective and perspective.strip():
        p = perspective.strip()
        focus = (
            f"This summary is from the perspective of {p}. Lead with what matters most to them, "
            "then cover the broader meeting context.\n\n"
        )
    return (
        f"{focus}"
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
    if template == "custom":
        return f"""## Style Contract
{template_contract}

## Source Context ({context_mode})
{context_text}

Follow the Style Contract above exactly.
Do not add sections, tables, or formatting requirements that are not explicitly requested in the Style Contract.
If the Style Contract asks for concise or simple output, keep it concise and simple.
Use concrete names, dates, numbers, and owners only when they materially support the requested output.
Extract only what the Style Contract asks for. Omit filler, pleasantries, and side chatter.
"""

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

    synthesis_note = ""
    if context_mode == "chunked_extraction":
        synthesis_note = (
            "\nThe source context consists of structured per-segment extraction records. "
            "Deduplicate repeated points across segments, resolve conflicts conservatively, "
            "and preserve tentative decisions or ambiguous ownership rather than dropping them.\n"
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
Owner must come directly from the transcript: use the speaker's real name when known, otherwise use the transcript's provided fallback label such as Attendee A. Use "TBD" only when no speaker attribution exists at all. Never guess a name from context or invent a role.
Be specific: use actual names, exact terms, concrete details, dates, and numbers from the context.
Omit filler, pleasantries, and off-topic chatter.{synthesis_note}{extra_tables_block}
"""


def _build_pass2_prompt(template: str, draft: str) -> str:
    if template == "custom":
        return f"""You are editing a custom meeting summary draft for clarity and accuracy.

Requirements:
- Preserve the exact section structure already present in the draft.
- Do not add new sections, tables, or extra structure unless they already exist in the draft.
- Preserve the draft's requested level of detail. If it is concise, keep it concise.
- Improve wording, scannability, and consistency without broadening the scope.
- Preserve all concrete names, dates, numbers, and decisions already present.

Draft:
{draft}

Return ONLY the final edited output."""

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
    structured_items: Optional["StructuredItems"] = None,
    context_length: int = 4096,
    custom_instructions: Optional[str] = None,
    include_structured_tables: bool = False,
    progress_callback: Optional[Callable[[float], None]] = None,
    force_context_mode: Optional[str] = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int | None = None,
    debug_info: Optional[dict[str, Any]] = None,
) -> tuple[str, str, int, str, dict[str, str], dict[str, str]]:
    """Generate summary with mandatory two-pass flow.

    Returns:
        (final_summary, context_mode, passes_used, style_profile, speaker_map, prompt_audit)
    """
    def _emit(p: float) -> None:
        if progress_callback:
            try:
                progress_callback(p)
            except Exception:
                pass

    _emit(0.02)
    transcript, speaker_map = humanize_transcript_speaker_labels(transcript)
    _emit(0.08)

    approx_char_budget = max(2200, int(context_length * 3.2 * 0.75))
    context_text, context_mode = _build_context(
        transcript=transcript,
        items=structured_items,
        context_length=context_length,
    )
    if force_context_mode == "full_transcript":
        context_text = transcript
        context_mode = "full_transcript"
    elif force_context_mode == "chunked_extraction":
        context_text = transcript
        context_mode = "compressed_pack"
    logger.info(
        "cohesive: transcript=%d chars, budget=%d chars, context_mode=%s",
        len(transcript), approx_char_budget, context_mode,
    )

    if debug_info is not None:
        debug_info["transcript_chars"] = len(transcript)
        debug_info["transcript_estimated_tokens"] = _estimate_tokens(transcript)
        debug_info["approx_char_budget"] = approx_char_budget
        debug_info["initial_context_mode"] = context_mode

    if context_mode == "compressed_pack":
        chunks = _split_into_chunks(transcript, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
        if debug_info is not None:
            debug_info["chunk_strategy"] = "timestamp_boundary_chars"
            debug_info["chunk_size"] = chunk_chars
            debug_info["overlap_size"] = chunks[1]["overlap_chars"] if len(chunks) > 1 else 0
            debug_info["chunk_count"] = len(chunks)
            debug_info["chunks"] = [
                {
                    "index": ch["index"],
                    "start_char": ch["start_char"],
                    "end_char": ch["end_char"],
                    "overlap_chars": ch["overlap_chars"],
                    "estimated_tokens": ch["estimated_tokens"],
                    "speaker_turns": ch["speaker_turns"],
                }
                for ch in chunks
            ]
        if len(chunks) > 1:
            extractions = []
            chunk_diagnostics: list[dict[str, Any]] = []
            with pipeline_step(logger, "chunk_extraction", n_chunks=len(chunks)):
                for i, chunk in enumerate(chunks):
                    logger.info("[step] chunk_extraction | chunk %d/%d", i + 1, len(chunks))
                    record, diagnostics = await _extract_chunk_record(
                        llm_call,
                        chunk,
                        len(chunks),
                        perspective=perspective,
                    )
                    chunk_diagnostics.append(diagnostics)
                    extractions.append(_render_chunk_record(record, i, len(chunks)))
            context_text = "\n\n".join(extractions)
            context_mode = "chunked_extraction"
            if debug_info is not None:
                debug_info["chunk_diagnostics"] = chunk_diagnostics
                debug_info["chunk_merge_input_chars"] = len(context_text)
                debug_info["chunk_merge_input_estimated_tokens"] = _estimate_tokens(context_text)

    pass1_system = _build_system_prompt(template, perspective=perspective)
    pass1_user = _build_pass1_prompt(
        template=template,
        template_contract=template_contract,
        context_text=context_text,
        context_mode=context_mode,
        custom_instructions=custom_instructions,
        include_structured_tables=include_structured_tables,
    )
    if debug_info is not None:
        debug_info["final_context_mode"] = context_mode
        debug_info["pass1_prompt_estimated_tokens"] = (
            _estimate_tokens(pass1_system) + _estimate_tokens(pass1_user)
        )
    with pipeline_step(logger, "summarize_pass1", mode=context_mode, chars=len(transcript)):
        draft = await llm_call(pass1_system, pass1_user)
    _emit(0.65)

    prompt_audit = {
        "pass1_system_prompt": pass1_system,
        "pass1_user_prompt": pass1_user,
        "pass2_system_prompt": "",
        "pass2_user_prompt": "",
    }

    # Skip Pass 2 (editorial polish) for very short transcripts to significantly speed up processing.
    # The first pass is usually high quality for short inputs.
    if len(transcript) < 3000 and not _needs_retry(draft, structured_items):
        _emit(0.90)
        return draft.strip(), context_mode, 1, "narrative_first_v1", speaker_map, prompt_audit

    pass2_system = (
        f"{pass1_system}\n\nYou are now in editorial rewrite mode. Output polished final content."
    )
    pass2_user = _build_pass2_prompt(template, draft)
    prompt_audit["pass2_system_prompt"] = pass2_system
    prompt_audit["pass2_user_prompt"] = pass2_user
    if debug_info is not None:
        debug_info["pass2_prompt_estimated_tokens"] = (
            _estimate_tokens(pass2_system) + _estimate_tokens(pass2_user)
        )
    with pipeline_step(logger, "summarize_pass2"):
        final = await llm_call(pass2_system, pass2_user)
    passes_used = 2
    _emit(0.90)

    if _needs_retry(final, structured_items):
        retry_user = (
            f"{pass2_user}\n\nRetry constraints:\n"
            "- Remove all control tokens or model-thought remnants\n"
            "- Ensure section headers exist exactly once\n"
            "- Tighten wording and avoid repeated points"
        )
        with pipeline_step(logger, "summarize_retry"):
            final = await llm_call(pass2_system, retry_user)
        passes_used = 3
        _emit(0.95)

    return final.strip(), context_mode, passes_used, "narrative_first_v1", speaker_map, prompt_audit
