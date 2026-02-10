# Sidekick Agent Handoff

## Current Runtime

```bash
# Managed lifecycle
./start.sh
./start.sh --ngrok
./status.sh
./stop.sh
```

Manual run is still supported:

```bash
source venv/bin/activate
python -m src.main
```

## Product Flow

1. Record audio in browser
2. Stop recording
3. Select template (with optional prompt editing)
4. Process/export to Obsidian markdown with real-time progress

## Architecture: Two Transcription Pipelines

### 1) Live Preview Pipeline (optional UX)

`microphone stream -> websocket chunks -> live preview text`

- Implemented in `src/api/routes/websocket.py`
- Controlled by `.env` setting: `LIVE_TRANSCRIPTION_PREVIEW=true|false`
- Used only for in-session preview on web UI
- Not source of truth for export

### 2) Export Pipeline (authoritative)

`saved audio file -> full transcription -> transcript segments -> summary -> markdown`

- Implemented in `src/api/routes/export.py`
- Uses saved session audio from `data/audio/`
- Async job-based with real-time progress tracking
- Rebuilds transcript segments at export time
- Deterministic export behavior, independent of live preview timing

## Summary Templates

Templates are defined in `src/summarization/prompts.py`:

| Template | Description |
|----------|-------------|
| **1-on-1** | Personal meetings - feedback, goals, development |
| **Standup** | Brief status updates - done, doing, blocked |
| **Strategic Review** | Leadership meetings - reports, feedback, decisions, timelines |
| **Working Session** | Technical work - high detail, decisions, open questions needing consensus |
| **General Meeting** | Standard meeting notes (default) |
| **Brainstorm** | Ideas, themes, promising directions |
| **Interview** | Q&A format with assessment |
| **Lecture** | Study notes with key concepts |
| **Custom** | User-provided prompt |

Templates are editable in the UI before export (click "Show" to view/edit prompt).

UI template chooser order (shown templates only):
1. `meeting` (General Meeting)
2. `strategic_review`
3. `working_session`
4. `standup`
5. `one_on_one`
6. `brainstorm`
7. `custom`

## UX Conventions

- Keep one primary action per step; avoid duplicate entry points for the same action.
- Recording list cards should stay minimal: `View` and `Delete` only.
- Re-summarize/export should be initiated from the recording view context, not cards.
- Download affordances belong in the view modal (`Download Audio`, `Download Transcript`).
- Template chooser should show only the primary 7 templates in the established order.
- Keep `General Meeting` as default unless explicit product changes are requested.

## Key Config (Current)

- `TRANSCRIPTION_BACKEND=local`
- `WHISPER_MODEL_SIZE=large-v3`
- `WHISPER_DEVICE=cuda`
- `WHISPER_COMPUTE_TYPE=float16`
- `SUMMARIZATION_BACKEND=ollama`
- `OLLAMA_MODEL=qwen2.5:14b`
- `OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault`

## Output Format (Current)

- Filename: `YYYY-MM-DD-HHMM - [Title] [Template].md`
- Markdown metadata: Template, Recorded date, Exported date, Duration
- Includes summary and collapsible transcript

## Data Locations

- DB: `data/sidekick.db`
- Audio: `data/audio/{session_id}.{ext}`
- Sidekick logs/PID: `data/sidekick.log`, `data/sidekick.pid`
- ngrok logs/PID/URL: `data/ngrok.log`, `data/ngrok.pid`, `data/ngrok.url`

## Important Endpoints

- `GET /` main UI
- `GET /recordings` history UI
- `GET /api/templates` list templates with prompts
- `GET /api/recordings` list recordings
- `GET /api/recordings/{id}` recording details
- `PUT /api/recordings/{id}/audio` upload encoded audio
- `PUT /api/recordings/{id}/audio/chunks/{index}` chunked upload
- `POST /api/recordings/{id}/audio/finalize` finalize chunked upload
- `GET /api/recordings/{id}/audio` stream/download audio
- `POST /api/recordings/{id}/export-obsidian` sync export (legacy)
- `POST /api/recordings/{id}/export-obsidian-job` async export with progress
- `GET /api/export-jobs/{job_id}` poll export job status
- `WS /ws/audio` live stream + optional live preview transcript

## Recent Changes

- Timer uses wall clock (no drift when tab inactive)
- WebSocket keepalive ping every 25s prevents disconnects
- Chunked audio upload during recording for reliability
- Real-time transcription progress (segment-based)
- Animated progress bar for summarization
- Export date added to markdown output
- Template chooser now shows only primary templates in usage-priority order
- Default template is General Meeting
- History cards removed redundant Export and Download Audio actions
- View modal contains audio download, transcript download, and re-summarize
- Editable template prompts in UI

## Notes

- Obsidian Sync is near-real-time, not truly instant.
- If `start.sh` reports port in use, `./stop.sh` can stop managed or detected unmanaged Sidekick process.
- First startup with large-v3 model may be slow (downloading ~3GB model).
