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
./use-main.sh             # Stop app, switch to main, restart on the stable branch
./use-dev.sh              # Stop app, switch to dev, restore auto-stashed work, restart
./scripts/switch_sidekick_branch.sh <main|dev> [start args...]
                           # Underlying helper used by use-main/use-dev
```

## File Structure

```
sidekick/
├── start.sh / restart.sh / stop.sh / status.sh / debug.sh / use-main.sh / use-dev.sh
├── .env                              # All runtime config
├── config/
│   └── settings.py                   # Pydantic settings, LRU-cached via get_settings()
│
├── src/
│   ├── main.py                       # FastAPI app entry point
│   ├── api/
│   │   └── routes/
│   │       ├── export.py             # Async export jobs, diarization, transcription pipeline
│   │       ├── sessions.py           # Recording CRUD, workspace/settings APIs, completion/recovery
│   │       └── websocket.py          # Live audio stream; preview-only transport
│   ├── audio/
│   │   └── storage.py                # Audio file management, retained chunk recovery
│   ├── core/
│   │   ├── datetime_utils.py         # Timezone helpers
│   │   ├── markdown_utils.py         # SHARED Obsidian note construction logic
│   │   └── speaker_labels.py         # Humanized fallback labels for unresolved speakers
│   ├── sessions/
│   │   ├── models.py                 # SQLAlchemy models (Summary has processing_duration_seconds, template)
│   │   └── repository.py             # DB CRUD incl. update_segments_speakers() + migrations
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
│       ├── diarize.py                # Legacy standalone pyannote helpers (deprecated runtime path)
│       ├── manager.py                # Transcription orchestration
│       ├── whisper_local.py          # Legacy faster-whisper engine (deprecated runtime path)
│       └── whisperx_local.py         # WhisperX local engine for authoritative file transcription
│   └── workspace_chat/
│       └── service.py                # Experimental meeting assistant persistence/service layer
│
├── web/
│   ├── index.html                    # Main recording UI
│   ├── recordings.html               # History / search UI (opens the shared workspace)
│   ├── settings.html                 # Global settings / feature flags
│   ├── css/styles.css                # Mobile-optimized (13px text, no double scroll)
│   └── js/
│       ├── app.js                    # Recording + upload flow + completion/recovery handoff
│       ├── recordings.js             # History cards, search, optimistic delete, workspace launch
│       ├── audio.js                  # AudioCapture + DAW-style spectrum analyzer
│       ├── network.js                # Shared API/media URL resolver + fetch wrapper for go.sidekickgo.app
│       ├── settings.js               # Global settings page controller
│       └── websocket.js              # WebSocket client, 25s keepalive ping
│
├── data/                             # Runtime data (gitignored)
│   ├── sidekick.db                   # SQLite database
│   ├── sidekick.log                  # App logs (cleared on each start)
│   ├── sidekick.pid                  # Managed process PID
│   └── audio/
│       ├── {session_id}.webm         # Finalized recordings
│       └── chunks/{session_id}/{client_id}/  # Retained chunk backups for recovery
│
└── scripts/
    ├── monitor_ollama.ps1            # PowerShell: Ollama + GPU live watcher
    ├── monitor_export_job.sh         # Bash: poll export job progress
    ├── benchmark_ollama_models.py    # Benchmark raw model latency on transcript chunks
    ├── benchmark_summary.py          # Benchmark full two-pass cohesive summary pipeline
    └── switch_sidekick_branch.sh     # Stop/stash/switch/restart helper for main/dev workflows
```

## Recording + Processing Flow

```
Browser
  │
  ├─[during recording]──► HTTP session start
  │                      └─► WebSocket preview attach (best effort only)
  │
  ├─[recording]─────────► 1s MediaRecorder chunks
  │                      └─► PUT /api/recordings/{id}/audio/chunks/{n}
  │
  ├─[stop recording]────► Recorder stop + wait for chunk uploads
  │                      └─► POST /api/recordings/{id}/complete
  │                            • retries chunk-based recovery server-side
  │                            • marks session ready only when finalized audio exists
  │                            • falls back to full blob upload only if chunk recovery does not complete
  │
  ├─[workspace open]────► Review modal
  │                      • Speakers
  │                      • Summary
  │                      • Settings
  │                      • Transcript
  │                      • Chat (experimental, feature-flagged)
  │
  └─[transcribe/export]─► WhisperX → diarization → transcript versions
                         → cohesive.py summary → Obsidian markdown export
