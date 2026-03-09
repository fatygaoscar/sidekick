# Sidekick

Browser-based meeting recorder that transcribes audio, identifies speakers, and exports structured notes to Obsidian — entirely local.

## Features

- **High-fidelity audio** — captures and plays back at 48kHz (DVD quality) while downsampling to 16kHz for AI
- **Local transcription** via faster-whisper large-v3 (CUDA)
- **Speaker diarization** via pyannote.audio 3.1 — speech-aware optimization skips silent ends
- **Manual-first speaker identification** — review speaker clips and assign names directly in the workspace
- **Readable unresolved speakers** — transcript and summary views use `Attendee`, `Attendee A`, `Attendee B`, etc. instead of raw `SPEAKER_XX`
- **Eager background processing** — transcription starts in the background as soon as recording stops, so export skips Whisper when you click Process
- **Workspace-first rename** — open a recording and click the workspace title to rename it
- **History Workspace** — open past recordings from history and use the same workspace for summary review, transcript, speakers, and settings
- **Unified Review/View** — functionally identical modals for new and past recordings (Refine, Edit, Undo)
- **Prompt Audit Export** — Obsidian exports include the exact Pass 1 / Pass 2 prompts used plus a collapsed transcript section
- **DAW-style analyzer** — the live recording visualizer uses a higher-resolution log-spaced spectrum analyzer while keeping the same minimal style
- **Obsidian-Optimized Formatting** — summaries use nested bullet points and clean spacing for maximum scannability
- **Smart Versioning** — Obsidian exports append `(v2)`, `(v3)`, etc., to prevent overwriting existing notes
- **Performance Optimizations** — dynamic context sizing and single-pass early exit for ultra-fast short meeting processing
- **Structured templates** — general meeting, strategic review, working session, custom
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
│  Humanized transcript → Two-pass summary → Obsidian .md    │
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
                     {title, template, custom_prompt}
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
                    │  [MM:SS] Name/Attendee A: text             │
                    └───────────┬────────────────────────────────┘
                                │
                    ┌───────────▼────────────────────────────────┐
                    │  SUMMARIZATION  (cohesive.py)              │
                    │                                            │
                    │  Pass 1: Draft                             │
                    │    system: template style contract         │
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
                    Metadata block + summary
                    + Pass 1 / Pass 2 prompt code blocks
                    + collapsible transcript
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
│   │   ├── datetime_utils.py         # Timezone helpers
│   │   ├── markdown_utils.py         # Shared Obsidian note construction logic
│   │   └── speaker_labels.py         # Humanized fallback labels for unresolved speakers
│   ├── sessions/
│   │   ├── models.py                 # SQLAlchemy models (Session, Meeting,
│   │   │                             #   TranscriptSegment w/ speaker, StructuredItem)
│   │   └── repository.py             # DB CRUD incl. update_segments_speakers()
│   ├── summarization/
│   │   ├── cohesive.py               # Two-pass summary with humanized unresolved speakers
│   │   ├── manager.py                # Summarization orchestration
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
│   ├── index.html                    # Main recording UI
│   ├── recordings.html               # History / re-summarize UI
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
    ├── monitor_sidekick.sh           # Bash: inline WSL live monitor (GPU, Whisper, Ollama, job, pipeline)
    ├── monitor_ollama.ps1            # PowerShell: Ollama + GPU live watcher
    ├── monitor_export_job.sh         # Bash: poll export job progress
    ├── benchmark_ollama_models.py    # Benchmark raw model latency on transcript chunks
    └── benchmark_summary.py         # Benchmark full two-pass cohesive summary pipeline
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
OLLAMA_MODEL=qwen3:8b
OLLAMA_THINK=false
OLLAMA_NUM_GPU=99
SUMMARIZATION_TIMEOUT_SECONDS=300
OLLAMA_CONTEXT_LENGTH=32768

# Export
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

### Model Selection Guide

