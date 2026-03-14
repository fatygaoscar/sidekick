"""Prompt templates for summarization."""

DEFAULT_TEMPLATE_KEY = "meeting"

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

TOPIC_SEGMENTED_PASS1_SYSTEM_PROMPT = """You extract structured facts from meeting transcripts.

Rules:
- Extract only information explicitly stated in the transcript.
- Do not infer owners, dates, or decisions.
- If ownership is unclear, return null.
- Suggestions, questions, and vague intentions are not action items.
- Return valid JSON matching the schema exactly."""

TOPIC_SEGMENTED_PASS1_USER_PROMPT_TEMPLATE = """Participants:
{participants}

Topic Segment:
{topic_label}

Transcript:
{topic_text}

Return JSON matching this schema:

{{
  "summary": ["string"],
  "decisions": [
    {{
      "text": "string",
      "owner": "string|null",
      "timestamp": "MM:SS|null"
    }}
  ],
  "action_items": [
    {{
      "text": "string",
      "owner": "string|null",
      "due_date": "string|null",
      "timestamp": "MM:SS|null"
    }}
  ],
  "milestones": [
    {{
      "text": "string",
      "date": "string|null",
      "owner": "string|null"
    }}
  ],
  "unresolved_questions": [
    {{
      "text": "string",
      "owner": "string|null"
    }}
  ]
}}"""

TOPIC_SEGMENTED_LABEL_SYSTEM_PROMPT = """You generate short human-readable labels for one meeting topic chunk.

Rules:
- Return only the label text.
- Use 2-6 words.
- Prefer concrete business, product, process, or problem phrases.
- Do not use generic labels like "Discussion", "General Update", or "Topic 1".
- Do not include speaker names unless central to the topic.
- Do not write a sentence or summary."""

TOPIC_SEGMENTED_LABEL_USER_PROMPT_TEMPLATE = """Participants:
{participants}

Topic Time Range:
{topic_time_range}

Keyword Hints:
{keyword_hints}

Opening Turns:
{opening_turns}

Closing Turns:
{closing_turns}

Examples of good labels:
- Inventory Planner Rollout
- Kia Genesis Data Issues
- Dashboard Filter Design
- Maintenance Care Payout Cadence

Return one short label only."""

TOPIC_SEGMENTED_POST_EXTRACT_LABEL_SYSTEM_PROMPT = """You generate short human-readable business topic labels from extracted meeting content.

Rules:
- Return only the label text.
- Use 2-7 words.
- Base the label on the actual business issue, decision, workstream, or unresolved question.
- Prefer labels that a person would recognize as a real meeting topic.
- Do not use generic labels like "Discussion", "General Update", or "Topic 1".
- Do not restate transcript filler or social chatter.
- Do not write a sentence or summary."""

TOPIC_SEGMENTED_POST_EXTRACT_LABEL_USER_PROMPT_TEMPLATE = """Participants:
{participants}

Topic Time Range:
{topic_time_range}

Summary:
{summary_lines}

Decisions:
{decision_lines}

Action Items:
{action_lines}

Unresolved Questions:
{question_lines}

Milestones:
{milestone_lines}

Examples of good labels:
- Kia/Genesis Maintenance Reporting
- Dealer Dashboard Filter Readiness
- Inventory Planner Rollout Timeline
- Maintenance Care Payout Cadence

Return one short label only."""

TOPIC_SEGMENTED_TOPIC_ID_SYSTEM_PROMPT = """You identify real business topics in meeting transcripts.

Rules:
- Ignore filler, greetings, coffee talk, weather, weekend chatter, jokes, and social banter.
- Create topics only for sustained business discussion.
- Separate adjacent but distinct business issues, even when they happen back-to-back.
- Do not create topics for conversational style or incidental wording.
- Use topic names that reflect the actual business issue, workstream, policy question, reporting problem, pricing issue, or goal discussion.
- Do not invent owners, dates, or decisions.
- Return valid JSON only."""

