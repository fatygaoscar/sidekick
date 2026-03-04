# Sidekick Agent Handoff

Quick reference for AI agents working on this codebase.

## Runtime Commands

```bash
./start.sh                # Start server
./start.sh --ngrok        # Start with ngrok public URL
./start.sh --cloudflare   # Start with Cloudflare quick tunnel
./restart.sh              # Restart server
./restart.sh --cloudflare # Restart with Cloudflare tunnel
./stop.sh                 # Stop server
./status.sh               # Check status + public URL
./debug.sh                # Unified debug helper (ollama / export / benchmark)
```

## File Structure

```
sidekick/
├── start.sh / restart.sh / stop.sh / status.sh / debug.sh
├── .env                              # All runtime config
├── config/
│   └── settings.py                   # Pydantic settings, LRU-cached via get_settings()
│
├── src/
│   ├── main.py                       # FastAPI app entry point
│   ├── api/
│   │   └── routes/
│   │       ├── export.py             # Async export jobs, diarization, transcription pipeline
│   │       ├── sessions.py           # Recording CRUD, chunked audio upload, refined saves
│   │       └── websocket.py          # Live audio stream + optional live preview
│   ├── audio/
│   │   └── storage.py                # Audio file management, chunk recovery
│   ├── core/
│   │   ├── datetime_utils.py         # Timezone helpers
│   │   └── markdown_utils.py         # SHARED Obsidian note construction logic
│   ├── sessions/
│   │   ├── models.py                 # SQLAlchemy models (Summary has processing_duration_seconds)
│   │   └── repository.py             # DB CRUD incl. update_segments_speakers() + migrations
│   ├── summarization/
│   │   ├── cohesive.py               # Two-pass summary + speaker pre-pass
│   │   ├── manager.py                # Summarization orchestration, passes attendees
│   │   ├── ollama_backend.py         # Ollama client, strips <think> blocks
│   │   ├── prompts.py                # Template strings + TEMPLATE_INFO (UI order)
│   │   └── pipeline/                 # Kept in codebase but NOT invoked from export
│   │       ├── types.py
│   │       ├── chunker.py
│   │       ├── extraction.py
│   │       ├── merger.py
│   │       ├── structurer.py
│   │       ├── narrator.py
│   │       └── pipeline.py
│   └── transcription/
│       ├── diarize.py                # pyannote.audio 4.x diarization (PyAV audio loading)
│       ├── manager.py                # Transcription orchestration
│       └── whisper_local.py          # faster-whisper with progress callbacks
│
├── web/
│   ├── index.html                    # Main recording UI (has Attendees field)
│   ├── recordings.html               # History / re-summarize UI (unified View modal)
│   ├── css/styles.css                # Mobile-optimized (13px text, no double scroll)
│   └── js/
│       ├── app.js                    # Recording + export flow (Review modal has Undo)
│       ├── recordings.js             # History + re-summarize flow (View modal has Refine)
│       ├── audio.js                  # AudioCapture, visualizer
│       └── websocket.js              # WebSocket client, 25s keepalive ping
│
├── data/                             # Runtime data (gitignored)
│   ├── sidekick.db                   # SQLite database
│   ├── sidekick.log                  # App logs (cleared on each start)
│   ├── sidekick.pid                  # Managed process PID
│   └── audio/
│       ├── {session_id}.webm         # Finalized recordings
│       └── chunks/{session_id}/{client_id}/  # Temp upload chunks
│
└── scripts/
    ├── monitor_ollama.ps1            # PowerShell: Ollama + GPU live watcher
    ├── monitor_export_job.sh         # Bash: poll export job progress
    ├── benchmark_ollama_models.py    # Benchmark raw model latency on transcript chunks
    └── benchmark_summary.py         # Benchmark full two-pass cohesive summary pipeline
```

## Export Pipeline (Full Flow)

```
Browser
  │
  ├─[during recording]──► WebSocket stream ──► Live preview text (optional, not authoritative)
  │
  └─[stop recording]────► Audio saved: data/audio/{session_id}.webm
                                │
                     POST /export-obsidian-job
                     {title, template, attendees, custom_prompt}
                                │
                    ┌───────────▼────────────┐
                    │   HAS TRANSCRIPT?      │
                    └───────┬────────┬───────┘
                         YES│        │NO
                            │        ▼
                            │   Whisper large-v3 (CUDA)
                            │   Word-level timestamps → segments → DB
                            │        │
                    ┌───────▼────────▼───────┐
                    │  DIARIZATION ENABLED?  │
                    │  (DIARIZATION_ENABLED) │
                    └───────────┬────────────┘
                             YES│
                                ▼
                    pyannote/speaker-diarization-3.1
                    Audio loaded via PyAV (no system FFmpeg)
                    → (start, end, SPEAKER_XX) spans
                    → assign_speaker() aligns to segments
                    → DB update (update_segments_speakers)
                                │
                    ┌───────────▼────────────────────────────────┐
                    │  Build transcript string                    │
                    │  [MM:SS] SPEAKER_XX: text (per segment)    │
                    └───────────┬────────────────────────────────┘
                                │
                    ┌───────────▼────────────────────────────────┐
                    │  SUMMARIZATION  (cohesive.py)              │
                    │                                            │
                    │  Pre-pass (if attendees provided):         │
                    │    LLM maps SPEAKER_XX → real names        │
                    │    Apply string replace across transcript  │
                    │                                            │
                    │  Pass 1: Draft (Indented bullets)          │
                    │    system: template style contract         │
                    │           + attendees note                 │
                    │    user:  transcript                       │
                    │                                            │
                    │  Pass 2: Editorial polish (No paragraphs)  │
                    │    Preserves all ## headers from draft     │
                    │                                            │
                    └───────────┬────────────────────────────────┘
                                │
                    Build Obsidian markdown (markdown_utils.py):
                    YYYY-MM-DD-HHMM - [Title] [Template] (vN).md
                    Metadata block + summary + collapsible transcript
                                │
                    Write to OBSIDIAN_VAULT_PATH
                                │
                    Return job result + obsidian:// URI
```

