# Sidekick Frontend Simplification Plan

## Overview

Simplify Sidekick from a complex session/meeting/mode UI to a minimal record-name-summarize-export workflow with Obsidian integration.

**Design Aesthetic**: Technical/minimal inspired by Berkeley Graphics - monospace typography, dark background, clean grid layout, no decorative elements.

---

## User Decisions

- **Past recordings**: Separate page (`recordings.html`)
- **Re-summarization**: New file each time (preserves history)
- **Template style**: Text only, minimal/professional
- **Obsidian path**: `/mnt/c/Users/ozzfa/Documents/Obsidian Vault` (root)

---

## Files to Modify

### Backend (Python)

| File | Change |
|------|--------|
| `src/summarization/prompts.py` | Add 5 new template prompts (Meeting, Brainstorm, Interview, Lecture, Custom) |
| `src/api/routes/export.py` | **NEW** - Obsidian export endpoint |
| `src/api/routes/sessions.py` | Add recordings list endpoint |
| `src/api/app.py` | Register export router |
| `config/settings.py` | Add `obsidian_vault_path` setting |

### Frontend (Web)

| File | Change |
|------|--------|
| `web/index.html` | Complete rewrite - minimal centered layout |
| `web/js/app.js` | Rewrite - simplified recording flow (~200 lines) |
| `web/js/websocket.js` | Simplify - remove mode/meeting commands |
| `web/js/audio.js` | Keep as-is (well-designed) |
| `web/css/styles.css` | Rewrite - Berkeley Mono aesthetic |
| `web/recordings.html` | **NEW** - Past recordings page |
| `web/js/recordings.js` | **NEW** - Recordings page logic |

---

## Implementation Phases

### Phase 1: Backend - Templates & Settings

**1.1 Add new prompts to `prompts.py`**

```python
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

BRAINSTORM_TEMPLATE = """Create a brainstorming session summary:

## Session Overview
Brief summary of the brainstorming topic and goals.

## Ideas Generated
- Idea 1: [description]
- Idea 2: [description]
...

## Themes Identified
Common threads or categories that emerged.

## Most Promising Directions
Top 3-5 ideas worth pursuing further.

## Questions Raised
Open questions that need answers.

## Next Steps
Concrete actions to explore the best ideas.

---
TRANSCRIPT:
{transcript}"""

INTERVIEW_TEMPLATE = """Create an interview summary:

## Interview Overview
- **Candidate/Interviewee**: [if mentioned]
- **Position/Topic**: [if mentioned]
- **Date**: [from transcript context]

## Key Questions & Responses
| Question | Response Summary |
|----------|-----------------|
| ... | ... |

## Strengths Noted
- Strength 1
- Strength 2

## Areas of Concern
- Concern 1
- Concern 2

## Notable Quotes
Direct quotes that stood out.

## Assessment/Recommendation
Overall evaluation if discussed.

---
TRANSCRIPT:
{transcript}"""

LECTURE_TEMPLATE = """Create a lecture/learning summary:

## Topic Overview
Main subject and learning objectives.

## Key Concepts
| Concept | Definition/Explanation |
|---------|----------------------|
| ... | ... |

## Main Points
1. Point 1 with supporting details
2. Point 2 with supporting details
...

## Examples Given
Concrete examples used to illustrate concepts.

## Questions Raised
Questions asked during the session or that arose.

## Key Takeaways
3-5 most important things to remember.

## Further Study
Topics to explore further.

---
TRANSCRIPT:
{transcript}"""
```

**1.2 Add Obsidian setting to `config/settings.py`**

```python
obsidian_vault_path: str = "/mnt/c/Users/ozzfa/Documents/Obsidian Vault"
```

### Phase 2: Backend - Export API

**2.1 Create `src/api/routes/export.py`**

```python
@router.post("/recordings/{session_id}/export-obsidian")
async def export_to_obsidian(
    session_id: str,
    request: ExportRequest,  # title, template, custom_prompt
    session_manager: SessionManager,
    summarization_manager: SummarizationManager,
):
    # 1. Get transcript from session
    # 2. Generate summary using template
    # 3. Build markdown with summary + collapsible transcript
    # 4. Write to vault: YYYY-MM-DD-HHMM - [title] [Template].md
    # 5. Return obsidian:// URI
```

**Markdown output format:**
```markdown
# 2026-02-07-1540 - Weekly Standup

**Template**: Meeting Summary
**Recorded**: 2026-02-07 15:40
**Duration**: 00:15:42

---

## Meeting Overview
...

## Action Items
...

---

<details>
<summary>Full Transcript</summary>

[00:00:12] First transcript segment...
[00:00:45] Second segment...

</details>
```

**2.2 Add recordings list endpoint to `sessions.py`**

```python
@router.get("/recordings")
async def list_recordings(limit: int = 50, offset: int = 0):
    # Return past sessions with: id, title, started_at, duration, has_summary
```

### Phase 3: Frontend - Main Page

**3.1 New `index.html` structure**

```
+----------------------------------------+
| SIDEKICK                    [HISTORY]  |
+----------------------------------------+
|                                        |
|                                        |
|            [ RECORD ]                  |  <- Large centered button
|                                        |
|         ═══════════════════            |  <- Audio visualizer (bars)
|                                        |
|             00:00:00                   |  <- Elapsed time
|                                        |
|                                        |
+----------------------------------------+
|  STATUS: READY              CONNECTED  |
+----------------------------------------+
```

