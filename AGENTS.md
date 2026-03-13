# Sidekick Agent Handoff

Use this as the short operating contract for work in this repo.

## System Layout

```text
Browser
  -> chunk upload + optional WS preview
  -> FastAPI (`src/api/routes/*`)
  -> session/audio state (`data/sidekick.db`, `data/audio/`)
  -> WhisperX ASR/alignment (`src/transcription/whisperx_local.py`)
  -> pyannote diarization + shared speaker attribution
  -> workspace review (`web/js/recording-workspace.js`)
  -> cohesive summary + Obsidian export
```

Key files:
- `src/api/routes/sessions.py`: recording/workspace/speaker APIs
- `src/api/routes/export.py`: async transcription/export jobs
- `src/transcription/whisperx_local.py`: authoritative local transcription
- `src/transcription/speaker_attribution.py`: shared speaker mapping logic
- `src/summarization/cohesive.py`: active summary path
- `web/js/recording-workspace.js`: shared workspace controller

## Runtime Facts

- Local authoritative stack: WhisperX + pyannote community-1 + Ollama.
- Initial transcription and diarization-only speaker reruns must stay behaviorally aligned through `src/transcription/speaker_attribution.py`.
- Transcript versions are authoritative for summary drafts/saved summaries.
- Speaker review is manual-first. Respect `clip_available`; weak legacy clusters should show `No Preview`.
- Speaker preview playback uses Web Audio plus `web/js/network.js`, not raw `new Audio().src`.
- If frontend changes do not show up, bump the `?v=` asset query string in `web/index.html` and `web/recordings.html`.
- Do not "fix" speaker rerun quality by loosening the gate first; check attribution drift before changing thresholds.

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items  
2. **Verify Plan**: Check in before starting implementation  
3. **Track Progress**: Mark items complete as you go  
4. **Explain Changes**: High-level summary at each step  
5. **Document Results**: Add review section to `tasks/todo.md`  
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections  

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
