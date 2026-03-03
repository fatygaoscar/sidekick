# Sidekick

Browser-based meeting recorder that transcribes audio, identifies speakers, and exports structured notes to Obsidian — entirely local.

## Features

- **Browser recording** with real-time audio visualization
- **Local transcription** via faster-whisper large-v3 (CUDA)
- **Speaker diarization** via pyannote.audio 3.1 — labels `SPEAKER_00`, `SPEAKER_01`, etc.
- **Speaker name resolution** — provide attendee names and the LLM maps labels to real people before summarizing
- **Structured templates** — meeting notes, 1-on-1, standup, working session, custom
- **Editable prompts** — customize any template before export
- **Real-time progress** — live percent tracking through transcription and summarization
- **Obsidian export** — writes a dated `.md` file and opens it with `obsidian://`
- **Phone access** — ngrok or Cloudflare tunnel support

## Quick Start

```bash
./start.sh                # Start in background
./start.sh --ngrok        # Start + ngrok public URL
./start.sh --cloudflare   # Start + Cloudflare quick tunnel
./restart.sh              # Restart cleanly
./status.sh               # Check status + public URL
./stop.sh                 # Stop server
./debug.sh                # Unified debug helper
```

Then open `http://localhost:8000`.

## Architecture

### Two Transcription Pipelines

```
┌─────────────────────────────────────────────────────────────┐
│ LIVE PREVIEW PIPELINE (optional UX only)                    │
│                                                             │
│  Microphone → WebSocket chunks → Live preview text         │
│  src/api/routes/websocket.py                               │
│  Not source of truth. Not used in export.                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ EXPORT PIPELINE (authoritative)                             │
│                                                             │
│  Saved audio file → Whisper → Diarization →                │
│  Speaker map → Two-pass summary → Obsidian .md             │
│  src/api/routes/export.py                                   │
└─────────────────────────────────────────────────────────────┘
```

### Export Pipeline — Full Flow

```
Browser
  │
  ├─[during recording]──► WebSocket stream ──► Live preview text (not authoritative)
  │
  └─[stop recording]────► Audio saved: data/audio/{session_id}.webm
                                │
                     POST /api/recordings/{id}/export-obsidian-job
                     {title, template, attendees, custom_prompt}
                                │
                    ┌───────────▼────────────┐
                    │   HAS TRANSCRIPT?      │
                    └───────┬────────┬───────┘
                         YES│        │NO
                            │        ▼
                            │   faster-whisper large-v3 (CUDA)
                            │   Word-level timestamps → segments → DB
                            │        │
                    ┌───────▼────────▼───────┐
                    │  DIARIZATION ENABLED?  │
                    │  (DIARIZATION_ENABLED) │
                    └───────────┬────────────┘
                             YES│  (skipped if speakers already in DB)
                                ▼
                    pyannote/speaker-diarization-3.1
                    Audio loaded via PyAV (bundled FFmpeg)
                    → (start, end, SPEAKER_XX) spans
                    → assign_speaker() aligns to transcript segments
                    → Saved to DB (TranscriptSegment.speaker)
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

### File Structure

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

## Configuration

Edit `.env`:

```bash
# Transcription
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16

# Speaker Diarization
HF_TOKEN=<your_huggingface_read_token>
DIARIZATION_ENABLED=true

# Summarization
SUMMARIZATION_BACKEND=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_THINK=false
SUMMARIZATION_TIMEOUT_SECONDS=300
OLLAMA_CONTEXT_LENGTH=40960

# Export
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

### Model Selection Guide

| Model | VRAM | Quality | Notes |
|-------|------|---------|-------|
| `qwen3.5:4b` | ~2.5 GB | Good | 100% GPU, fits 64K context on 16GB VRAM |
| `qwen3.5:9b` | ~6.6 GB | Better | 100% GPU at 40K context, ~9.4GB headroom for KV cache |
| `qwen2.5:14b` | ~10.3 GB | Best local | Only ~5.7GB left for KV cache — spills to RAM at 40K context |