| Model | VRAM | Quality | Notes |
|-------|------|---------|-------|
| `qwen3:4b` | ~2.4 GB | Good | Respects `think:False`, 100% GPU |
| `qwen3:8b` | ~5.2 GB | Better | **Recommended** — respects `think:False`, ~30s/25K chars |
| `qwen3.5:9b` | ~6.6 GB | Better | Always generates think tokens (not suppressable) — slower |
| `qwen2.5:14b` | ~10.3 GB | Best local | Spills to RAM at 32K context (requires 24GB VRAM for full GPU) |

`qwen3:8b` with `OLLAMA_CONTEXT_LENGTH=32768` is the recommended default for 16GB VRAM systems.

**Important**: Use `qwen3` models (not `qwen3.5`). `qwen3.5` models always generate internal thinking tokens regardless of `OLLAMA_THINK=false`, wasting 3000-5000 tokens per call. `qwen3` models properly suppress thinking.

**`OLLAMA_NUM_GPU=99`**: Ollama's auto-estimate conservatively offloads some layers to CPU. Setting `num_gpu=99` forces all layers to GPU and roughly doubles throughput.

### Ollama Runtime

Sidekick supports both:

- WSL-local Ollama
- Windows-host Ollama

Use whichever is stable enough on your machine. Windows host was previously preferred for better RAM headroom, but WSL-local Ollama is acceptable if performance is good enough.

Important:

- Sidekick only uses `OLLAMA_HOST`
- `OLLAMA_HOST=http://127.0.0.1:11434` is ambiguous in WSL
- that address may hit either a WSL Ollama daemon or the Windows host runtime, depending on which process owns the port

Use these commands to verify which runtime is active:

```bash
# WSL daemon + loaded models
ps -ef | rg '[o]llama'
ollama ps

# Windows host daemon + loaded models
powershell.exe -NoProfile -Command "Get-Process ollama -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path"
powershell.exe -NoProfile -Command "ollama ps"
```

Detailed runtime guidance:

- [Ollama Runtime Modes](/home/ozzfa/sidekick/docs/OLLAMA_RUNTIME_MODES.md)
- [Host Ollama Setup](/home/ozzfa/sidekick/docs/host_ollama_setup.md)

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
| **Strategic Review** | Leadership and planning reviews — metrics, direction changes, milestones, next steps |
| **Working Session** | Technical work — decisions, SQL notes, open questions, high detail |
| **Custom** | User-provided prompt — targeted extraction for a specific audience or artifact |

All templates are editable before export from the workspace Settings tab.

## Usage

1. **Record** — Click the microphone button to start
2. **Stop** — Click again to stop recording
3. **Title** — Give the recording a name
4. **Speakers** *(optional but recommended)* — In the workspace `Speakers` tab, name any detected speakers you recognize
5. **Template** — Choose a template and edit or reset the prompt in the summary-only Settings tab if needed
6. **Generate Summary** — Watch real-time progress through transcription and summarization
7. **Save to Obsidian** — Save the draft and open the exported note

## Workspace UX

- Recording cards stay simple: `Open` and `Delete` only.
- Main page uses a single centered `Record` action; mobile surfaces easy bottom CTAs for `History` and `Record`.
- Main page prevents page scrolling / pull-to-refresh and guards against leaving while recording.
- Rename happens from the editable workspace title after opening a recording.
- The Settings tab is summary-only and follows the currently selected summary version by default.
- Summary versions are labeled `vN (Draft)`, `vN (Latest)`, then descending `vN-1 ... v1`.
- Desktop uses the polished tab-row hide/reveal motion; mobile uses a simpler direct-tracking path for smoother touch scrolling.

## Backups

If you want reliable recovery, the most important things to back up are:

- `data/sidekick.db` — meetings, transcripts, summaries, speaker mappings, version history, and prompt audit fields
- `data/audio/` — finalized source recordings for playback and reprocessing
- your Obsidian vault — exported notes
- `.env` — only in a private secure backup

Things you usually do not need to back up:

- `data/audio/chunks/`
- `data/*.log`
- `data/*.pid`
- tunnel runtime files such as Cloudflare / ngrok state

Recommended approach:

