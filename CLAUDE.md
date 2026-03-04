# Sidekick Agent Handoff

## Current Runtime

```bash
# Managed lifecycle
./start.sh
./start.sh --ngrok
./start.sh --cloudflare
./restart.sh
./stop.sh
./status.sh
./debug.sh
```

Manual run:

```bash
source venv/bin/activate
python -m src.main
```

## Product Flow

1. Record audio in browser
2. Stop recording
3. Name recording, select template, optionally enter attendees + edit prompt
4. Process/export to Obsidian markdown with real-time progress

## Architecture: Two Transcription Pipelines

### 1) Live Preview Pipeline (optional UX)

`microphone stream -> websocket chunks -> live preview text`

- `src/api/routes/websocket.py`
- Controlled by `.env`: `LIVE_TRANSCRIPTION_PREVIEW=true|false`
- Used only for in-session preview — not the source of truth for export

### 2) Export Pipeline (authoritative)

`saved audio file -> transcription -> diarization -> cohesive summary -> markdown`

- `src/api/routes/export.py`
- Uses saved session audio from `data/audio/`
- Async job-based with real-time progress
- Rebuilds transcript segments at export time from authoritative audio

### 3) Summarization

`transcript -> optional speaker pre-pass -> two-pass cohesive summary -> markdown`

- All templates use `generate_cohesive_summary()` in `src/summarization/cohesive.py`
- **Speaker pre-pass**: if attendees are provided, one dedicated LLM call resolves `SPEAKER_XX` labels to real names before any summarization pass
- **Pass 1 (draft)**: template style contract + attendees context → draft following exact section structure
- **Pass 2 (polish)**: editorial rewrite preserving all `##` headers from draft
- **Retry pass**: triggered if artifacts or repeated sentences detected in pass 2 output
- Context budget: full transcript when it fits; compressed evidence pack fallback for long meetings; chunked extraction for very long meetings

Pipeline modules (`src/summarization/pipeline/`) remain in codebase but are **not invoked from export**.

## Summary Templates

Defined in `src/summarization/prompts.py`:

| Template | Description |
|----------|-------------|
| **General Meeting** | Summary, Key Decisions, Action Items table, Discussion Notes (default) |
| **1-on-1** | Summary, Highlights, Feedback, Goals, Action Items table |
| **Standup** | Per-person Done/Doing/Blocked, Team Blockers, Action Items table |
| **Working Session** | High-detail technical log — decisions, SQL notes, open questions |
| **Custom** | User-provided prompt |

UI template chooser order:
1. `meeting` (General Meeting)
2. `one_on_one`
3. `standup`
4. `working_session`
5. `custom`

Legacy templates (constants kept for backward compat, not in UI): `strategic_review`, `brainstorm`, `interview`, `lecture`

## UX Conventions

- Keep one primary action per step; avoid duplicate entry points.
- Recording list cards: `View` and `Delete` only.
- Re-summarize/export initiated from the recording view modal, not cards.
- **Unified Modals:** Both History "View" and post-recording "Review" modals now support identical features: AI refinement, manual editing, and undo history.
- **View Modal Title:** Shows plain meeting title (no date prefix). Date/time is in the Details section.
- **Details Section:** Two-column grid (label + value) — Template, Recorded, Exported, Length, Processing Time. Template omitted for old summaries (null). Updates when switching summary versions.
- **Summary label:** "Summary Version" (not "Summary") in the view modal.
- **Metadata Visibility:** Details section mirrors Obsidian markdown header fields. Template is stored per-summary in DB (`summaries.template`).
- **Obsidian Save:** "Save to Obsidian" button available in history view to create versioned copies or re-export.
- **Audio player** is positioned at the bottom of the modal (below summary, above Downloads).
- Download affordances in the view modal (`Download Audio`, `Download Transcript`).
- Recording list cards show plain title (no date prefix) — date is shown separately on the card.
- Template chooser shows 5 templates in the order above.
- Keep `General Meeting` as default unless explicit product changes requested.
- Attendees field (optional) in both export modals — used for speaker name resolution.