**Design specs:**
- Background: `#0a0a0a` (near black)
- Font: `Berkeley Mono` or `JetBrains Mono` fallback to `monospace`
- Text: `#e0e0e0` (light grey)
- Accent: `#ffffff` for hover states
- Recording state: `#ff4444`
- All caps for labels
- Minimal borders, use spacing

**3.2 Simplified `app.js`**

```javascript
class SidekickApp {
    state = {
        isRecording: false,
        sessionId: null,
        elapsedSeconds: 0,
    };

    async toggleRecording() {
        if (this.state.isRecording) {
            await this.stopRecording();
            this.showNamingModal();
        } else {
            await this.startRecording();
        }
    }

    showNamingModal() { /* title input + template selector */ }

    async processRecording(title, template, customPrompt) {
        // 1. Show "PROCESSING..." state
        // 2. Call export endpoint
        // 3. Show confirmation modal
    }

    showConfirmation(result) {
        // "SAVED: filename.md"
        // [OPEN IN OBSIDIAN] [NEW RECORDING]
    }
}
```

**3.3 Naming Modal UI**

```
+----------------------------------------+
|  NAME RECORDING                    [X] |
+----------------------------------------+
|                                        |
|  TITLE                                 |
|  [____________________________]        |
|                                        |
|  TEMPLATE                              |
|  [MEETING    ] [BRAINSTORM ]           |
|  [INTERVIEW  ] [LECTURE    ]           |
|  [CUSTOM     ]                         |
|                                        |
|  (if CUSTOM selected)                  |
|  PROMPT                                |
|  [____________________________]        |
|  [____________________________]        |
|                                        |
|              [PROCESS]                 |
+----------------------------------------+
```

### Phase 4: Frontend - Recordings Page

**4.1 New `recordings.html`**

```
+----------------------------------------+
| <- BACK           RECORDINGS           |
+----------------------------------------+
|                                        |
| 2026-02-07-1540                        |
| Weekly Standup                         |
| DURATION: 00:15:42  SUMMARIES: 1       |
| [VIEW] [RESUMMARIZE] [EXPORT]          |
+----------------------------------------+
| 2026-02-06-0900                        |
| Product Planning                       |
| DURATION: 00:45:12  SUMMARIES: 2       |
| [VIEW] [RESUMMARIZE] [EXPORT]          |
+----------------------------------------+
| ...                                    |
+----------------------------------------+
```

**4.2 View modal** - Shows transcript + existing summaries
**4.3 Re-summarize** - Opens naming modal with pre-filled title

### Phase 5: Styling

**`styles.css` key properties:**

```css
:root {
    --bg: #0a0a0a;
    --bg-elevated: #141414;
    --text: #e0e0e0;
    --text-dim: #707070;
    --accent: #ffffff;
    --recording: #ff4444;
    --border: #2a2a2a;
}

* {
    font-family: 'Berkeley Mono', 'JetBrains Mono', monospace;
}

body {
    background: var(--bg);
    color: var(--text);
}

.btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 12px 24px;
}

.btn:hover {
    border-color: var(--accent);
    color: var(--accent);
}

.record-btn {
    width: 120px;
    height: 120px;
    border-radius: 50%;
    border: 2px solid var(--text);
    font-size: 14px;
}

.record-btn.recording {
    border-color: var(--recording);
    color: var(--recording);
    animation: pulse 1.5s infinite;
}
```

---

## Implementation Order

1. **Backend templates** - Add 5 prompts to `prompts.py`
2. **Backend settings** - Add `obsidian_vault_path` to settings
3. **Backend export API** - Create `export.py` with Obsidian endpoint
4. **Backend recordings list** - Add endpoint to `sessions.py`
5. **Frontend CSS** - Rewrite styles for new aesthetic
6. **Frontend main page** - Rewrite `index.html` and `app.js`
7. **Frontend modals** - Naming modal, confirmation modal
8. **Frontend recordings page** - New page with list/re-summarize

---

## Obsidian URI Format

```
obsidian://open?vault=Obsidian%20Vault&file=2026-02-07-1540%20-%20Weekly%20Standup%20%5BMeeting%5D
```

Note: On WSL, this URI opens in the Windows browser which can launch Obsidian. If issues occur, show file path as fallback.

---

## Verification

1. **Record flow**: Click record -> speak -> stop -> naming modal appears
2. **Naming**: Enter title, select template -> click Process
3. **Export**: File appears in Obsidian vault with correct name
4. **Confirmation**: Modal shows success, "Open in Obsidian" works
5. **History**: Recordings page shows past sessions
6. **Re-summarize**: Select old recording, choose different template, new file created

---

## Technical Notes

### Session/Meeting Model Mapping
- Keep existing database models
- Session = Recording (1:1 mapping)
- Meeting auto-created when recording starts
- Minimal backend changes required

### WebSocket Simplification
- Remove: mode commands, meeting start/stop
- Keep: session start/stop, audio streaming, transcription events

### Audio.js
- Keep unchanged - well-designed AudioCapture and AudioVisualizer classes