`qwen3.5:9b` is the recommended default for 16GB VRAM systems.

**Important**: `OLLAMA_THINK=false` is critical — qwen3.5 models output `<think>...</think>` chain-of-thought blocks by default. This wastes tokens and degrades quality.

### WSL + Host Ollama

Sidekick runs in WSL; Ollama runs on Windows host. With WSL mirrored networking mode, `127.0.0.1:11434` works directly — no special host address needed.

```bash
# .env
OLLAMA_HOST=http://127.0.0.1:11434
```

### Speaker Diarization Setup

Diarization is free, fully local, and runs on GPU.

1. Create a free account at [huggingface.co](https://huggingface.co)
2. Accept the license for each gated model:
   - [pyannote/speaker-diarization-3.1](https://hf.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://hf.co/pyannote/segmentation-3.0)
   - [pyannote/speaker-diarization-community-1](https://hf.co/pyannote/speaker-diarization-community-1)
3. Generate a read token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
4. Add to `.env`:
   ```bash
   HF_TOKEN=hf_...
   DIARIZATION_ENABLED=true
   ```

Models download once (~1 GB total) and run locally from then on. Diarization runs once per recording — re-summarizing reuses the saved speaker labels.

## Templates

| Template | Best For |
|----------|----------|
| **General Meeting** | Standard meetings — summary, decisions, action items, discussion notes (default) |
| **1-on-1** | Manager/report — highlights, feedback, goals, action items |
| **Standup** | Daily status — per-person done/doing/blocked, team blockers |
| **Working Session** | Technical work — decisions, SQL notes, open questions, high detail |
| **Custom** | User-provided prompt — targeted extraction for a specific audience or artifact |

All templates are editable before export (click "Show" to view and modify the prompt).

## Usage

1. **Record** — Click the microphone button to start
2. **Stop** — Click again to stop recording
3. **Title** — Give the recording a name
4. **Attendees** *(optional)* — Enter names (e.g. `Oscar, Jane, Mike`) to resolve speaker labels to real names in the summary
5. **Template** — Choose a template; click "Show" to edit the prompt
6. **Export** — Watch real-time progress through transcription and summarization
7. **Open in Obsidian** — One click opens the exported note

## API

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

## Debugging

```bash
# Unified debug helper (defaults to export monitor + Ollama GPU window)
./debug.sh

# Monitor latest export job from logs
./debug.sh export-latest

# Monitor a specific export job
./debug.sh export <job_id>

# Live Ollama + GPU stats (launches PowerShell watcher)
./debug.sh ollama

# Benchmark model latency on a real recording
./debug.sh benchmark --runs 2
```

## Requirements

- Python 3.10+
- CUDA-capable GPU (for Whisper transcription + diarization)
- Ollama with a pulled model (for summarization)
- HuggingFace account with accepted licenses (for diarization)
- ngrok or cloudflared (optional, for remote access)

## Gotchas

- `get_settings()` is LRU-cached — restart required to pick up `.env` changes
- `OLLAMA_THINK=false` is critical — think mode adds thousands of tokens with no benefit for meeting summaries
- Pipeline package (`src/summarization/pipeline/`) exists in codebase but is **not called from export**
- Re-summarize reuses existing transcript when `session.has_transcription=true` AND segments exist; diarization also skips if speakers already saved
- `start.sh` port check uses `connect()` (not `bind()`) to avoid false positives in WSL mirrored mode
- Ollama runs on **Windows host**, Sidekick runs in **WSL** — mirrored networking makes `127.0.0.1:11434` work
- Context budget: `OLLAMA_CONTEXT_LENGTH=40960` suits `qwen3.5:9b` (6.6 GB model, ~9.4 GB headroom for KV cache on 16 GB VRAM). Larger models need reduced context or will split CPU/GPU.
