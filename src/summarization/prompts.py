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

ONE_ON_ONE_TEMPLATE = """Create a 1-on-1 meeting summary following this structure:

## Overview
Brief context: who met and the general purpose.

## Discussion Topics
Summarize each topic discussed with key points.

## Feedback Given
- Feedback provided (positive or constructive)
- Specific examples or situations referenced

## Feedback Received
- Feedback received about your work, team, or processes
- Concerns or suggestions raised

## Goals & Development
- Career goals discussed
- Skills to develop
- Growth opportunities mentioned

## Action Items
| Item | Owner | Timeline |
|------|-------|----------|
| ... | ... | ... |

## Follow-up For Next 1-on-1
Topics or items to revisit in the next meeting.

IGNORE: Small talk, scheduling logistics, weather chat.

---
TRANSCRIPT:
{transcript}"""

STANDUP_TEMPLATE = """Create a brief standup/status meeting summary. Keep it concise.

## Attendees
List who participated (if identifiable).

## Updates By Person
For each person who gave an update:

### [Name]
- **Completed:** What they finished
- **Working On:** Current focus
- **Blockers:** Any impediments (if none, omit)

## Team Blockers
List any blockers that need escalation or cross-team coordination.

## Announcements
Any team-wide announcements or reminders shared.

IGNORE: Jokes, off-topic banter, detailed technical discussions (those belong in working sessions).
KEEP IT BRIEF: This should be scannable in 30 seconds.

---
TRANSCRIPT:
{transcript}"""

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
    "one_on_one": {
        "name": "1-on-1",
        "description": "Personal meetings - feedback, goals, development",
    },
    "standup": {
        "name": "Standup",
        "description": "Brief status updates - done, doing, blocked",
    },
    "strategic_review": {
        "name": "Strategic Review",
        "description": "Leadership meetings - reports, feedback, decisions, timelines",
    },
    "working_session": {
        "name": "Working Session",
        "description": "Technical work - high detail, decisions, open questions",
    },
    "meeting": {
        "name": "General Meeting",
        "description": "Standard meeting notes with decisions and action items",
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


def get_template_content(template_key: str) -> str:
    """Get the raw template content for display/editing in the UI."""
    templates = {
        "one_on_one": ONE_ON_ONE_TEMPLATE,
        "standup": STANDUP_TEMPLATE,
        "strategic_review": STRATEGIC_REVIEW_TEMPLATE,
        "working_session": WORKING_SESSION_TEMPLATE,
        "meeting": MEETING_TEMPLATE,
        "brainstorm": BRAINSTORM_TEMPLATE,
        "interview": INTERVIEW_TEMPLATE,
        "lecture": LECTURE_TEMPLATE,
        "custom": "",
    }
    content = templates.get(template_key, "")
    # Remove the transcript placeholder section for display
    if content:
        # Remove everything from "---\nTRANSCRIPT:" onwards
        parts = content.split("---\nTRANSCRIPT:")
        if len(parts) > 1:
            content = parts[0].strip()
    return content
