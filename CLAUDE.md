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
3. Enter title + template
4. Process/export to Obsidian markdown

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
- Rebuilds transcript segments at export time
- Deterministic export behavior, independent of live preview timing

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
- Markdown body has **no top H1 title**
- Includes metadata, summary, and transcript details block

## Data Locations

- DB: `data/sidekick.db`
- Audio: `data/audio/{session_id}.{ext}`
- Sidekick logs/PID: `data/sidekick.log`, `data/sidekick.pid`
- ngrok logs/PID/URL: `data/ngrok.log`, `data/ngrok.pid`, `data/ngrok.url`

## Important Endpoints

- `GET /` main UI
- `GET /recordings` history UI
- `GET /api/recordings` list recordings
- `GET /api/recordings/{id}` recording details
- `PUT /api/recordings/{id}/audio` upload encoded audio
- `GET /api/recordings/{id}/audio` stream/download audio
- `POST /api/recordings/{id}/export-obsidian` authoritative transcribe+summarize+export
- `WS /ws/audio` live stream + optional live preview transcript

## Notes

- Obsidian Sync is near-real-time, not truly instant.
- If `start.sh` reports port in use, `./stop.sh` can stop managed or detected unmanaged Sidekick process.