## Key Config (.env)

```
# Transcription
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16

# Summarization
SUMMARIZATION_BACKEND=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_THINK=false
OLLAMA_NUM_GPU=99
OLLAMA_CONTEXT_LENGTH=32768
SUMMARIZATION_TIMEOUT_SECONDS=300

# Diarization
HF_TOKEN=<huggingface_read_token>
DIARIZATION_ENABLED=true

# Export
OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault
```

## Templates

Active (shown in UI, in chooser order):

| Key | Name | Description |
|-----|------|-------------|
| `meeting` | General Meeting | Summary, Key Decisions, Action Items table, Discussion Notes |
| `one_on_one` | 1-on-1 | Summary, Highlights, Feedback, Goals, Action Items table |
| `standup` | Standup | Per-person Done/Doing/Blocked, Team Blockers, Action Items |
| `working_session` | Working Session | High-detail technical log — decisions, SQL notes, open questions |
| `custom` | Custom | User-provided prompt |

Default template: `meeting`

## UX Conventions

- One primary action per step; no duplicate entry points.
- Recording list cards: `View` and `Delete` only.
- Export / re-summarize initiated from the view modal, not from cards.
- **Unified Modals:** History "View" matches post-recording "Review". Both support AI Refine, Manual Edit, and Undo.
- **Mobile optimization:** `13px` text, `1.7` line height, single-unit scroll (no double scrollbars).
- Metadata (`Exported At`, `Processing Time`) prominently displayed.
- Template chooser shows 5 templates in the order above.
- `General Meeting` is default unless explicitly changed.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Main recording UI |
| `GET /recordings` | History UI |
| `GET /api/templates` | List templates with prompts |
| `GET /api/recordings` | List recordings |
| `GET /api/recordings/{id}` | Recording detail (includes latest `summary` + metadata) |
| `POST /api/recordings/{id}/summaries` | Save refined/manual summary to DB and vault |
| `POST /api/summaries/refine` | General AI refinement endpoint |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export job |
| `POST /api/recordings/{id}/transcription-job` | Transcription only |
| `PUT /api/recordings/{id}/audio` | Upload audio |
| `WS /ws/audio` | Live audio stream |

## Speaker Diarization

**Status**: Live and enabled.

**Handoff Notes (2026-03-03, latest)**:
- **Model**: `qwen3:8b` (5.2GB, 100% GPU). `OLLAMA_NUM_GPU=99` forces all layers to GPU. `temperature=0.3` added to all calls.
- **Speaker Resolution**: `_resolve_speaker_map()` uses first 5000 chars + attendee name lines from full transcript. Prompt clarifies "addressing vs. being" speaker distinction. Resolved `speaker_map` returned from `generate_cohesive_summary()` as 5th element and persisted to DB segments after export.
- **Pipeline Optimizations**:
  - Speech-Aware Diarization: stops at last Whisper timestamp + 5s.
  - Dynamic Context: `num_ctx` calculated from input size.
  - Single-Pass Early Exit: short transcripts (< 3000 chars) skip polish pass.
- **Audio Quality**: Captures and saves at 48kHz; downsampled to 16kHz for AI.
- **Unified View & Refinement:** functionally identical review/view modals.
- **Obsidian Versioning:** exports append `(v2)`, `(v3)`, etc.
- **Markdown Logic:** Consolidated into `src/core/markdown_utils.py`.
- **Database:** Auto-migrations in `repository.py` for `processing_duration_seconds`.

**Key implementation notes**:
- Audio loaded via **PyAV** — no system `ffmpeg` needed.
- pyannote 4.x returns `DiarizeOutput`.
- Speaker name resolution pre-pass LLM call if `attendees` provided. Results persisted to DB.

## Gotchas

- `get_settings()` is LRU-cached — restart required to pick up `.env` changes.
- **qwen3 vs qwen3.5 thinking**: `qwen3:8b` properly respects `OLLAMA_THINK=false`. `qwen3.5` models always generate 3000-5000 think tokens per call regardless of this setting — not suppressable at the application level.
- `OLLAMA_NUM_GPU=99` is required — Ollama's auto-estimate offloads ~3 layers to CPU for qwen3:8b (shows 8%/92% split in `ollama ps`). This halves tok/s. Setting `num_gpu=99` forces all layers to GPU.
- `temperature=0.3` is set in all Ollama call options for consistent, factual output (Ollama default is 0.8).
- Mobile: removed `max-height` from internal containers to fix double scrolling.
- Context budget: `OLLAMA_CONTEXT_LENGTH=32768` suits `qwen3:8b` (5.2GB model). Fits 100% in 16GB VRAM. Supports ~2.5+ hours of speech. Larger context or larger models cause CPU spillover.
- Speaker name resolution requires the **Attendees field** to be filled in at export time. Without it, SPEAKER_XX labels remain unresolved. With it, the pre-pass maps labels to names and writes them back to the DB.
- Pull Ollama models from Windows PowerShell, not WSL: `powershell.exe -Command "ollama pull qwen3:8b"`
