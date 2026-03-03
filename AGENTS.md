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
│   │       ├── sessions.py           # Recording CRUD, chunked audio upload
│   │       └── websocket.py          # Live audio stream + optional live preview
│   ├── audio/
│   │   └── storage.py                # Audio file management, chunk recovery
│   ├── core/
│   │   └── datetime_utils.py         # Timezone helpers
│   ├── sessions/
│   │   ├── models.py                 # SQLAlchemy models (Session, Meeting,
│   │   │                             #   TranscriptSegment w/ speaker, StructuredItem)
│   │   └── repository.py             # DB CRUD incl. update_segments_speakers()
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
│   ├── recordings.html               # History / re-summarize UI (has Attendees field)
│   ├── css/styles.css
│   └── js/
│       ├── app.js                    # Recording + export flow
│       ├── recordings.js             # History + re-summarize flow
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
    └── benchmark_ollama_models.py    # Benchmark models on real transcript chunks
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
                    │  Pass 1: Draft                             │
                    │    system: template style contract         │
                    │           + attendees note                 │
                    │    user:  transcript (or compressed pack   │
                    │           if transcript > context budget)  │
                    │                                            │
                    │  Pass 2: Editorial polish                  │
                    │    Preserves all ## headers from draft     │
                    │                                            │
                    │  Retry (if artifacts / repetition):        │
                    │    One additional cleanup pass             │
                    └───────────┬────────────────────────────────┘
                                │
                    Build Obsidian markdown:
                    YYYY-MM-DD-HHMM - [Title] [Template].md
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
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_THINK=false
OLLAMA_CONTEXT_LENGTH=40960
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

Legacy (constants kept for backward compat, not shown in UI):
`strategic_review`, `brainstorm`, `interview`, `lecture`

Default template: `meeting`

## UX Conventions

- One primary action per step; no duplicate entry points.
- Recording list cards: `View` and `Delete` only.
- Export / re-summarize initiated from the view modal, not from cards.
- Download affordances (`Download Audio`, `Download Transcript`) live in the view modal.
- Template chooser shows 5 templates in the order above.
- `General Meeting` is default unless explicitly changed.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Main recording UI |
| `GET /recordings` | History UI |
| `GET /api/templates` | List templates with prompts |
| `GET /api/recordings` | List recordings |
| `GET /api/recordings/{id}` | Recording detail |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export job |
| `POST /api/recordings/{id}/transcription-job` | Transcription only (no summary) |
| `GET /api/transcription-jobs/{job_id}` | Poll transcription job |
| `PUT /api/recordings/{id}/audio` | Upload full audio blob (fallback) |
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Upload chunk (needs `X-Client-ID`) |
| `POST /api/recordings/{id}/audio/finalize` | Finalize chunks (needs `X-Client-ID`) |
| `WS /ws/audio` | Live audio stream |

## Export Job Progress Weights

| Stage | Overall % |
|-------|-----------|
| Transcription (incl. diarization) | 0–40% |
| Summarization (speaker pre-pass + 2 passes) | 40–95% |
| Write to vault | 95–100% |

## Speaker Diarization

**Status**: Live and enabled.

**Dependencies** (accepted HuggingFace gated licenses required):
- `pyannote/speaker-diarization-3.1`
- `pyannote/segmentation-3.0`
- `pyannote/speaker-diarization-community-1`

**Key implementation notes**:
- Audio loaded via **PyAV** (bundled FFmpeg) — no system `ffmpeg` install needed
- pyannote 4.x returns `DiarizeOutput`; use `.exclusive_speaker_diarization.itertracks(yield_label=True)`
- Diarization runs once per recording; subsequent re-summarizes skip it (`has_speakers` guard)
- Non-blocking on failure: falls back to no-speaker transcript

**Speaker name resolution**:
- If `attendees` is provided in the export request, a dedicated pre-pass LLM call resolves `SPEAKER_XX` → real names
- The mapping is applied via string replace on the full transcript before any summarization pass
- Pre-pass uses first ~3000 chars for efficient identification

## Gotchas

- `get_settings()` is LRU-cached — restart required to pick up `.env` changes
- `qwen3.5` models output `<think>...</think>` blocks; `ollama_backend.py` strips them and passes `think: false`
- `OLLAMA_THINK=false` is critical — think mode on summaries wastes tokens and time with no benefit
- Pipeline package (`src/summarization/pipeline/`) is in codebase but **not called from export**
- Re-summarize reuses existing transcript when `session.has_transcription=true` AND segments exist
- `start.sh` port check uses `connect()` (not `bind()`) to avoid false positives in WSL mirrored mode
- Ollama runs on **Windows host**, Sidekick runs in **WSL** — mirrored networking makes `127.0.0.1:11434` work
- Context budget: `OLLAMA_CONTEXT_LENGTH=40960` suits `qwen3.5:9b` (6.6GB model, ~9.4GB headroom for KV cache on 16GB VRAM). Larger models need reduced context or will split CPU/GPU.