```

### Recording Durability Rules

- Live preview is optional and never authoritative.
- Live recordings are stored as retained chunks first, finalized audio second.
- `data/audio/chunks/{session_id}/{client_id}/` is preserved until the recording is deleted.
- `POST /api/recordings/{id}/complete` is idempotent and will recover finalized audio from retained chunks when possible.
- `POST /api/recordings/{id}/recover-audio` exists for stranded recordings whose final file is missing but chunks still exist.
- `GET /api/recordings/{id}/audio` and workspace/detail reads will auto-attempt chunk recovery if the final file is missing.

## Key Config (.env)

```
# Transcription
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPERX_BATCH_SIZE=16

# Summarization
SUMMARIZATION_BACKEND=ollama
SUMMARIZATION_MEETING_STRUCTURED_ENABLED=false
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
| `strategic_review` | Strategic Review | Strategic alignment, metrics review, decisions, and next steps |
| `working_session` | Working Session | High-detail technical log — decisions, SQL notes, open questions |
| `custom` | Custom | User-provided prompt |

Default template: `meeting`

## UX Conventions

- One primary action per step; no duplicate entry points.
- Recording list cards: `Open` and `Delete` only.
- Export / re-summarize initiated from the workspace, not from cards.
- **Unified Workspace:** History `Open` matches post-recording review. Both support AI Refine, Manual Edit, and Undo.
- **Global Settings:** `/settings` is the app-level feature flag page. Experimental features live there, not in `.env`.
- **Meeting Assistant:** Experimental, off by default, and hidden unless enabled in global settings.
- **Recordings Search:** AI-assisted search is answer-first and transcript-grounded. The recordings page shows a summary answer, follow-up chips, and grouped recording results with `Open Summary` / `Open Transcript`.
- **View Modal Title:** Plain meeting title. Date/time is in the Details section.
- **Rename flow:** Rename from the editable workspace title after opening the recording. History cards stay `Open` / `Delete` only.
- **Workspace header metadata:** Simplified to date and time under the title.
- **Details Section:** Two-column grid — Template (if stored), Recorded, Exported, Length, Processing Time. Updates on version change.
- **Summary Version label:** "Summary Version" (not "Summary") in view modal.
- **Summary Version labels:** `vN (Draft)`, `vN (Latest)`, then descending `vN-1 ... v1`.
- **Transcript Versioning:** Transcript-aware workspaces load one transcript version at a time; summary drafts/saved summaries are tied to that version.
- **Audio player** is at the bottom of the view modal.
- **Card titles:** Plain meeting title (no date prefix); date shown separately.
- **History delete:** Optimistic local removal first, then a non-blocking background refresh. Do not reintroduce a blocking full-list refetch requirement after delete.
- **Mobile optimization:** `13px` text, `1.7` line height, single-unit scroll, Details grid uses `word-break` to prevent horizontal overflow.
- **Workspace tab motion:** Desktop uses the polished hide/reveal motion. Mobile uses a simpler direct-tracking path to avoid touch-scroll jank.
- **Main page guards:** The recording page disables page scroll / pull-to-refresh and blocks in-app navigation while recording.
- **Recording CTA layout:** Main page has a single centered record button; mobile uses easy bottom CTAs for `History` and `Record`.
- Template chooser shows 4 templates in the order above.
- `General Meeting` is default unless explicitly changed.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Main recording UI |
| `GET /recordings` | History UI |
| `GET /api/templates` | List templates with prompts |
| `GET /api/recordings` | List recordings |
| `GET /api/recordings/{id}` | Recording detail (includes latest `summary` + metadata) |
| `GET /api/recordings/{id}/workspace` | Unified workspace payload (recording, transcript versions, summaries, speakers, chat gate) |
| `POST /api/search/recordings` | Cross-recording transcript search with grouped grounded results |
| `POST /api/recordings/{id}/summaries` | Save refined/manual summary to DB and vault |
| `PATCH /api/recordings/{id}/settings` | Update recording title and transcript-version-specific prompt settings |
| `POST /api/summaries/refine` | General AI refinement endpoint |
| `POST /api/recordings/{id}/export-obsidian-job` | Start async export |
| `GET /api/export-jobs/{job_id}` | Poll export job |
| `POST /api/recordings/{id}/transcription-job` | Transcription only |
| `PUT /api/recordings/{id}/audio` | Upload audio |
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Store one retained recording chunk |
| `POST /api/recordings/{id}/audio/finalize` | Explicit chunk finalize (legacy direct finalize path) |
| `POST /api/recordings/{id}/complete` | Authoritative recording completion + readiness check |
| `POST /api/recordings/{id}/recover-audio` | Recover finalized audio from retained chunks |
| `GET /api/recordings/{id}/speakers` | Get speaker cards and clip metadata for workspace review |
| `PUT /api/recordings/{id}/speakers` | Save manual speaker name mapping |
| `GET /api/settings` | Read global app settings / feature flags |
| `PATCH /api/settings` | Update global app settings / feature flags |
| `WS /ws/audio` | Live audio stream |

