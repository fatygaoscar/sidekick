"""Prompt templates for summarization."""

SYSTEM_PROMPT = """You are an expert meeting summarizer. Your task is to create clear, actionable, and well-structured summaries from meeting transcripts.

Guidelines:
- Focus on key decisions, action items, and important discussions
- Use bullet points and markdown formatting for clarity
- Highlight any deadlines or assignments mentioned
- Keep the summary concise but comprehensive
- Pay special attention to sections marked as [IMPORTANT START]...[IMPORTANT END]
- If the transcript contains important markers, ensure those topics are prominently featured
- Correct obvious transcription errors (e.g., homophones, unclear words) based on context
- Use proper capitalization for names, companies, and technical terms
- If speakers are identifiable, attribute key points to them
- Be specific: include numbers, dates, and concrete details when mentioned
- Omit filler words, tangents, and off-topic chatter from the summary"""

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

ONE_ON_ONE_TEMPLATE = """Create 1-on-1 meeting notes with these sections:

## Summary
2-3 sentences: who met, main themes.

## Highlights & Recognition
Positive feedback, achievements, and wins mentioned.

## Feedback & Challenges
Constructive feedback, concerns, or growth areas discussed.

## Goals & Development
Career goals, development plans, or growth opportunities discussed.

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
Every concrete next step or commitment made.

If a section has nothing to note, write "None discussed."

Guidelines: Use actual names. Be specific about commitments made. Note who raised what when attributable."""

STANDUP_TEMPLATE = """Create a standup summary. Keep it brief and scannable.

## Updates
For each person who spoke:

### [Name]
- **Done:** What they completed
- **Doing:** What they're working on now
- **Blocked:** Any impediments (omit if none)

## Team Blockers
Issues needing cross-team coordination or escalation. "None" if none.

## Action Items
| Owner | Action |
|-------|--------|
Follow-ups, decisions needed, or escalations from this standup. "None" if none.

Keep it brief — this should be scannable in 30 seconds."""

STRATEGIC_REVIEW_TEMPLATE = """Create an adaptive summary for this leadership/strategic meeting. First, analyze what was actually discussed, then include ONLY the relevant sections below. Do not include empty sections.

## Meeting Context
Brief 2-3 sentence overview: who attended, what was reviewed, and the primary outcome.

## Report/Dashboard Review
(Include if reports, dashboards, or metrics were reviewed)
- Key metrics discussed
- Trends or anomalies noted
- Data quality issues raised

## Feedback & Discussion
(Include if stakeholders provided feedback)
- Feedback from each stakeholder (attribute by name/role if possible)
- Concerns raised
- Suggestions proposed

## Decisions Made
(Include if decisions were reached)
For each decision:
- **Decision:** What was decided
- **Rationale:** Why (if discussed)
- **Owner:** Who is responsible

## Strategy & Direction Changes
(Include if strategy or approach was adjusted)
- What changed from the previous approach
- Why the pivot was made
- Impact on current work

## Timeline & Milestones
(Include if dates, deadlines, or schedules were discussed)
| Milestone | Date | Owner | Notes |
|-----------|------|-------|-------|
| ... | ... | ... | ... |

## Resource & Prioritization
(Include if resourcing, priorities, or trade-offs were discussed)
- Priority changes
- Resource allocation decisions
- What's being deprioritized

## Action Items
| Task | Owner | Due Date |
|------|-------|----------|
| ... | ... | ... |

## Open Items
Questions or topics requiring further discussion or stakeholder input.

## Next Steps
What happens next and when to reconvene.

IGNORE: Small talk, scheduling logistics, off-topic tangents.
BE SPECIFIC: Include actual numbers, dates, names, and concrete details.

---
TRANSCRIPT:
{transcript}"""

WORKING_SESSION_TEMPLATE = """Create a detailed technical working session summary. This template prioritizes PRESERVING DETAIL because many micro-decisions are made during technical work.

## Session Focus
What problem, project, or system was being worked on?

## Participants
Who was involved and their roles (if identifiable).

## Work Completed
Describe what was actually built, fixed, or changed during the session:
- Changes made (be specific about tables, fields, queries, models)
- Problems solved
- Code or queries written (summarize logic, include key snippets if mentioned)

## Technical Decisions
For EACH decision made (even small ones), document:

### Decision: [Brief title]
- **What:** What was decided
- **Why:** The reasoning or trade-off considered
- **Alternatives Rejected:** Other options discussed and why they weren't chosen
- **Impact:** What this affects

(Repeat for each decision)

## Data Model Changes
(Include if data models, schemas, or structures were modified)
- Tables/entities affected
- Fields added, removed, or modified
- Relationships changed
- Migration notes

## SQL / Query Notes
(Include if SQL or queries were discussed)
- Query logic discussed
- Performance considerations
- Key joins or filters

## Issues Discovered
Problems found during the session that need attention:
- Bug or data issue
- Root cause (if identified)
- Proposed fix

## Outstanding Questions - Needs Consensus
**These items require input from other departments or leadership (VP/Director/Manager) before proceeding:**

| Question | Context | Who Needs to Decide | Urgency |
|----------|---------|---------------------|---------|
| ... | ... | ... | ... |

## Outstanding Questions - Technical
Technical questions to research or resolve within the team:
- Question and current thinking

## Next Session Agenda
What to tackle next time:
- Carry-over items
- Next priorities

## Parking Lot
Ideas or tangents mentioned but not pursued - saved for later consideration.

DO NOT SUMMARIZE AWAY DETAIL: This is a technical log. Preserve specifics.
ATTRIBUTE DECISIONS: Note who proposed or decided something when identifiable.
CAPTURE THE "WHY": The reasoning behind decisions is as important as the decision itself.

---
TRANSCRIPT:
{transcript}"""

MEETING_TEMPLATE = """Create structured meeting notes with these sections:

## Summary
2-3 sentences: purpose of the meeting and key outcome.

## Key Decisions
Bullet list. For each: what was decided and by whom (if stated). If none, write "None."

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
Every concrete next step. Use actual names. If owner not named, use "TBD". If no due date, leave blank. If none, write "None."

## Discussion Notes
Group by topic. For each topic: **[Topic]**: 2-4 sentences capturing key points, positions, and outcomes.

Guidelines: Use actual names (not "a participant"). Quote specific commitments. Skip pleasantries and off-topic tangents. Include numbers, dates, and concrete details."""

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


# Template metadata for frontend display
TEMPLATE_INFO = {
    "meeting": {
        "name": "General Meeting",
        "description": "Standard meeting notes with decisions and action items",
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
