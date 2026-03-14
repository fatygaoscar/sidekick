# Sidekick

Browser-based meeting recorder that captures audio, builds transcript versions, generates versioned summaries, and exports structured notes to Obsidian — entirely local.

## Features

- **High-fidelity audio** — captures and plays back at 48kHz while downsampling to 16kHz for AI
- **Recovery-first recording pipeline** — live recordings are retained as chunk storage first, finalized audio second
- **Local transcription** via WhisperX large-v3 (CUDA, float16)
- **Speaker alignment + diarization** via WhisperX forced alignment plus pyannote `speaker-diarization-community-1`
- **Consistent speaker reruns** — diarization-only speaker detection now reuses the same gap-backfill attribution rules as initial transcription, so short in-turn utterances do not fall through the repair quality gate
- **Manual-first speaker identification** — review speaker clips and assign names directly in the workspace
- **Robust speaker preview playback** — workspace speaker clips are fetched through the shared network transport and played with Web Audio so remote/fallback loading is less brittle
- **Readable unresolved speakers** — transcript and summary views use `Attendee`, `Attendee A`, `Attendee B`, etc. instead of raw `SPEAKER_XX`
- **Authoritative stop flow** — browser waits for chunk uploads, asks the server to finalize, and only uses a large backup upload as a last resort
- **Auto-recovery** — if a final audio file is missing but retained chunks exist, the app can rebuild it later
- **Eager background processing** — transcription starts in the background as soon as recording stops
- **Workspace-first rename** — open a recording and click the workspace title to rename it
- **Unified workspace** — the same modal is used post-recording and from History
- **Answer-first recordings search** — cross-recording AI search returns a grounded answer, follow-up suggestions, and grouped recording hits
- **Transcript versioning** — retranscription creates transcript versions; summary drafts and saved summaries are tied to the active version
- **Transcript-aware AI revise** — `Ask AI to Revise` can pull grounded detail from the active transcript version using retrieval while treating the active template as flexible guidance instead of blindly editing the summary
- **Prompt Audit Export** — Obsidian exports keep `Meeting Info`, revision history, Pass 1 / Pass 2 prompts, and transcript in collapsed foldable callouts so the note stays short until expanded
- **Clean Latest Notes** — Obsidian latest exports now live in year/month folders with short filenames, while older exported versions are copied into `_versions/`
- **DAW-style analyzer** — the live recording visualizer uses a higher-resolution log-spaced spectrum analyzer while keeping the same minimal style
- **Relevance-first meeting summaries** — `General Meeting` uses the cohesive two-pass summarizer by default, with an optional topic-identified `topic_segmented_v1` pipeline for topic-local notes
- **Smart Versioning** — canonical `vN` summary numbering still exists, but the latest visible note keeps a clean filename while archived exports use `vN.md`
- **Performance Optimizations** — dynamic context sizing and single-pass early exit for ultra-fast short meeting processing
- **Structured templates** — general meeting, strategic review, working session, custom
- **Editable prompts** — customize any template before export
- **Real-time progress** — live percent tracking through transcription and summarization
- **Obsidian export** — writes a stable latest `.md` note, preserves older exported versions in `_versions/`, and opens the latest file with `obsidian://`
- **Global settings page** — app-level feature flags such as the experimental Meeting Assistant
- **Experimental Meeting Assistant** — transcript-aware workspace chat behind a DB-backed feature flag
- **Phone access** — ngrok or Cloudflare tunnel support
- **Speaker preview guardrails** — speaker cards with only unusable legacy micro-fragments are marked `No Preview` instead of exposing a broken play button

## Quick Start

```bash
./start.sh                # Start in background
./start.sh --ngrok        # Start + ngrok public URL
./start.sh --cloudflare   # Start + Cloudflare quick tunnel
./restart.sh              # Restart cleanly
./status.sh               # Check status + public URL
./stop.sh                 # Stop server
./debug.sh                # Unified debug helper
./use-main.sh             # Switch to main and restart
./use-dev.sh              # Switch to dev and restart
```

Then open `http://localhost:8000`.

## Architecture

### Recording Lifecycle

