"""Prompt templates for summarization."""

SYSTEM_PROMPT = """You are an expert meeting summarizer optimized for Obsidian markdown. Your task is to create clear, highly scannable, and well-structured summaries.

Formatting Guidelines for Obsidian:
- **Prioritize Bullet Points:** Avoid long paragraphs. Break down discussions into concise bullet points using `-`.
- **Indentation & Hierarchy:** Use nested bullet points (indented by 2 spaces) to show supporting details or sub-topics.
- **Spacing:** ALWAYS include a blank line between a header and the content below it. Include a blank line between different topics or sections.
- **Tables:** Use standard Markdown tables for Action Items and structured data.
- **Typography:** Use bolding for emphasis on key terms, names, or decisions within bullets.
- **Cleanliness:** Correct transcription errors, omit filler, and ensure proper capitalization.
- **Factual Accuracy:** Focus on decisions, action items, and specific details (dates, numbers, names).
- **Markers:** Pay special attention to sections marked [IMPORTANT START]...[IMPORTANT END]."""

USER_PROMPT_TEMPLATE = """Please summarize the following meeting transcript. Use clear Obsidian-friendly markdown with plenty of bullet points and proper spacing.

TRANSCRIPT:
{transcript}

Please provide:
1. **Executive Summary**: 2-3 concise sentences.
2. **Key Discussion Points**: Use nested bullet points to capture topics and details.
3. **Decisions Made**: A clear list of what was decided.
4. **Action Items**: A markdown table with columns | Owner | Action | Due |.
5. **Important Highlights**: Featured points from any [IMPORTANT] marked sections.
6. **Next Steps**: Immediate follow-ups."""

QUICK_SUMMARY_TEMPLATE = """Summarize this meeting transcript in 5-8 concise bullet points. Use nested bullets for supporting details. Use '-' for all bullets.

{transcript}"""

ACTION_ITEMS_TEMPLATE = """Extract all action items into a clean markdown table with columns | Owner | Action | Due |. If owner or due date is unknown, use "TBD" or leave blank.

TRANSCRIPT:
{transcript}"""

DECISION_LOG_TEMPLATE = """Extract all decisions made during this meeting. Format as a bulleted list where each decision is followed by a brief nested bullet explaining the rationale.

TRANSCRIPT:
{transcript}"""

# New structured templates for Obsidian export

ONE_ON_ONE_TEMPLATE = """Create 1-on-1 meeting notes optimized for Obsidian.

## Summary
- 2-3 sentences: who met and the main themes.

## Highlights & Recognition
- Use bullet points for positive feedback, achievements, and wins mentioned.

## Feedback & Challenges
- Use bullet points for constructive feedback, concerns, or growth areas.

## Goals & Development
- Use bullet points for career goals and development plans.

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
Every concrete next step.

If a section has nothing to note, write "None discussed."
Use actual names. Use nested bullets for detail."""

STANDUP_TEMPLATE = """Create a concise standup summary.

## Updates
For each person:
- **[Name]**
  - **Done:** Bulleted list of completions.
  - **Doing:** Bulleted list of current work.
  - **Blocked:** Any impediments (omit if none).

## Team Blockers
- List any issues needing coordination.

## Action Items
| Owner | Action |
|-------|--------|
Follow-ups and immediate needs.

Keep it very brief and scannable."""

STRATEGIC_REVIEW_TEMPLATE = """Create a strategic meeting summary for Obsidian. Include ONLY relevant sections.

## Meeting Context
- Brief overview of participants and primary outcomes.

## Report/Dashboard Review
- Key metrics and trends discussed (use bullets).

## Feedback & Discussion
- Stakeholder feedback (attribute by name).
- Use nested bullets for specific suggestions/concerns.

## Decisions Made
- **[Decision Title]**: Brief description.
  - **Rationale**: Why it was decided.
  - **Owner**: Who is responsible.

## Strategy & Direction Changes
- Bulleted list of shifts in approach and their impact.

## Timeline & Milestones
| Milestone | Date | Owner | Notes |
|-----------|------|-------|-------|

## Action Items
| Task | Owner | Due Date |
|------|-------|----------|

## Next Steps
- What happens next and when to reconvene.

TRANSCRIPT:
{transcript}"""

WORKING_SESSION_TEMPLATE = """Create a technical working session summary for Obsidian. Preserve detail using nested bullets.

## Session Focus
- What problem or system was being worked on?

## Work Completed
- Detailed list of what was built, fixed, or changed.
- Use sub-bullets for technical specifics (fields, logic, etc.).

## Technical Decisions
- **Decision Title**
  - **What**: The decision.
  - **Why**: Reasoning/trade-offs.
  - **Alternatives**: Why other options were rejected.

## Data Model & Logic
- Schema changes, SQL notes, or architectural shifts.

## Issues Discovered
- What project or report it concerns, problems found, root causes, and proposed fixes.

## Outstanding Questions
| Question | Context | Owner | Urgency |
|----------|---------|-------|---------|

## Next Session Agenda
- Carry-over items and next priorities.

TRANSCRIPT:
{transcript}"""

