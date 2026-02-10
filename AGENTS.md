# Sidekick Agent Handoff

Quick reference for AI agents working on this codebase.

## Runtime Commands

```bash
./start.sh          # Start server
./start.sh --ngrok  # Start with public URL
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
│   └── manager.py        # Summarization orchestration
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
| `PUT /api/recordings/{id}/audio/chunks/{n}` | Upload audio chunk |
| `POST /api/recordings/{id}/audio/finalize` | Finalize chunked upload |

## Export Progress Flow

1. Frontend calls `POST /export-obsidian-job` → returns `job_id`
2. Frontend polls `GET /export-jobs/{job_id}` every 900ms
3. Backend updates job state as transcription segments complete
4. Transcription progress: real segment-based (0-100%)
5. Summarization progress: animated/indeterminate (LLM timing unpredictable)

## Recent Improvements

- **Timer**: Uses `Date.now()` wall clock, no drift when tab inactive
- **WebSocket**: Ping every 25s, unlimited reconnects, visibility-change reconnect
- **Audio**: Chunked upload during recording, fallback to full blob
- **Progress**: Real segment-based transcription progress
- **Templates**: Only primary templates shown in chooser, ordered for common usage; prompts still editable before export
- **History UX**: Removed redundant card actions (no card-level Export/Download Audio)
- **View Pane Actions**: Audio download kept in view modal, added transcript download button, re-summarize remains primary action
- **Export**: Includes both recorded and exported timestamps
