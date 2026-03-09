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
3. Open the workspace, name recording, review speakers if needed, choose template, edit prompt
4. Generate or revise summary, then save/export to Obsidian markdown with real-time progress

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

`transcript -> humanized speaker labels -> two-pass cohesive summary -> markdown`

- All templates use `generate_cohesive_summary()` in `src/summarization/cohesive.py`
- **Speaker handling**: unresolved diarization labels are converted to `Attendee`, `Attendee A`, `Attendee B`, etc. before summarization
- **Pass 1 (draft)**: template style contract → draft following exact section structure
- **Pass 2 (polish)**: editorial rewrite preserving all `##` headers from draft
- **Retry pass**: triggered if artifacts or repeated sentences detected in pass 2 output
- Context budget: full transcript when it fits; compressed evidence pack fallback for long meetings; chunked extraction for very long meetings

Pipeline modules (`src/summarization/pipeline/`) remain in codebase but are **not invoked from export**.

## Summary Templates

Defined in `src/summarization/prompts.py`:

| Template | Description |
|----------|-------------|
| **General Meeting** | Summary, Key Decisions, Action Items table, Discussion Notes (default) |
| **Strategic Review** | Meeting Context, report review, decisions, strategy changes, milestones, next steps |
| **Working Session** | High-detail technical log — decisions, SQL notes, open questions |
| **Custom** | User-provided prompt |

UI template chooser order:
1. `meeting` (General Meeting)
2. `strategic_review`
3. `working_session`
4. `custom`

Legacy templates (constants kept for backward compat, not in UI): `one_on_one`, `standup`, `brainstorm`, `interview`, `lecture`

## UX Conventions

- Keep one primary action per step; avoid duplicate entry points.
- Recording list cards: `Open` and `Delete` only.
- Re-summarize/export initiated from the shared workspace, not cards.
- **Unified Workspace:** Both History `Open` and post-recording review support identical features: AI refinement, manual editing, and undo history.
- **View Modal Title:** Shows plain meeting title (no date prefix). Date/time is in the Details section.
- **Rename flow:** Rename from the editable workspace title after opening a recording. Do not reintroduce card-level rename controls.
- **Details Section:** Two-column grid (label + value) — Template, Recorded, Exported, Length, Processing Time. Template omitted for old summaries (null). Updates when switching summary versions.
- **Summary label:** "Summary Version" (not "Summary") in the view modal.
- **Summary versions:** `vN (Draft)`, `vN (Latest)`, then descending `vN-1 ... v1`.
- **Metadata Visibility:** Details section mirrors Obsidian markdown header fields. Template is stored per-summary in DB (`summaries.template`).
- **Obsidian Save:** "Save to Obsidian" button available in history view to create versioned copies or re-export.
- **Audio player** is positioned at the bottom of the modal (below summary, above Downloads).
- Download affordances in the view modal (`Download Audio`, `Download Transcript`).
- Recording list cards show plain title (no date prefix) — date is shown separately on the card.
- History delete is optimistic: remove the card locally first, then do a non-blocking background refresh instead of depending on a full blocking refetch.
- Template chooser shows 4 templates in the order above.
- Keep `General Meeting` as default unless explicit product changes requested.
- Speaker naming is manual-first in the workspace `Speakers` tab.
- Summary generation is allowed before speaker review is complete.
- Settings is summary-only and follows the currently selected summary version by default.
- Workspace tab hide/reveal motion is device-specific: polished on desktop, simpler direct tracking on mobile.
- Main page locks scroll / pull-to-refresh and guards against leaving while recording.
- Recording page visualizer is a minimalist log-spaced spectrum analyzer, not a waveform.

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
- **qwen3 vs qwen3.5**: `qwen3:8b` properly respects `OLLAMA_THINK=false`. `qwen3.5` models always generate 3000-5000 think tokens regardless of the setting.
- `OLLAMA_NUM_GPU=99` forces all layers to GPU (bypasses Ollama's conservative auto-estimate). Doubles tok/s.
- `OLLAMA_CONTEXT_LENGTH=32768` fits model + KV cache 100% in 16GB VRAM. Supports ~2.5+ hours of speech.
- `temperature=0.3` is set in all Ollama call options for consistent, factual output.
- Ollama runs on **Windows host**; Sidekick in **WSL** with mirrored networking → `127.0.0.1:11434` works directly

## Output Format (Current)

- Filename: `YYYY-MM-DD-HHMM - [Title] [Template].md`
- **Versioning:** Exports append ` (v2)`, ` (v3)`, etc., to filename if summary already exists for that meeting.
- Metadata block: Template, Recorded date, Exported date, Duration, Processing Time
- Summary body follows template section structure
- **Markdown Standard:** Strict bullet-point-first structure with nested indentation (2 spaces) and blank lines between sections for Obsidian scannability.
- Prompt audit blocks: `Pass 1: System Prompt`, `Pass 1: User Prompt`, `Pass 2: System Prompt`, `Pass 2: User Prompt`
- Collapsible `Transcript` section (with resolved names or fallback labels like `Attendee A`)

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
- `PATCH /api/recordings/{id}/title` rename a recording
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

## Notes

- Obsidian Sync is near-real-time, not truly instant.
- PWA planning doc: `docs/pwa-plan.md` in-repo, mirrored to the Obsidian vault under `Sidekick/pwa-plan.md`.
- `./stop.sh` can stop managed or detected unmanaged Sidekick processes.
- First startup with large-v3 Whisper model may be slow (downloads ~3GB).
- First export with diarization enabled downloads pyannote models (~1GB, cached after).
- `get_settings()` is LRU-cached — always `./restart.sh` after `.env` changes.
- Remote use through `go.sidekickgo.app` depends on the current Cloudflare quick tunnel URL. The rendered HTML injects fallback `wss://` and `https://*.trycloudflare.com` transport targets, and `web/js/network.js` rewrites browser `/api/...` plus recording-media URLs onto that fallback when present.
- Frontend cache busting: if a change to `web/index.html`, `web/recordings.html`, `web/css/styles.css`, or `web/js/*.js` does not show up after refresh, bump the `?v=` asset query string in the relevant HTML entrypoint first. Treat stale browser assets as a common cause before assuming the CSS/JS change failed.