```
Start recording
  ├─ POST /api/sessions                      # authoritative session creation
  ├─ WebSocket attach                        # preview only, best effort
  └─ MediaRecorder emits 1s chunks
       └─ PUT /api/recordings/{id}/audio/chunks/{n}

Stop recording
  ├─ wait for recorder stop + chunk uploads
  ├─ POST /api/recordings/{id}/complete
  │    ├─ chunk-settle retry window on server
  │    ├─ finalize from retained chunks when possible
  │    └─ mark session ready only once finalized audio exists
  └─ fallback full-blob upload only if chunk recovery does not finish in time

Open workspace
  ├─ speakers
  ├─ summary
  ├─ settings
  ├─ transcript
  └─ chat (experimental, feature-flagged)

Search history
  └─ POST /api/search/recordings
       ├─ transcript-grounded retrieval
       ├─ structured answer metadata
       └─ grouped recording results with direct open actions

Later reads
  └─ if finalized audio is missing, Sidekick can rebuild it from retained chunks
```

### Processing Pipeline

```
Browser
  │
  ├─[during recording]──► WebSocket stream ──► Live preview text (optional, not authoritative)
  │
  └─[stop recording]────► Retained chunks + finalized audio readiness
                                │
                     POST /api/recordings/{id}/transcription-job
                     or auto-start from workspace
                                │
                     WhisperX large-v3 (CUDA, float16, batch 16)
                     transcribe → forced alignment → pyannote diarization
                     → shared speaker attribution + review-state derivation
                     → transcript version + aligned segments
                                │
                    POST /api/recordings/{id}/speaker-detection-job
                    → pyannote diarization rerun on existing transcript timing
                    → same shared speaker attribution backfill rules
                                │
                     cohesive.py summary generation
                     → draft summary / saved summary
                                │
                     POST /api/recordings/{id}/export-obsidian-job
                     → Obsidian markdown with prompt audit + transcript
```

### Durability Model

- WebSocket preview is never the source of truth.
- For live recordings, retained chunks are the durable recovery artifact.
- Finalized audio files in `data/audio/{session_id}.{ext}` are derived artifacts.
- `POST /api/recordings/{id}/recover-audio` can recover stranded recordings when chunks still exist.
- Chunk storage is retained until the recording is deleted.

### File Structure

```
sidekick/
├── start.sh / restart.sh / stop.sh / status.sh / debug.sh / use-main.sh / use-dev.sh
├── .env                              # All runtime config
├── config/
│   └── settings.py                   # Pydantic settings, LRU-cached via get_settings()
│
├── src/
│   ├── main.py                       # FastAPI app entry point
│   ├── api/app.py                    # FastAPI app factory + HTML/static serving
│   ├── api/
│   │   └── routes/
│   │       ├── export.py             # Async export jobs, diarization, transcription pipeline
│   │       ├── modes.py              # Session mode/submode APIs
│   │       ├── search.py             # Cross-recording transcript-grounded search APIs
│   │       ├── sessions.py           # Recording CRUD, workspace/settings APIs, completion/recovery
│   │       └── websocket.py          # Live audio stream; preview only
│   ├── audio/
│   │   └── storage.py                # Audio file management, retained chunk recovery
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
│   │   └── pipeline/                 # Deprecated experimental meeting summarization pipeline
│   │       ├── types.py
│   │       ├── chunker.py
│   │       ├── extraction.py
│   │       ├── merger.py
│   │       ├── structurer.py
│   │       ├── narrator.py
│   │       └── pipeline.py
│   └── transcription/
│       ├── diarize.py                # Shared pyannote diarization helpers
│       ├── manager.py                # Transcription orchestration
│       ├── speaker_attribution.py    # Shared speaker mapping + rerun backfill rules
│       ├── whisper_local.py          # Legacy faster-whisper engine (deprecated runtime path)
│       └── whisperx_local.py         # WhisperX local engine for authoritative file transcription
│   └── workspace_chat/
│       └── service.py                # Experimental meeting assistant service
│
├── web/
│   ├── index.html                    # Main recording UI
│   ├── recordings.html               # History / search / shared workspace UI
│   ├── settings.html                 # Global settings page
│   ├── css/styles.css
│   └── js/
│       ├── app.js                    # Recording + upload + completion/recovery flow
│       ├── audio.js                  # AudioCapture, visualizer
│       ├── network.js                # Shared API/media URL resolver + fetch wrapper
│       ├── recording-workspace.js    # Shared workspace controller used from record + history
│       ├── recordings.js             # History list, search, optimistic delete, workspace launch
│       ├── settings.js               # Global settings page controller
│       └── websocket.js              # WebSocket client, 25s keepalive ping
│
├── data/                             # Runtime data (gitignored)
│   ├── sidekick.db                   # SQLite database
│   ├── sidekick.log                  # App logs (cleared on each start)
│   ├── sidekick.pid                  # Managed process PID
│   └── audio/
│       ├── {session_id}.webm         # Finalized recordings
│       └── chunks/{session_id}/{client_id}/  # Retained chunk backups
│
└── scripts/
    ├── backup_to_dropbox.sh          # Backup helper for runtime data
    ├── benchmark_chunking_ab.py      # Compare chunking strategies on real transcripts
    ├── benchmark_ollama_models.py    # Benchmark raw model latency on transcript chunks
    ├── benchmark_summary.py          # Benchmark full two-pass cohesive summary pipeline
    ├── benchmark_utils.py            # Shared benchmark helpers
    ├── monitor_export_job.sh         # Bash: poll export job progress
    ├── monitor_ollama.ps1            # PowerShell: Ollama + GPU live watcher
    ├── monitor_sidekick.sh           # Bash: inline WSL live monitor (GPU, Whisper, Ollama, job, pipeline)
    ├── re_export.py                  # Re-export an existing recording summary
    ├── capture_week_folder_meetings.py
    │                                 # Conservative capture/move tool for legacy `2026 Week ##` meeting folders
    ├── rewrite_meetings_to_modern_export.py
    │                                 # Conservative dry-run-first rewrite of untouched Meetings notes
    └── switch_sidekick_branch.sh     # Stop/stash/switch/restart helper for main/dev workflows
