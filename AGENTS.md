# Sidekick Agent Handoff

Quick reference for AI agents working on this codebase.

## Runtime Commands

```bash
./start.sh          # Start server
./start.sh --ngrok  # Start with public URL
./start.sh --cloudflare  # Start with Cloudflare quick tunnel URL
./restart.sh        # Restart server
./restart.sh --cloudflare  # Restart with Cloudflare tunnel
./debug.sh          # Unified debug helper (ollama/export/benchmark)
./status.sh         # Check status
./stop.sh           # Stop server
```

## Project Structure

```
src/
├── api/routes/
│   ├── export.py       # Async export jobs with progress
│   ├── sessions.py     # Recording CRUD, chunked upload
│   └── websocket.py    # Live audio streaming
├── transcription/
│   ├── whisper_local.py  # faster-whisper with progress callbacks
│   └── manager.py        # Transcription orchestration
├── summarization/
│   ├── prompts.py        # Template definitions
│   ├── ollama_backend.py # Local LLM
│   ├── manager.py        # Summarization orchestration
│   └── pipeline/         # Multi-stage extraction pipeline
│       ├── types.py      # ExtractedItem, PipelineResult
│       ├── chunker.py    # Time-based transcript splitting
│       ├── extraction.py # Per-chunk item extraction
│       ├── merger.py     # Cross-chunk deduplication
│       ├── structurer.py # ID assignment, validation
│       ├── narrator.py   # Narrative generation
│       └── pipeline.py   # Orchestrator
├── sessions/
│   ├── models.py         # SQLAlchemy models (incl. StructuredItem)
│   └── repository.py     # Database operations
└── audio/
    └── storage.py        # Audio file management

web/
├── index.html          # Main recording UI
├── recordings.html     # History/re-export UI
├── css/styles.css      # All styles
└── js/
    ├── app.js          # Main app logic
    ├── recordings.js   # History page logic
    ├── audio.js        # AudioCapture, visualizer
    └── websocket.js    # WebSocket client with keepalive
```

## Key Config (.env)

```
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
SUMMARIZATION_BACKEND=ollama
OLLAMA_MODEL=qwen2.5:14b
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_CONTEXT_LENGTH=4096
OLLAMA_THINK=false
SUMMARIZATION_TIMEOUT_SECONDS=120
OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault
```

## Templates (src/summarization/prompts.py)

- `one_on_one` - 1-on-1 meetings
- `standup` - Daily standups
- `strategic_review` - Leadership meetings
- `working_session` - Technical work sessions
- `meeting` - General meetings (default)
- `brainstorm`, `interview`, `lecture`, `custom`

UI template chooser order (shown templates only):
1. `meeting` (General Meeting)
2. `strategic_review`
3. `working_session`
4. `standup`
5. `one_on_one`
6. `brainstorm`
7. `custom`

## UX Conventions