MEETING_TEMPLATE = """Create structured meeting notes for Obsidian. Use nested bullets for discussion points.

## Summary
- 2-3 sentences on purpose and outcome.

## Key Decisions
- List of decisions. Use nested bullets for context. Write "None" if none.

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
Use actual names. If owner not named, use "TBD".

## Discussion Notes
Group by topic using ### headers.
### [Topic Name]
- Key points and positions.
- Use nested bullets for supporting details or sub-topics.
- Be specific about who said what.

Guidelines: Avoid paragraphs; use bullets. Use actual names. Include dates and numbers.

TRANSCRIPT:
{transcript}"""

BRAINSTORM_TEMPLATE = """Create a brainstorming session summary for Obsidian.

## Session Focus
- Goal of the exploration.

## Ideas Generated
Grouped by theme:
### [Theme]
- Idea 1
  - Detail/Variant A
  - Detail/Variant B
- Idea 2

## Promising Directions
- Ideas with the most potential or consensus.

## Next Steps
- Decision for further exploration.

TRANSCRIPT:
{transcript}"""

INTERVIEW_TEMPLATE = """Create an interview summary for Obsidian.

## Interview Overview
- Purpose and participants.

## Key Questions & Answers
- **[Question]**
  - **A**: [Summary of answer]
  - Use sub-bullets for specific examples or details provided.

## Candidate Assessment
- **Strengths**: Bulleted list.
- **Concerns**: Bulleted list.

## Notable Quotes
- "> [Direct quote]"

## Next Steps
- Recommended follow-up actions.

TRANSCRIPT:
{transcript}"""

LECTURE_TEMPLATE = """Create structured lecture/presentation notes for Obsidian.

## Topic
- Main subject.

## Key Concepts
### [Concept Name]
- Definition and explanation.
- Key points and sub-details (indented).

## Important Terms
| Term | Definition |
|------|------------|

## Key Takeaways
- Most important points to remember.

## Questions & Further Study
- Unanswered questions or topics for exploration.

TRANSCRIPT:
{transcript}"""

CUSTOM_TEMPLATE = """Summarize the following transcript according to the user's specific instructions.

TRANSCRIPT:
{transcript}

USER INSTRUCTIONS:
{custom_prompt}"""

# Default prompt shown in the UI when "Custom" template is selected
CUSTOM_DEFAULT_PROMPT = """Review the transcript and extract only the parts that are relevant to [insert audience or artifact here, e.g. "Account Manager dashboard", "Leadership report", "Product X rollout", "Incentive design"].

Ignore casual conversation, side topics, and implementation details unless they directly affect the design, scope, audience, timing, or guardrails.

For the relevant sections, produce:

Where the topic is discussed (what moments in the conversation matter, summarized concisely — not verbatim quotes unless critical)

Explicit decisions and implied direction (what was decided, even if not stated formally)

Non-negotiable constraints / guardrails

Audience definition and access expectations

Timeline and sequencing expectations

Then synthesize that into:

A clear, forward-looking summary of what the team wants to build or deliver

Written as if it will be handed to someone who was not in the meeting

Do not restate the transcript.
Do not speculate beyond what the conversation supports.
Optimize for clarity, alignment, and reusability."""


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
        "one_on_one": ONE_ON_ONE_TEMPLATE,
        "standup": STANDUP_TEMPLATE,
        "strategic_review": STRATEGIC_REVIEW_TEMPLATE,
        "working_session": WORKING_SESSION_TEMPLATE,
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


# All template metadata, including legacy templates kept for compatibility.
TEMPLATE_INFO = {
    "meeting": {
        "name": "General Meeting",
        "description": "Standard meeting notes with decisions and action items",
    },
    "strategic_review": {
        "name": "Strategic Review",
        "description": "Strategic alignment, metrics review, decisions, and next steps",
    },
    "one_on_one": {
        "name": "1-on-1",
        "description": "Personal meetings - feedback, goals, development",
    },
    "standup": {
        "name": "Standup",
        "description": "Brief status updates - done, doing, blocked",
    },
    "working_session": {
        "name": "Working Session",
        "description": "Technical work - high detail, decisions, open questions",
    },
    "custom": {
        "name": "Custom",
        "description": "Targeted extraction for a specific audience or artifact",
    },
}


# Templates intentionally exposed in the app.
PUBLIC_TEMPLATE_KEYS = (
    "meeting",
    "strategic_review",
    "working_session",
    "custom",
)


def get_template_content(template_key: str) -> str:
    """Get the raw template content for display/editing in the UI."""
    templates = {
        "meeting": MEETING_TEMPLATE,
        "one_on_one": ONE_ON_ONE_TEMPLATE,
        "standup": STANDUP_TEMPLATE,
        "working_session": WORKING_SESSION_TEMPLATE,
        "custom": CUSTOM_DEFAULT_PROMPT,
        # Legacy templates kept for backward compatibility with existing recordings
        "strategic_review": STRATEGIC_REVIEW_TEMPLATE,
        "brainstorm": BRAINSTORM_TEMPLATE,
        "interview": INTERVIEW_TEMPLATE,
        "lecture": LECTURE_TEMPLATE,
    }
    content = templates.get(template_key, "")
    # Remove the transcript placeholder section for display (legacy templates only)
    if content:
        parts = content.split("---\nTRANSCRIPT:")
        if len(parts) > 1:
            content = parts[0].strip()
    return content
