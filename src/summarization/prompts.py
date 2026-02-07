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

# New structured templates for Obsidian export

MEETING_TEMPLATE = """Create a comprehensive meeting summary following this structure:

## Meeting Overview
Brief 2-3 sentence summary of the meeting's purpose and outcome.

## Attendees
List participants if mentioned, otherwise note "Not specified"

## Agenda/Topics Discussed
- Topic 1
- Topic 2
...

## Key Decisions
- Decision 1: [description] - Rationale: [if mentioned]
- Decision 2: ...

## Action Items
| Task | Owner | Due Date |
|------|-------|----------|
| ... | ... | ... |

## Follow-up Items
Items requiring future discussion or pending resolution.

## Additional Notes
Any other relevant information.

---
TRANSCRIPT:
{transcript}"""

BRAINSTORM_TEMPLATE = """Create a brainstorming session summary following this structure:

## Session Focus
What problem or topic was being explored?

## Ideas Generated
List all ideas mentioned, grouped by theme if applicable:

### Theme 1
- Idea 1
- Idea 2

### Theme 2
- Idea 3
- Idea 4

## Promising Directions
Which ideas showed the most potential or received the most discussion?

## Concerns/Constraints
Any limitations, risks, or concerns raised about the ideas.

## Next Steps
What was decided for further exploration?

## Raw Ideas
Unfiltered list of all concepts mentioned.

---
TRANSCRIPT:
{transcript}"""

INTERVIEW_TEMPLATE = """Create an interview summary following this structure:

## Interview Overview
Who was interviewed and for what purpose?

## Key Questions & Answers

### Q1: [Question]
**A:** [Summary of answer]

### Q2: [Question]
**A:** [Summary of answer]

(Continue for all significant Q&A exchanges)

## Candidate/Interviewee Assessment
Key strengths and areas of concern observed.

## Notable Quotes
Direct quotes that were particularly insightful or relevant.

## Follow-up Questions
Questions that should be explored in future conversations.

## Recommendation/Conclusion
Overall assessment or next steps.

---
TRANSCRIPT:
{transcript}"""

LECTURE_TEMPLATE = """Create lecture/presentation notes following this structure:

## Topic
Main subject of the lecture/presentation.

## Key Concepts

### Concept 1
- Definition/explanation
- Key points

### Concept 2
- Definition/explanation
- Key points

## Important Terms
| Term | Definition |
|------|------------|
| ... | ... |

## Examples/Case Studies
Examples used to illustrate concepts.

## Key Takeaways
The most important points to remember.

## Questions Raised
Questions asked during the session or topics for further study.

## Study Notes
Additional context helpful for understanding the material.

---
TRANSCRIPT:
{transcript}"""

CUSTOM_TEMPLATE = """Summarize the following transcript according to the user's specific instructions.

TRANSCRIPT:
{transcript}

USER INSTRUCTIONS:
{custom_prompt}"""


def get_prompt(
    prompt_type: str = "default",
    transcript: str = "",
    custom_instructions: str | None = None,
) -> tuple[str, str]:
    """
    Get system and user prompts for summarization.

    Args:
        prompt_type: Type of summary (default, quick, action_items, decisions,
                     meeting, brainstorm, interview, lecture, custom)
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
        "meeting": MEETING_TEMPLATE,
        "brainstorm": BRAINSTORM_TEMPLATE,
        "interview": INTERVIEW_TEMPLATE,
        "lecture": LECTURE_TEMPLATE,
        "custom": CUSTOM_TEMPLATE,
    }

    template = templates.get(prompt_type, USER_PROMPT_TEMPLATE)

    if prompt_type == "custom" and custom_instructions:
        user_prompt = template.format(transcript=transcript, custom_prompt=custom_instructions)
    else:
        user_prompt = template.format(transcript=transcript)
        if custom_instructions:
            user_prompt += f"\n\nAdditional instructions: {custom_instructions}"

    return SYSTEM_PROMPT, user_prompt


# Template metadata for frontend display
TEMPLATE_INFO = {
    "meeting": {
        "name": "Meeting",
        "description": "Structured meeting notes with decisions and action items",
    },
    "brainstorm": {
        "name": "Brainstorm",
        "description": "Capture ideas, themes, and promising directions",
    },
    "interview": {
        "name": "Interview",
        "description": "Q&A format with assessment and key quotes",
    },
    "lecture": {
        "name": "Lecture",
        "description": "Study notes with key concepts and terms",
    },
    "custom": {
        "name": "Custom",
        "description": "Provide your own summarization instructions",
    },
}
