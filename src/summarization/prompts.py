"""Prompt templates for summarization."""

SYSTEM_PROMPT = """You are an expert meeting summarizer. Your task is to create clear, actionable summaries from meeting transcripts.

Guidelines:
- Focus on key decisions, action items, and important discussions
- Use bullet points for clarity
- Highlight any deadlines or assignments mentioned
- Keep the summary concise but comprehensive
- Pay special attention to sections marked as [IMPORTANT START]...[IMPORTANT END]
- If the transcript contains important markers, ensure those topics are prominently featured"""

USER_PROMPT_TEMPLATE = """Please summarize the following meeting transcript. Pay special attention to any sections marked with [IMPORTANT START] and [IMPORTANT END] tags - these indicate topics that were flagged as particularly important during the meeting.

TRANSCRIPT:
{transcript}

Please provide:
1. **Executive Summary** (2-3 sentences)
2. **Key Discussion Points** (bullet points)
3. **Decisions Made** (if any)
4. **Action Items** (with assignees if mentioned)
5. **Important Highlights** (from marked sections)
6. **Next Steps** (if discussed)"""

QUICK_SUMMARY_TEMPLATE = """Summarize this meeting transcript in 3-5 bullet points, focusing on the most important outcomes:

{transcript}"""

ACTION_ITEMS_TEMPLATE = """Extract all action items and tasks from this meeting transcript. For each item, identify:
- The task description
- Who is responsible (if mentioned)
- Any deadline (if mentioned)

TRANSCRIPT:
{transcript}"""

DECISION_LOG_TEMPLATE = """Extract all decisions made during this meeting. For each decision, note:
- What was decided
- The context/reasoning (if discussed)
- Any conditions or caveats

TRANSCRIPT:
{transcript}"""


def get_prompt(
    prompt_type: str = "default",
    transcript: str = "",
    custom_instructions: str | None = None,
) -> tuple[str, str]:
    """
    Get system and user prompts for summarization.

    Args:
        prompt_type: Type of summary (default, quick, action_items, decisions)
        transcript: The transcript to summarize
        custom_instructions: Optional custom instructions to append

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    templates = {
        "default": USER_PROMPT_TEMPLATE,
        "quick": QUICK_SUMMARY_TEMPLATE,
        "action_items": ACTION_ITEMS_TEMPLATE,
        "decisions": DECISION_LOG_TEMPLATE,
    }

    template = templates.get(prompt_type, USER_PROMPT_TEMPLATE)
    user_prompt = template.format(transcript=transcript)

    if custom_instructions:
        user_prompt += f"\n\nAdditional instructions: {custom_instructions}"

    return SYSTEM_PROMPT, user_prompt