- keep Sidekick's live `data/` folder where it is
- back up copies or snapshots into Dropbox, another private repo, or encrypted cloud storage
- avoid syncing the live SQLite file directly while the app is writing to it

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Main recording UI |
| `GET /recordings` | History UI |
| `GET /api/templates` | List templates with prompts |
| `GET /api/recordings` | List recordings |
| `GET /api/recordings/{id}` | Recording detail |
| `PATCH /api/recordings/{id}/title` | Rename a recording |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export job |
| `POST /api/recordings/{id}/transcription-job` | Transcription only (no summary) |
| `GET /api/transcription-jobs/{job_id}` | Poll transcription job |
| `PUT /api/recordings/{id}/audio` | Upload full audio blob (fallback) |
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Upload chunk (needs `X-Client-ID`) |
| `POST /api/recordings/{id}/audio/finalize` | Finalize chunks (needs `X-Client-ID`) |
| `GET /api/recordings/{id}/speakers` | Get speaker cards and clip metadata for workspace review |
| `PUT /api/recordings/{id}/speakers` | Save manual speaker name mapping |
| `WS /ws/audio` | Live audio stream |

## Debugging

```bash
# Inline WSL live monitor — GPU, Whisper, Ollama, export job, pipeline steps
./debug.sh
./debug.sh monitor            # same

# Monitor a specific export job
./debug.sh export
./debug.sh export <job_id>

# Live Ollama + GPU stats (launches PowerShell watcher)
./debug.sh ollama --gpu

# Tail app logs (optional grep filter)
./debug.sh logs
./debug.sh logs "speaker"

# Watch pipeline step timing lines only
./debug.sh pipeline

# Benchmark model latency on a real recording
./debug.sh benchmark --runs 2
./debug.sh benchmark-summary
```

## Requirements

- Python 3.10+
- CUDA-capable GPU (for Whisper transcription + diarization)
- Ollama with a pulled model (for summarization)
- HuggingFace account with accepted licenses (for diarization)
- ngrok or cloudflared (optional, for remote access)

## Gotchas

- `get_settings()` is LRU-cached — restart required to pick up `.env` changes
- Remote recording through `go.sidekickgo.app` depends on the current Cloudflare quick tunnel URL. The app injects fallback `wss://` and `https://*.trycloudflare.com` transport targets into the HTML at render time, so restart Sidekick after the tunnel changes or if the custom domain starts loading UI but recorder/workspace requests stop reaching the app.
- **`qwen3` vs `qwen3.5` thinking**: `qwen3:8b` properly respects `OLLAMA_THINK=false`. `qwen3.5` models always generate internal thinking tokens regardless of this setting — not suppressable.
- **`OLLAMA_NUM_GPU=99`**: required to prevent Ollama's conservative auto-estimate from offloading layers to CPU.
- `SUMMARIZATION_TIMEOUT_SECONDS` is only a per-call timeout. It does not control model unloading.
- Summarization calls now send Ollama `keep_alive=0`, so the summarization model unloads immediately after each call finishes.
- Pipeline package (`src/summarization/pipeline/`) exists in codebase but is **not called from export**
- Re-summarize reuses existing transcript when `session.has_transcription=true` AND segments exist; it does not require attendees.
- Speaker identity is manual-first. The `Speakers` tab is the only product-facing place to map diarization clusters to real names.
- If a speaker is still unresolved, transcript and summary output use fallback labels like `Attendee`, `Attendee A`, `Attendee B`.
- `start.sh` port check uses `connect()` (not `bind()`) to avoid false positives in WSL mirrored mode
- `OLLAMA_HOST=http://127.0.0.1:11434` does not prove you are using Windows host Ollama. In WSL it may also point at a WSL-local Ollama daemon.
- Context budget: `OLLAMA_CONTEXT_LENGTH=32768` suits `qwen3:8b` (5.2 GB model). Fits 100% in 16 GB VRAM. Supports ~2.5+ hours of speech.
- If you intentionally use Windows-host Ollama, pull models from Windows: `powershell.exe -Command "ollama pull qwen3:8b"`