- Prefer one clear action per step; remove duplicated actions across list cards and modals.
- Recording history cards should stay minimal: `View` and `Delete` only.
- Export/re-summarize actions should happen from the view pane context, not from list cards.
- Download actions should live in the view pane (`Download Audio`, `Download Transcript`), not cards.
- Keep template chooser focused to the primary 7 templates in the defined order above.
- Default template should remain `meeting` unless product direction changes.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/templates` | List templates with prompts |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export progress |
| `PUT /api/recordings/{id}/audio` | Upload full audio blob (authoritative fallback) |
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Upload chunk (requires `X-Client-ID` header) |
| `POST /api/recordings/{id}/audio/finalize` | Finalize chunks (requires `X-Client-ID` header) |

## Export Progress Flow

1. Frontend calls `POST /export-obsidian-job` → returns `job_id`
2. Frontend polls `GET /export-jobs/{job_id}` every 900ms
3. Backend updates job state as transcription segments complete
4. Transcription progress: 0-40% (real segment-based)
5. Pipeline progress: 40-95% (chunking → extraction → merge → structure → narrative)
6. Write progress: 95-100%

## Recent Improvements

- **Timer**: Uses `Date.now()` wall clock, no drift when tab inactive
- **WebSocket**: Ping every 25s, unlimited reconnects, visibility-change reconnect
- **Audio Upload**: Parallel chunks with client isolation, idempotent writes, blob-first fallback
- **Progress**: Real segment-based transcription progress
- **Templates**: Only primary templates shown in chooser, ordered for common usage; prompts still editable before export
- **History UX**: Removed redundant card actions (no card-level Export/Download Audio)
- **View Pane Actions**: Audio download kept in view modal, added transcript download button, re-summarize remains primary action
- **Export**: Includes both recorded and exported timestamps

## Handoff Notes (2026-03-02)

- **Multi-Stage Pipeline**: Replaced single-pass summarization with structured extraction.
  - Chunks transcript into 8-12 min segments
  - Extracts actions, decisions, risks, questions, follow-ups per chunk
  - Deduplicates across chunks (Jaccard similarity)
  - Assigns IDs: A-001, D-001, R-001, Q-001, F-001
  - Generates narrative referencing all item IDs
  - Coverage checking with patch for missed items
  - Output: narrative + markdown tables + transcript
- **Model upgrade**: `qwen2.5:14b` → `qwen3.5:35b-a3b` (pull with `ollama pull qwen3.5:35b-a3b`)
- **Database**: New `StructuredItem` model stores extracted items per meeting

- **Template-Aware Routing** (`src/api/routes/export.py`):
  - `_PIPELINE_TEMPLATES = {"meeting", "standup", "one_on_one", "strategic_review"}` → multi-stage pipeline
  - All other templates (`working_session`, `brainstorm`, `interview`, `lecture`, `custom`) → single-pass with full transcript
  - Single-pass uses `SummarizationManager.summarize()` with the template's rich prompt and produces same header format (Template, Recorded, Exported, Duration) + LLM content + collapsible transcript
  - Reason: pipeline narrator only sees ~30 lines of context; content-heavy templates need the full transcript to produce quality output

## Handoff Notes (2026-03-02, later)

- **Export stability fixes (re-summarize + pipeline)**:
  - Re-summarize now reuses existing transcript when both conditions are true:
    - `session.has_transcription == true`
    - transcript segments exist for session
  - Export preview bug fixed (`summary_result` typo -> `pipeline_result.narrative`).
  - Pipeline extraction failures now log per-chunk exceptions with chunk index/timestamps.
  - Export fails explicitly when all extraction chunks fail:
    - `All extraction chunks failed (timeout/backend).`
  - Extraction errors are no longer silently swallowed in `extraction.py`.
  - Frontend 20-minute hard timeout removed for export polling in both:
    - `web/js/recordings.js`
    - `web/js/app.js`

- **Summarization timeout hardening**:
  - Timeout is enforced centrally in `SummarizationManager` for all backends, not Ollama-only.
  - New config:
    - `SUMMARIZATION_TIMEOUT_SECONDS` (default 600)
  - Timeout exception message:
    - `Summarization model call timed out after <N> seconds`

- **Ollama request tuning**:
  - Added `OLLAMA_CONTEXT_LENGTH` config and pass-through to Ollama chat option `num_ctx`.
  - Current tested value for reliability on this hardware: `3072`.

- **Host Ollama migration notes**:
  - Confirmed working architecture: Sidekick in WSL + Ollama on Windows host.
  - Mirrored-networking secure path is preferred:
    - Keep host Ollama on localhost (`127.0.0.1`)
    - WSL calls `http://127.0.0.1:11434`
  - If not using mirrored mode, host binding/firewall scoping is required.
  - `qwen3.5:35b-a3b` showed repeated timeout/hang behavior on 16GB VRAM under extraction load.
  - `qwen3.5:27b` was introduced as next recommended quality/perf test.

- **Debug and benchmarking tooling added**:
  - New scripts:
    - `debug.sh` (unified wrapper)
    - `scripts/monitor_ollama.ps1`
    - `scripts/monitor_export_job.sh`
    - `scripts/benchmark_ollama_models.py`
  - Key commands:
    - `./debug.sh ollama --gpu --interval 1`
    - `./debug.sh export-latest`
    - `./debug.sh benchmark --runs 2`
  - `monitor_export_job.sh` and `debug.sh` were patched to avoid `rg` hard dependency (grep fallback).

- **Ops doc handling**:
  - Detailed host-ollama setup/troubleshooting doc:
    - `docs/HOST_OLLAMA_SETUP.md`
  - Added to `.gitignore` for local-only ops usage.

## Handoff Notes (2026-02-11)

- **Audio Upload Redesign**: Prevents multi-device corruption and ensures reliable persistence.
  - Client isolation via `X-Client-ID` header; chunks at `data/audio/chunks/{session}/{client}/`
  - Order-independent: chunks can arrive in any order (no 409 for out-of-order)
  - Idempotent: re-uploading same chunk is a no-op
  - Parallel uploads with blob-first fallback if chunks fail
  - Legacy `.part` recovery still works for backward compatibility

## Handoff Notes (2026-02-10)

- **Audio Recovery Fix**: If user stops recording and closes naming modal without processing, audio is now still recoverable for later history re-summarize/export.
  - Frontend `web/js/app.js` now does best-effort background audio persistence before modal close resets state.
  - Backend `src/audio/storage.py` adds `ensure_session_audio_path()` to promote chunk partial (`.part`) into finalized session audio when possible.
  - Recovery is used in `src/api/routes/sessions.py` (recording list/detail/audio endpoints + finalize flow) and `src/api/routes/export.py` (authoritative transcription path).
- **Obsidian Open Reliability**:
  - Export URI now opens exact `.md` filename (`obsidian://open?...&file=<name>.md`) instead of extensionless file path.
  - Recording view now prefers direct `obsidian://open` to resolved exported note; falls back to `obsidian://search` only when no filename match is found.
- **Cloudflare Tunnel Behavior**:
  - `--cloudflare` uses quick tunnel (`*.trycloudflare.com`) and URL is ephemeral.
  - URL generally changes after restart; keep process running for temporary stability.
  - Stable URL requires named tunnel + owned domain (not available with quick tunnel only).
