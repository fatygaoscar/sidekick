# Claude Code Instructions

## Quick Start

```bash
# Start the server
source venv/bin/activate
python -m src.main

# Ollama must be running for summarization
ollama serve
```

## What This App Does

Sidekick is a personal audio transcription assistant with a minimal record-name-summarize-export workflow:

1. **Record** - Click the record button, speak into your microphone
2. **Stop** - Click again to stop recording
3. **Name & Template** - Enter a title and choose a summary template (Meeting, Brainstorm, Interview, Lecture, Custom)
4. **Export** - AI generates a summary and saves to Obsidian vault as markdown

## Project Structure

```
src/
├── main.py                 # Entry point (uvicorn on port 8000)
├── api/
│   ├── app.py              # FastAPI app factory
│   └── routes/
│       ├── websocket.py    # WebSocket audio streaming
│       ├── sessions.py     # Session/recording REST endpoints
│       ├── export.py       # Obsidian export endpoint
│       └── modes.py        # Mode configuration
├── audio/                  # Audio buffering and VAD
├── transcription/          # Whisper backends (local/API)
├── summarization/
│   ├── manager.py          # Summarization orchestration
│   ├── prompts.py          # Template prompts (Meeting, Brainstorm, etc.)
│   └── *_backend.py        # Ollama/OpenAI/Anthropic backends
├── sessions/
│   ├── manager.py          # Session lifecycle
│   ├── repository.py       # Database operations
│   └── models.py           # SQLAlchemy models
└── core/                   # Events system

web/
├── index.html              # Main recording page
├── recordings.html         # Past recordings list
├── css/styles.css          # Berkeley Mono aesthetic
└── js/
    ├── app.js              # Main app logic
    ├── recordings.js       # Recordings page logic
    ├── websocket.js        # WebSocket client
    └── audio.js            # Audio capture & visualizer

config/
└── settings.py             # Pydantic settings from .env
```

## Key API Endpoints

```
GET  /                           # Main recording UI
GET  /recordings                 # Past recordings page
GET  /api/recordings             # List past recordings
GET  /api/recordings/{id}        # Get recording details + transcript
DELETE /api/recordings/{id}      # Delete a recording
POST /api/recordings/{id}/export-obsidian  # Generate summary & export
GET  /api/templates              # Available summary templates
WS   /ws/audio                   # Audio streaming WebSocket
```

## Configuration

Key `.env` settings:

```bash
# Summarization (ollama, openai, or anthropic)
SUMMARIZATION_BACKEND=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Obsidian integration
OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Vault

# Transcription (local or openai)
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=base
```

## Summary Templates

Available in `src/summarization/prompts.py`:
- **Meeting** - Structured notes with decisions and action items
- **Brainstorm** - Ideas, themes, and promising directions
- **Interview** - Q&A format with assessment
- **Lecture** - Study notes with key concepts
- **Custom** - User-provided prompt

## Output Format

Exported files go to Obsidian vault as:
```
YYYY-MM-DD-HHMM - [Title] [Template].md
```

With structure:
```markdown
# 2026-02-07-1540 - Weekly Standup

**Template**: Meeting
**Recorded**: 2026-02-07 15:40
**Duration**: 00:15:42

---

[AI-generated summary]

---

<details>
<summary>Full Transcript</summary>
[Timestamped transcript]
</details>
```

## Database

SQLite at `data/sidekick.db` with tables:
- `sessions` - Recording sessions
- `transcript_segments` - Timestamped text chunks
- `meetings` - Named meetings within sessions
- `summaries` - Generated summaries
- `important_markers` - Flagged moments

## Dependencies

- **Ollama** - Local LLM for summarization (`ollama serve`)
- **faster-whisper** - Local speech-to-text
- Python 3.12 in `venv/`

## Future Features

- Audio file saving (mp3 export)
- Mobile-optimized UI
- Real-time transcript display during recording