TOPIC_SEGMENTED_TOPIC_ID_USER_PROMPT_TEMPLATE = """Participants:
{participants}

Max Topics:
{max_topics}

Transcript:
{transcript}

Return JSON matching this schema:

{{
  "topics": [
    {{
      "topic_id": "topic_001",
      "topic_name": "string",
      "start_timestamp": "MM:SS|null",
      "end_timestamp": "MM:SS|null",
      "why_this_is_a_topic": "string"
    }}
  ],
  "warnings": ["string"]
}}"""

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

MEETING_TEMPLATE = """Review the transcript and extract only the information that is relevant to the meeting's outcome, next steps, important problems, key themes, or unresolved questions.

Ignore filler conversation, greetings, jokes, repeated statements, and side comments.

Only include information that a human would reasonably write down in useful meeting notes. If a point would not materially help someone understand what mattered in the meeting, omit it.

Do not present proposals, suggestions, or unresolved questions as decisions.
Do not guess action item owners. If ownership is unclear, use "TBD".
Preserve exact names, dates, numbers, and system names when they materially matter.

Produce draft notes using this structure:

## Summary
- 2-3 bullets preferred; use 4 only if clearly necessary.
- Summarize only the most important outcomes and takeaways.
- Prefer decisions, major problems, important constraints, and next-step direction.
- Do not use the Summary to restate every section.
- At most one Summary bullet should primarily describe actions.

## Key Decisions
- Include only decisions that were explicitly agreed, confirmed, or clearly adopted.
- If there were no clear decisions, write: None.

## Key Discussion Points
- 3-5 bullets preferred; use 6-7 only if the meeting truly covered multiple distinct important topics.
- Each bullet should summarize one important topic, tradeoff, problem, or unresolved question.
- Prefer one strong bullet per topic rather than many small bullets.
- Merge related remarks into a single strong bullet when they support the same topic.
- Do not create multiple bullets for the same topic unless the sub-points are materially different.
- Focus on points that explain decisions, next steps, important problems, constraints, or unresolved questions.
- Include attribution only when it materially changes the meaning of the point.

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
- Only include concrete follow-up tasks that the meeting clearly established.
- An action item must be specific enough that someone could actually do it and must materially affect what happens after the meeting.
- Use actual names when explicit. If no owner was assigned, use "TBD" only when the task is clearly real.

Valid action patterns:
- "[Name] will..."
- "We will..."
- "[Name] to..." when clearly used as an assignment
- "Follow up on...", "Send...", "Validate...", "Document...", "Investigate...", "Prepare...", "Update...", "Schedule..."
- "Let's..." only when it clearly indicates a committed next step

Do NOT create action items for:
- suggestions
- recommendations
- speculative ideas
- questions
- hypothetical next steps
- general recommendations
- opinions about what should happen unless they were clearly assigned and adopted

If a task is vague, speculative, or not clearly committed, omit it rather than using "TBD".

## Open Questions / Unresolved Items
- Include only questions or uncertainties that materially affect decisions or next steps.
- Do not include minor unanswered curiosities.

Important rules:
- Focus on relevance rather than completeness.
- Consider the entire meeting, not just the ending.
- Keep the notes concise and useful.

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

AUTO_TEMPLATE = MEETING_TEMPLATE

LEGACY_TEMPLATE_KEY_MAP = {
    "auto": DEFAULT_TEMPLATE_KEY,
}


def normalize_template_key(template_key: str | None) -> str:
    """Map legacy or empty template keys to the active user-facing template."""
    key = (template_key or "").strip()
    if not key:
        return DEFAULT_TEMPLATE_KEY
    return LEGACY_TEMPLATE_KEY_MAP.get(key, key)


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


def get_topic_segmented_pass1_prompts(
    *,
    participants: list[str],
    topic_label: str,
    topic_text: str,
) -> tuple[str, str]:
    """Render the dedicated Pass 1 extraction prompts for topic_segmented_v1."""
    participant_text = ", ".join(participants) if participants else "(none)"
    user_prompt = TOPIC_SEGMENTED_PASS1_USER_PROMPT_TEMPLATE.format(
        participants=participant_text,
        topic_label=(topic_label.strip() or "General discussion"),
        topic_text=(topic_text.strip() or "(none)"),
    )
    return TOPIC_SEGMENTED_PASS1_SYSTEM_PROMPT, user_prompt


def get_topic_segmented_label_prompt(
    *,
    participants: list[str],
    topic_time_range: str,
    keyword_hints: list[str],
    opening_turns: list[str],
    closing_turns: list[str],
) -> tuple[str, str]:
    """Render the dedicated label-generation prompt for topic_segmented_v1."""
    participant_text = ", ".join(participants) if participants else "(none)"
    keyword_text = ", ".join(keyword_hints) if keyword_hints else "(none)"
    opening_text = "\n".join(opening_turns) if opening_turns else "(none)"
    closing_text = "\n".join(closing_turns) if closing_turns else "(none)"
    user_prompt = TOPIC_SEGMENTED_LABEL_USER_PROMPT_TEMPLATE.format(
        participants=participant_text,
        topic_time_range=(topic_time_range.strip() or "(unknown)"),
        keyword_hints=keyword_text,
        opening_turns=opening_text,
        closing_turns=closing_text,
    )
    return TOPIC_SEGMENTED_LABEL_SYSTEM_PROMPT, user_prompt


def get_topic_segmented_post_extract_label_prompt(
    *,
    participants: list[str],
    topic_time_range: str,
    summary_lines: list[str],
    decision_lines: list[str],
    action_lines: list[str],
    question_lines: list[str],
    milestone_lines: list[str],
) -> tuple[str, str]:
    """Render the post-extraction label-generation prompt for topic_segmented_v1."""
    participant_text = ", ".join(participants) if participants else "(none)"
    user_prompt = TOPIC_SEGMENTED_POST_EXTRACT_LABEL_USER_PROMPT_TEMPLATE.format(
        participants=participant_text,
        topic_time_range=(topic_time_range.strip() or "(unknown)"),
        summary_lines="\n".join(f"- {line}" for line in summary_lines) if summary_lines else "- (none)",
        decision_lines="\n".join(f"- {line}" for line in decision_lines) if decision_lines else "- (none)",
        action_lines="\n".join(f"- {line}" for line in action_lines) if action_lines else "- (none)",
        question_lines="\n".join(f"- {line}" for line in question_lines) if question_lines else "- (none)",
        milestone_lines="\n".join(f"- {line}" for line in milestone_lines) if milestone_lines else "- (none)",
    )
    return TOPIC_SEGMENTED_POST_EXTRACT_LABEL_SYSTEM_PROMPT, user_prompt


def get_topic_segmented_topic_identification_prompt(
    *,
    participants: list[str],
    transcript: str,
    max_topics: int,
) -> tuple[str, str]:
    """Render the transcript-level topic-identification prompt for topic_segmented_v1."""
    participant_text = ", ".join(participants) if participants else "(none)"
    user_prompt = TOPIC_SEGMENTED_TOPIC_ID_USER_PROMPT_TEMPLATE.format(
        participants=participant_text,
        max_topics=max_topics,
        transcript=(transcript.strip() or "(none)"),
    )
    return TOPIC_SEGMENTED_TOPIC_ID_SYSTEM_PROMPT, user_prompt


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
        "auto": AUTO_TEMPLATE,
        "brainstorm": BRAINSTORM_TEMPLATE,
        "interview": INTERVIEW_TEMPLATE,
        "lecture": LECTURE_TEMPLATE,
        "custom": CUSTOM_TEMPLATE,
    }

    normalized_prompt_type = normalize_template_key(prompt_type)
    template = templates.get(normalized_prompt_type, USER_PROMPT_TEMPLATE)

    if normalized_prompt_type == "custom" and custom_instructions:
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
        "description": "Concise, relevant meeting notes with decisions, actions, and key discussion points",
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
        "auto": MEETING_TEMPLATE,
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
    content = templates.get(normalize_template_key(template_key), "")
    # Remove the transcript placeholder section for display (legacy templates only)
    if content:
        parts = content.split("---\nTRANSCRIPT:")
        if len(parts) > 1:
            content = parts[0].strip()
    return content