## Speaker Diarization

**Status**: Live and enabled.

**Features**:
- **Manual Speaker Resolution**: Users identify speakers from the `Speakers` tab in the workspace:
  1. Open a recording
  2. See audio clips for each detected speaker (first 5 seconds of their first utterance)
  3. Enter names in text fields
  4. Save speaker edits, then re-summarize when needed

**Handoff Notes (2026-03-10, latest)**:
- **Model**: `qwen3:8b` (5.2GB, 100% GPU). `OLLAMA_NUM_GPU=99` forces all layers to GPU. `temperature=0.3` added to all calls.
- **Recording lifecycle**: Session start/stop is HTTP-authoritative. WebSocket is preview-only and attach/detach scoped.
- **Audio durability**: Retained chunk storage is the recovery artifact. The finalized `.webm` is derived and can be rebuilt.
- **Stop flow**: Browser waits for chunk uploads, calls `/complete`, retries chunk-based readiness, and only attempts large backup upload after that window.
- **Recovery path**: `/recover-audio` and `ensure_session_audio_path()` can finalize stranded chunk-only recordings later.
- **Global settings**: App-level flags live in the singleton `app_settings` table and `/settings` UI. `workspace_chat_enabled` is DB-backed.
- **Meeting Assistant**: Grounded transcript/summary chat exists behind the experimental global flag and is hidden by default.
- **Recordings search**: Cross-recording AI search remains transcript-grounded, but now returns structured answer metadata (`answer_type`, `reasoning_note`, follow-up queries) plus grouped recording cards for faster navigation.
- **Speaker Identity**: Manual-first. The `Speakers` tab is the product-facing source of truth for speaker naming.
- **Unresolved Speakers**: Raw `SPEAKER_XX` stays visible only in the `Speakers` tab. Transcript and summary views use stable fallback labels: `Attendee`, `Attendee A`, `Attendee B`, etc.
- **Workspace Summary Gate**: Pending speaker review does not block summary generation. Users can summarize before naming every speaker.
- **Settings Tab**: Summary-only. The workspace no longer shows a `People` or attendees field.
- **Settings / Summary coupling**: The Settings tab reflects the selected summary version by default. `Reset` restores that version's template/prompt baseline.
- **Obsidian audit trail**: Exported notes include `Pass 1: System Prompt`, `Pass 1: User Prompt`, `Pass 2: System Prompt`, `Pass 2: User Prompt`, plus a collapsed `Transcript` section.
- **Workspace State Isolation**: Opening a different recording clears unsaved speaker assignments from the previous workspace, and closing the workspace flushes pending settings edits before dismissing the modal.
- **Pipeline Optimizations**:
  - Speech-Aware Diarization: stops at last Whisper timestamp + 5s.
  - Dynamic Context: `num_ctx` calculated from input size.
  - Single-Pass Early Exit: short transcripts (< 3000 chars) skip polish pass.