```

## Speaker Repair Notes

- Initial transcription and speaker-detection reruns both flow through `src/transcription/speaker_attribution.py`.
- The rerun path now applies the same neighbor-consensus backfill rule as the initial aligned transcript path.
- That keeps the `repair_quality_gate_passed` check strict without failing reruns on short utterances that sit inside an already-detected speaker turn.

## Configuration

Edit `.env`:

```bash
# Transcription
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPERX_BATCH_SIZE=16

# Speaker Diarization
HF_TOKEN=<your_huggingface_read_token>
DIARIZATION_ENABLED=true

# Summarization
SUMMARIZATION_BACKEND=ollama
SUMMARIZATION_MEETING_STRUCTURED_ENABLED=false
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_THINK=false
OLLAMA_NUM_GPU=99
SUMMARIZATION_TIMEOUT_SECONDS=300
OLLAMA_CONTEXT_LENGTH=32768

# Export
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

### Global Settings

`/settings` is the app-level feature flag page. Today it controls:

- `Meeting Assistant` (`workspace_chat_enabled`)

This setting is stored in the SQLite `app_settings` table, not just in `.env`, so it applies immediately without a restart.

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
- [Summarization Architecture](/home/ozzfa/sidekick/docs/summarization-architecture.md)

### Speaker Diarization Setup

Diarization is free, fully local, and runs on GPU.