## Key Config (Current)

```
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16

SUMMARIZATION_BACKEND=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_THINK=false
OLLAMA_NUM_GPU=99
OLLAMA_CONTEXT_LENGTH=32768
SUMMARIZATION_TIMEOUT_SECONDS=300

HF_TOKEN=<huggingface_read_token>
DIARIZATION_ENABLED=true

OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault
```

**Model notes**:
- `qwen3:8b` = 5.2GB, 100% VRAM on RTX 5070 Ti (16GB). ~30s for 25K char recordings.
- **qwen3 vs qwen3.5**: `qwen3:8b` properly respects `OLLAMA_THINK=false` (~300-400 output tokens). `qwen3.5` models always generate 3000-5000 think tokens regardless of the setting.
- `OLLAMA_NUM_GPU=99` forces all layers to GPU (bypasses Ollama's conservative auto-estimate which offloads ~3 layers to CPU). Doubles tok/s.
- `OLLAMA_CONTEXT_LENGTH=32768` fits model + KV cache 100% in 16GB VRAM. Supports ~2.5+ hours of speech.
- `temperature=0.3` is set in all Ollama call options for more consistent, factual output.
- Larger models (14b+) or larger context (40k+) cause CPU spillover (RAM spill = 40%+ CPU usage)
- Ollama runs on **Windows host**; Sidekick in **WSL** with mirrored networking → `127.0.0.1:11434` works directly

## Output Format (Current)

- Filename: `YYYY-MM-DD-HHMM - [Title] [Template].md`
- **Versioning:** Exports append ` (v2)`, ` (v3)`, etc., to filename if summary already exists for that meeting.
- Metadata block: Template, Recorded date, Exported date, Duration, Processing Time
- Summary body follows template section structure
- **Markdown Standard:** Strict bullet-point-first structure with nested indentation (2 spaces) and blank lines between sections for Obsidian scannability.
- Collapsible full transcript (with `SPEAKER_XX:` or resolved real names)

## Data Locations

- DB: `data/sidekick.db` (Schema: `summaries` table has `processing_duration_seconds`)
- Audio: `data/audio/{session_id}.webm`
- Chunk storage: `data/audio/chunks/{session_id}/{client_id}/` (temporary)
- Sidekick logs/PID: `data/sidekick.log`, `data/sidekick.pid`

## Important Endpoints

- `GET /` main UI
- `GET /recordings` history UI
- `GET /api/templates` list templates with prompts
- `GET /api/recordings` list recordings
- `GET /api/recordings/{id}` recording details (includes latest `summary` + metadata)
- `PATCH /api/recordings/{id}/title` rename a recording (updates primary meeting title in DB)
- `POST /api/recordings/{id}/summaries` save refined/manual summary to DB and vault
- `POST /api/summaries/refine` general purpose AI refinement endpoint
- `PUT /api/recordings/{id}/audio` upload full audio blob
- `PUT /api/recordings/{id}/audio/chunks/{index}` chunked upload (requires `X-Client-ID`)
- `POST /api/recordings/{id}/audio/finalize` finalize chunks (requires `X-Client-ID`)
- `GET /api/recordings/{id}/audio` stream/download audio
- `POST /api/recordings/{id}/export-obsidian-job` async export with progress
- `POST /api/recordings/{id}/transcription-job` transcription only (no summary)
- `GET /api/export-jobs/{job_id}` poll export job status
- `GET /api/transcription-jobs/{job_id}` poll transcription job status
- `WS /ws/audio` live stream + optional live preview

## Handoff Notes (2026-03-04, latest)

### Inline Rename on Recordings Page
- **Pencil icon on hover**: `.recording-title-row` wraps the title `<span>` + a `rename-btn` (✎). Icon is `opacity:0`, fades in on `.recording-card:hover`.
- **`_startRename(id, currentTitle)`** in `recordings.js`: replaces title row HTML with an `<input>` + Save/✕ buttons inline. Enter = save, Escape = cancel.
- **`_saveRename(id, newTitle)`**: `PATCH /api/recordings/{id}/title` → updates `this.recordings` local state → re-renders cards. Cancel and error both call `_renderRecordings()` to restore from state.
- **Backend**: `RenameRecordingRequest` + `PATCH /recordings/{session_id}/title` in `sessions.py`. Resolves primary meeting (sorted by `key_start`), validates non-empty title, calls `repository.update_meeting_title()`.

### Eager Background Transcription
- **`_startEagerProcessing()`** in `app.js`: called immediately when recording stops (alongside showing the naming modal). Persists audio and triggers a transcription job in the background.
- **Result**: by the time the user fills in the title/template and clicks Process, Whisper is likely done → export job skips transcription and goes straight to summarization.
- Uses `_eagerProcessingPromise` to avoid duplicate work; the export flow awaits it if still in progress.

### Summarization Progress Callbacks
- `generate_cohesive_summary()` in `cohesive.py` now accepts an optional `progress_callback: Callable[[float], None]`.
- `export.py` passes a callback that maps summarization progress (0–1) to the job's `summarization_progress` field.
- Result: the summarization progress bar in the UI now moves during LLM inference instead of being stuck at 0.

### Debug Monitor — WSL Inline (`scripts/monitor_sidekick.sh`)
- **New file**: `scripts/monitor_sidekick.sh` — alternate-screen TUI showing GPU, Whisper, Ollama, latest export job, and recent pipeline steps. Refreshes every 2s.
- **`./debug.sh`** (default) and **`./debug.sh monitor`** now launch this inline WSL monitor instead of spawning a PowerShell window.
- **Bug fix**: was using `set -euo pipefail` but multiple grep/pipeline commands lacked `|| true`. Script would flash then exit. Fixed by dropping `-e` (kept `-uo pipefail`) and adding `|| true` to grep extraction pipelines. Monitoring scripts should be resilient, not brittle.

## Handoff Notes (2026-03-04)

### View Modal — Details Rework & Template Tracking
- **Template stored per-summary**: `summaries.template` column (VARCHAR 100, nullable). Auto-migration in `_ensure_summary_template_column()`. `export.py` computes `template_label` before `add_summary()` and passes it in. `all_summaries` API response now includes `"template"`.
- **Details section (view modal)**: Replaced flex row with a two-column CSS grid (`#view-meta`). Rows: Template (omitted if null), Recorded, Exported (omitted if no summary), Length, Processing. Uses `_renderDetailsMeta(rec, summary)` helper; refreshes on version change.
- **Plain titles everywhere**: Modal header and history cards both show `rec.title` (no date prefix). Date is shown on the card and in the Recorded row of Details.
- **Audio player moved** to bottom of view modal (after summary, before Downloads).
- **"Summary Version" label**: The summary form-group label was renamed from "Summary" to "Summary Version".
- **Mobile Details fix**: `#view-meta` grid with `word-break: break-word` prevents horizontal overflow on iPhone. Label column uses `white-space: nowrap`.

## Handoff Notes (2026-03-03, latest)

### Model Switch: qwen3:8b + num_gpu=99 + temperature
- **Model**: Switched from `qwen3.5:9b` to `qwen3:8b`. qwen3.5 models always generate think tokens regardless of `think:False` (~3000-5000 wasted tokens per call = 80%+ of inference time). qwen3 models properly respect the option.
- **num_gpu=99**: Ollama's auto-estimate offloads ~3 layers of qwen3:8b to CPU. `OLLAMA_NUM_GPU=99` forces all layers to GPU → 2x tok/s speedup (62→127 tok/s). Applied in `ollama_backend.py`, both benchmark scripts.
- **temperature=0.3**: Added to all Ollama call options for consistent, factual summarization output.
- **New .env keys**: `OLLAMA_NUM_GPU` (int, default 99) added to `config/settings.py` and `ollama_backend.py`.

### Speaker Resolution Improvements
- **Better sampling**: `_resolve_speaker_map()` now uses first ~5000 chars (was 3000) plus all lines containing attendee names from the full transcript — gives much more signal for identification.
- **Clearer prompt**: Added explicit instruction that "if SPEAKER_00 says 'Hey Pam', that identifies who is being addressed, not who is speaking." This was the root cause of wrong assignments.
- **DB persistence**: `generate_cohesive_summary()` now returns `speaker_map` as 5th element. Propagated through `SummarizationResult.speaker_map`. After summarization in `export.py`, resolved mappings are written back to DB transcript segments → transcript UI shows real names on next load (was showing raw SPEAKER_XX labels).
- **Sessions API fix**: `speaker` field was being silently dropped from the `/api/recordings/{id}` transcript response — fixed in `sessions.py`.

### Pipeline Optimizations (Previous)
- **Speech-Aware Diarization:** Diarization scan now stops at the last Whisper transcript timestamp + 5s. Prevents 30+ min "waits" on forgotten recordings.
- **Dynamic Context:** `SummarizationManager` calculates `num_ctx` based on input size. Dramatically speeds up short meeting processing.
- **Single-Pass Early Exit:** Short transcripts (< 3000 chars) skip the editorial polish pass if the first draft is high quality.

### Unified View & Refinement
- **Parity:** The History "View" modal now matches the post-recording "Review" modal.
- **Refinement:** Added "Ask AI to Revise", "Edit Manually", and "Undo" support to history.
- **Saving:** "Save to Obsidian" in history view persists revisions to both DB and Vault with versioning.
- **Auto-Refresh:** Modal reloads metadata from server after save to update "Exported At" and "Processing Time".

### Mobile Optimization (iPhone)
- **Double Scroll Fix:** Modals now expand naturally and scroll as a single unit; internal scrollbars removed from summary boxes.
- **Typography:** Summary text size set to `13px` with `1.7` line height for readable mobile display.
- **Auto-Expand:** Manual edit textarea automatically stretches to fit content to prevent internal jumping.

### Markdown & Metadata
- **Core Logic:** `src/core/markdown_utils.py` centralizes Obsidian note construction.
- **Stats:** `Summary` model includes `processing_duration_seconds` and `template` (display name, e.g. "General Meeting").
- **Migration:** `src/sessions/repository.py` includes auto-backfill for new columns on startup (`_ensure_summary_template_column` added).
- **Audio Quality:** Captures at 48kHz for high-fidelity playback; downsampled to 16kHz for AI.

## Handoff Notes (2026-03-03, earlier)

### Obsidian Markdown Refinement
- `src/summarization/prompts.py` updated with a new `SYSTEM_PROMPT` and template specific instructions to prioritize bullet points over paragraphs.
- **Indentation:** Mandated 2-space nested indentation for sub-details.
- **Spacing:** Enforced blank lines between headers and content for better Obsidian preview.
- `src/summarization/cohesive.py` Pass 2 (editorial) updated to enforce these scannability standards.

## Handoff Notes (2026-03-03, diarization)

- `src/transcription/diarize.py` — pyannote.audio 4.0.4
- Audio loaded via **PyAV** (bundled FFmpeg) to avoid system FFmpeg dependency
- pyannote 4.x API: result is `DiarizeOutput`; use `.exclusive_speaker_diarization.itertracks(yield_label=True)`
- Three HuggingFace gated repos require license acceptance (one-time):
  - `pyannote/speaker-diarization-3.1`
  - `pyannote/segmentation-3.0`
  - `pyannote/speaker-diarization-community-1`
- `TranscriptSegment.speaker` column stores assigned label per segment
- `repository.update_segments_speakers()` bulk-updates speakers in one transaction
- Re-summarize path runs diarization when: `DIARIZATION_ENABLED=true` AND `has_speakers=False` on existing segments
- Non-blocking on failure — falls back to no-speaker transcript gracefully

### Speaker Name Resolution (attendees field)

- `ExportRequest.attendees: Optional[str]` — comma-separated names (e.g. "Oscar, Pam, Mike")
- `cohesive.py: _resolve_speaker_map()` — dedicated pre-pass LLM call using first ~5000 chars + all lines containing attendee names (for better identification)
- Prompt explicitly clarifies: "if SPEAKER_00 says 'Hey Pam', that identifies who is being addressed, not who is speaking"
- Returns `{"SPEAKER_00": "Oscar", "SPEAKER_01": "Pam", ...}` via JSON extraction
- `_apply_speaker_map()` does string replace across full transcript before Pass 1
- `generate_cohesive_summary()` now returns `speaker_map` as 5th element — propagated through `SummarizationResult.speaker_map`
- After summarization, `export.py` writes resolved names back to DB segments → transcript UI shows real names on next load
- Separates speaker identification from summarization — LLM not asked to do both at once
- UI: Attendees field in both `web/index.html` (naming modal) and `web/recordings.html` (re-summarize modal)

### Summarization LLM call count

For a typical meeting (fits in context):
1. Speaker pre-pass (if attendees provided)
2. Pass 1 — draft
3. Pass 2 — editorial polish
4. Retry (if artifacts/repetition detected — uncommon)

For long meetings (compressed pack fallback):
1. Speaker pre-pass
2. N × chunk extraction (8K char chunks)
3. Pass 1
4. Pass 2

### Working Session template restored

- `working_session` added back to `TEMPLATE_INFO` in `prompts.py`
- Full template content was already present; just re-exposed in UI
- Now shows as 4th option in chooser (before Custom)

### start.sh port check fix (WSL)

- `port_is_available()` now uses `connect()` instead of `bind()` for port availability check
- `bind()` gave false positives in WSL mirrored mode
- `connect()` accurately reflects whether something is actually listening

## Handoff Notes (2026-03-03, earlier)

### Cohesive summarization (single-pass)

- Removed pipeline routing from `export.py`; all templates use `SummarizationManager.summarize()` → `generate_cohesive_summary()`
- Template prompt in `prompts.py` is the "style contract" defining section structure
- Two-pass: draft (follows style contract) → editorial polish (preserves all `##` headers)

### Ollama artifact cleanup

- `ollama_backend.py` strips: complete `<think>...</think>` blocks, orphan `</think>`, control tokens (`<|...|>`)
- `OLLAMA_THINK=false` passed in options to suppress chain-of-thought generation

### Circular import fix (cohesive.py)

- `cohesive.py` uses `TYPE_CHECKING` + string annotations for `StructuredItems` to avoid import cycle with pipeline package

### Model / context history

| Model | VRAM | Context | Notes |
|-------|------|---------|-------|
| qwen3.5:35b-a3b | CPU+GPU split | — | Stalled under load |
| qwen3.5:27b | 54/46 split | — | Too slow |
| qwen3.5:9b-q8_0 | split | — | 10.7GB, split |
| qwen2.5:14b | 100% GPU | 32768 | ~15.4GB total with KV, severe CPU spillover (5 tok/s) |
| qwen3.5:4b | 100% GPU | 40960 | 2.5GB, quality concerns |
| qwen3.5:9b | 100% GPU | 32768 | 6.6GB — always generates think tokens regardless of think:False |
| qwen3:4b | 100% GPU | 32768 | 2.4GB, respects think:False, quality concerns |
| **qwen3:8b** | **100% GPU** | **32768** | **Current — 5.2GB, respects think:False, ~30s/25K chars** |

## Notes

- Obsidian Sync is near-real-time, not truly instant.
- `./stop.sh` can stop managed or detected unmanaged Sidekick processes.
- First startup with large-v3 Whisper model may be slow (downloads ~3GB).
- First export with diarization enabled downloads pyannote models (~1GB, cached after).
- `get_settings()` is LRU-cached — always `./restart.sh` after `.env` changes.