- **Meeting Summaries**: `General Meeting` uses the cohesive two-pass summarizer with a relevance-first prompt contract. The structured pipeline in `src/summarization/pipeline/` is deprecated and not part of normal summary routing.
- **Audio Quality**: Captures and saves at 48kHz; downsampled to 16kHz for AI.
- **Live analyzer**: The recording page uses a higher-resolution log-spaced spectrum analyzer, not the saved file waveform.
- **Unified View & Refinement:** functionally identical review/view modals.
- **Obsidian Versioning:** exports append `(v2)`, `(v3)`, etc.
- **Markdown Logic:** Consolidated into `src/core/markdown_utils.py`.
- **Database:** Auto-migrations in `repository.py` for `processing_duration_seconds` and `template`.
- **Attendees Compatibility:** `meeting.attendees` and `attendees_snapshot` still exist in the DB/API for backward compatibility, but they are deprecated and no longer drive speaker resolution or summary gating.

**Key implementation notes**:
- Local authoritative transcription uses **WhisperX** with forced alignment and integrated diarization.
- `TRANSCRIPTION_BACKEND=local` disables live preview; only the saved-file pipeline is authoritative for local runs.
- Audio loaded via **PyAV** — no system `ffmpeg` needed.
- Manual resolution endpoint returns clip URLs; frontend plays cached WAV speaker clips directly.
- `src/core/speaker_labels.py` centralizes fallback speaker labels for transcript and summary output.

## Gotchas

- `get_settings()` is LRU-cached — restart required to pick up `.env` changes.
- Remote use through `go.sidekickgo.app` depends on the current Cloudflare quick tunnel URL. The app injects fallback `wss://` and `https://*.trycloudflare.com` transport targets into rendered HTML, and `web/js/network.js` rewrites browser `/api/...` plus recording-media URLs onto that fallback when present.
- `data/sidekick.log` is cleared on each start. If something dies between restarts, capture the log before starting again.
- Starts triggered from the agent tool context can behave differently from a normal interactive shell because background child processes may be reaped by the execution environment. If a restart only fails when launched by the agent, verify it from the user's own shell before debugging the app itself.
- Long recordings should finalize from retained chunks first. If the final audio file is missing but chunks exist, use `/recover-audio` or open the recording to trigger auto-recovery.
- **PWA planning doc:** The current PWA/app-store transition plan lives in `docs/pwa-plan.md` and is mirrored in the Obsidian vault under `Sidekick/pwa-plan.md`.
- **Frontend cache busting:** If `web/index.html` or `web/recordings.html` changes do not appear after a refresh, bump the `?v=` query string on the referenced `/static/css/*.css` or `/static/js/*.js` asset in the HTML entrypoint you touched. This is the first thing to check when the browser appears stuck on old UI code.
- **qwen3 vs qwen3.5 thinking**: `qwen3:8b` properly respects `OLLAMA_THINK=false`. `qwen3.5` models always generate 3000-5000 think tokens per call regardless of this setting — not suppressable at the application level.
- `OLLAMA_NUM_GPU=99` is required — Ollama's auto-estimate offloads ~3 layers to CPU for qwen3:8b (shows 8%/92% split in `ollama ps`). This halves tok/s. Setting `num_gpu=99` forces all layers to GPU.
- `temperature=0.3` is set in all Ollama call options for consistent, factual output (Ollama default is 0.8).
- Mobile: removed `max-height` from internal containers to fix double scrolling.
- Context budget: `OLLAMA_CONTEXT_LENGTH=32768` suits `qwen3:8b` (5.2GB model). Fits 100% in 16GB VRAM. Supports ~2.5+ hours of speech. Larger context or larger models cause CPU spillover.
- Speaker naming no longer depends on an attendees field. If speakers are not mapped manually, user-facing output falls back to `Attendee`, `Attendee A`, `Attendee B`, etc.
- Pull Ollama models from Windows PowerShell, not WSL: `powershell.exe -Command "ollama pull qwen3:8b"`