1. Create a free account at [huggingface.co](https://huggingface.co)
2. Accept the license for each gated model:
   - `pyannote/speaker-diarization-community-1`
   - language-specific WhisperX alignment models loaded on demand
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
- History delete removes the card locally first, then does a non-blocking background refresh.
- The Settings tab is summary-only and follows the currently selected summary version by default.
- Summary versions are labeled `vN (Draft)`, `vN (Latest Exported)`, `vN (Exported)`, and `vN (Saved Copy)`.
- If you select an older summary version and start `Edit` or `Ask AI to Revise`, Sidekick now branches from that selected version. Any different active draft is preserved in-app as a `Saved Copy` instead of silently becoming the new base.
- `Saved Copy` versions still advance the canonical `vN` sequence, so archived Obsidian version files and workspace version labels stay aligned.
- `Undo` currently applies only to the active draft during the current browser session. Saved versions do not support persistent undo yet. If we add this later, the preferred shape is a separate `Revert to Previous Version` action rather than overloading the current draft-only undo button.
- Latest Obsidian exports now live under `Meetings/YYYY/YYYY-MM/Title.md`. Prior exported versions are copied into `Meetings/YYYY/YYYY-MM/_versions/Title/vN.md`, which keeps the visible month folder clean.
- If you manually rename or move an exported note in Obsidian, Sidekick should preserve that note and write the next export to a fresh managed latest path instead of overwriting your renamed file.
- `Meeting Info` now includes a short `Sidekick ID` plus `Summary Version` and `Transcript Version`. Full machine IDs live in minimal YAML frontmatter at the top of the exported note so Bases/Dataview and Sidekick can still identify the note reliably. That frontmatter intentionally keeps `meeting_date`, `meeting_month`, `template_key`, `recording_duration_minutes`, `tags`, and `sidekick_export_status`.
- Sidekick does not auto-generate Obsidian tags or aliases for exported notes. It writes `tags: []` by default and carries forward any tags you manually add to the latest exported note on future exports.
- `sidekick_export_status` now uses string values (`latest` / `archived`) instead of a boolean checkbox field so it is harder to break Base filtering with an accidental click. Update Bases/Dataview filters to `sidekick_export_status == "latest"`. Use `python3 scripts/repair_obsidian_export_status.py --apply` to repair older exported notes after reviewing the dry run.
- Existing old `Meetings/` notes can be reorganized with `python3 scripts/migrate_obsidian_meetings_layout.py`. It is dry-run by default and only performs a copy-first migration with a backup when `--apply` is passed.
- Legacy `2026 Week ##` meeting folders can be captured into the new month layout with `python3 scripts/capture_week_folder_meetings.py`. It is dry-run by default, only touches notes under those week folders, preserves current note bodies unless a DB-backed note still matches Sidekick strongly enough for a full modern rebuild, and updates `summaries.obsidian_relative_path` only for notes that are still DB-backed. Review `data/week_capture_manifest.json` before any `--apply` run.
- Old exported `Meetings/` notes can be modernized into the current collapsed-callout export style with `python3 scripts/rewrite_meetings_to_modern_export.py`. It is intentionally strict, dry-run by default, file-only, and expected to skip most notes unless the current file still matches the stored Sidekick summary/transcript payload exactly. Review `data/obsidian_rewrite_manifest.json` before any `--apply` run.
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
| `GET /settings` | Global settings page |
| `GET /api/templates` | List templates with prompts |
| `GET /api/modes` | List available modes/submodes |
| `GET /api/modes/current` | Read current mode/submode |
| `POST /api/modes/change` | Change mode/submode |
| `GET /api/recordings` | List recordings |
| `GET /api/recordings/{id}` | Recording detail |
| `GET /api/recordings/{id}/workspace` | Unified workspace payload |
| `POST /api/search/recordings` | Cross-recording transcript-grounded search |
| `PATCH /api/recordings/{id}/settings` | Rename/update active transcript-version settings |
| `POST /api/recordings/{id}/summary-job` | Generate a draft summary for the active transcript version |
| `GET /api/summary-jobs/{job_id}` | Poll summary job |
| `POST /api/recordings/{id}/summary-draft` | Create or reuse editable draft summary |
| `PATCH /api/summary-drafts/{summary_id}` | Save manual draft edits |
| `POST /api/summary-drafts/{summary_id}/revise` | AI-revise a draft summary |
| `POST /api/summary-drafts/{summary_id}/save` | Save draft as a versioned summary and export note |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export job |
| `POST /api/recordings/{id}/transcription-job` | Transcription only (no summary) |
| `GET /api/transcription-jobs/{job_id}` | Poll transcription job |
| `POST /api/recordings/{id}/complete` | Authoritative recording completion + readiness |
| `POST /api/recordings/{id}/recover-audio` | Recover finalized audio from retained chunks |
| `PUT /api/recordings/{id}/audio` | Upload full audio blob (fallback) |
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Upload chunk (needs `X-Client-ID`) |
| `POST /api/recordings/{id}/audio/finalize` | Finalize chunks (needs `X-Client-ID`) |
| `GET /api/recordings/{id}/speakers` | Get speaker cards and clip metadata for workspace review |
| `PUT /api/recordings/{id}/speakers` | Save manual speaker name mapping |
| `GET /api/recordings/{id}/speaker-clips` | Get cached speaker clip metadata |
| `GET /api/recordings/{id}/speaker-clips/{speaker_key}/audio` | Stream cached speaker clip audio |
| `POST /api/recordings/{id}/speaker-mapping` | Legacy/manual speaker mapping helper |
| `POST /api/recordings/{id}/chat/messages` | Experimental grounded workspace chat |
| `POST /api/recordings/{id}/chat/messages/{message_id}/apply` | Apply assistant suggestion into the workspace |
| `GET /api/settings` | Read global feature flags |
| `PATCH /api/settings` | Update global feature flags |
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
- Change visibility rules:
  - Python/backend changes under `src/`, startup scripts, or API/server wiring require a Sidekick restart.
  - Frontend JS/CSS changes under `web/static` paths usually need only a browser refresh.
  - HTML entrypoint changes in `web/index.html`, `web/recordings.html`, or `web/settings.html` usually need a browser refresh, but if the browser keeps serving old assets, bump the `?v=` query string on the referenced `/static/js/*` or `/static/css/*` file and refresh again.
  - Database-backed settings changed through the UI apply immediately unless the specific feature is documented otherwise.
  - Pure documentation changes show immediately in the repo and do not require a restart.
- Remote use through `go.sidekickgo.app` depends on the current Cloudflare quick tunnel URL. The app injects fallback `wss://` and `https://*.trycloudflare.com` transport targets into rendered HTML, and `web/js/network.js` rewrites browser `/api/...` plus recording-media URLs onto that fallback when present. Restart Sidekick after the tunnel changes or if the custom domain starts loading UI but recorder/workspace requests stop reaching the app.
- Windows Update / host reboots can cleanly stop HWiNFO logging, WSL background processes, Cloudflare/ngrok tunnels, and Sidekick itself without leaving an app-level crash in `data/sidekick.log`. If Sidekick should recover automatically after overnight reboots, implement a host-side auto-restart/watchdog script.
- Sidekick currently records microphone input only. Desktop shared-audio capture may be feasible later through browser screen/tab sharing, but true native system-audio capture is intentionally deferred because cross-platform support would require significant OS-specific native integration.
- **`qwen3` vs `qwen3.5` thinking**: `qwen3:8b` properly respects `OLLAMA_THINK=false`. `qwen3.5` models always generate internal thinking tokens regardless of this setting — not suppressable.
- **`OLLAMA_NUM_GPU=99`**: required to prevent Ollama's conservative auto-estimate from offloading layers to CPU.
- `SUMMARIZATION_TIMEOUT_SECONDS` is only a per-call timeout. It does not control model unloading.
- Summarization calls now send Ollama `keep_alive=0`, so the summarization model unloads immediately after each call finishes.
- Transcription jobs now free CUDA memory after completion or error so repeated WhisperX runs do not pin VRAM.
- `SUMMARIZATION_MEETING_STRUCTURED_ENABLED` is deprecated. Normal meeting summaries use the cohesive two-pass summarizer by default.
- `topic_segmented_v1` is a separate opt-in pipeline strategy that first identifies business topics, then extracts each topic separately with deterministic fallback if topic identification fails.
- Re-summarize reuses existing transcript when `session.has_transcription=true` AND segments exist; it does not require attendees.
- Speaker identity is manual-first. The `Speakers` tab is the only product-facing place to map diarization clusters to real names.
- If a speaker is still unresolved, transcript and summary output use fallback labels like `Attendee`, `Attendee A`, `Attendee B`.
- `start.sh` port check uses `connect()` (not `bind()`) to avoid false positives in WSL mirrored mode
- `OLLAMA_HOST=http://127.0.0.1:11434` does not prove you are using Windows host Ollama. In WSL it may also point at a WSL-local Ollama daemon.
- Context budget: `OLLAMA_CONTEXT_LENGTH=32768` suits `qwen3:8b` (5.2 GB model). Fits 100% in 16 GB VRAM. Supports ~2.5+ hours of speech.
- If you intentionally use Windows-host Ollama, pull models from Windows: `powershell.exe -Command "ollama pull qwen3:8b"`
