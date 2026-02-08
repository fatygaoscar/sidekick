# Sidekick Project Handoff

## Project Overview

**Sidekick** is a personal audio transcription assistant - a Python web application that provides real-time audio transcription from the browser microphone, with meeting controls and AI-powered summarization.

## Current Status: Working (with recent fixes)

The application is functional. Recent bug fixes were applied to address WebSocket disconnection issues.

## Quick Start

```bash
# Activate virtual environment
source venv/bin/activate

# Run the server
python -m src.main

# Open browser to http://localhost:8000
```

## Project Structure

```
sidekick/
├── src/
│   ├── main.py                 # Entry point (uvicorn server)
│   ├── api/
│   │   ├── app.py              # FastAPI app factory & lifespan
│   │   └── routes/
│   │       ├── websocket.py    # WebSocket audio streaming endpoint
│   │       ├── sessions.py     # REST API for sessions
│   │       └── modes.py        # REST API for modes
│   ├── audio/
│   │   ├── buffer.py           # Audio buffering for transcription
│   │   ├── vad.py              # Voice Activity Detection (webrtcvad)
│   │   └── capture.py          # Audio capture utilities
│   ├── transcription/
│   │   ├── manager.py          # Transcription engine manager
│   │   ├── whisper_local.py    # Local faster-whisper backend
│   │   ├── whisper_api.py      # OpenAI Whisper API backend
│   │   └── base.py             # Base transcription interface
│   ├── summarization/
│   │   ├── manager.py          # Summarization manager
│   │   ├── ollama_backend.py   # Local Ollama backend
│   │   ├── openai_backend.py   # OpenAI API backend
│   │   ├── anthropic_backend.py # Claude API backend
│   │   └── prompts.py          # Summarization prompts
│   ├── sessions/
│   │   ├── manager.py          # Session state management
│   │   ├── models.py           # SQLAlchemy models
│   │   └── repository.py       # Database operations
│   └── core/
│       ├── events.py           # Event bus for internal messaging
│       └── exceptions.py       # Custom exceptions
├── web/                        # Frontend (vanilla JS)
│   ├── index.html
│   ├── css/styles.css
│   └── js/
│       ├── app.js              # Main UI logic
│       ├── audio.js            # Browser audio capture
│       └── websocket.js        # WebSocket client
├── config/
│   └── settings.py             # Pydantic settings (from .env)
├── data/                       # SQLite database location
├── tests/                      # Test directory (minimal)
├── .env                        # Configuration (copy from .env.example)
├── .env.example                # Configuration template
├── pyproject.toml              # Project dependencies
└── README.md                   # User documentation
```

## Recent Changes (Bug Fixes Applied)

### Issue: WebSocket Disconnecting - No Transcription

**Root Cause:** Multiple issues causing silent WebSocket failures:
1. No exception handling in WebSocket endpoint
2. VAD initialization could fail silently
3. TranscriptionManager not pre-initialized (lazy loading could fail)
4. Silent exception swallowing in `_send_json()`

### Fixes Applied

**1. `src/api/routes/websocket.py`**
- Added `traceback` import
- Added logging to `AudioWebSocketHandler.__init__()` showing sample rate and init status
- Changed `_send_json()` from silent `pass` to logging errors
- Wrapped entire WebSocket endpoint in try-except with full traceback logging

**2. `src/api/app.py`**
- Added startup logging (`[Startup] Initializing Sidekick...`, etc.)
- Pre-initialize TranscriptionManager at startup with `await app.state.transcription_manager.initialize()`
- Added shutdown logging

### Expected Console Output (Healthy Startup)

```
[Startup] Initializing Sidekick...
[Startup] Initializing database...
[Startup] Database initialized
[Startup] Initializing transcription manager (backend=local)...
[Startup] Transcription manager initialized successfully
[Startup] Sidekick ready!
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

## Dependencies

Key dependencies (see `pyproject.toml` for full list):
- **FastAPI** - Web framework
- **uvicorn** - ASGI server
- **faster-whisper** - Local transcription (requires **torch**)
- **webrtcvad** - Voice activity detection
- **SQLAlchemy + aiosqlite** - Async database
- **ollama/openai/anthropic** - Summarization backends

**Note:** `torch` is not in pyproject.toml but is required for local transcription. Install with:
```bash
pip install torch
```

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_BACKEND` | `local` | `local` (faster-whisper) or `openai` |
| `WHISPER_MODEL_SIZE` | `base` | Whisper model: tiny, base, small, medium, large |
| `SUMMARIZATION_BACKEND` | `ollama` | `ollama`, `openai`, or `anthropic` |
| `OPENAI_API_KEY` | - | Required if using OpenAI |
| `ANTHROPIC_API_KEY` | - | Required if using Claude |
| `AUDIO_SAMPLE_RATE` | `16000` | Must be 8000, 16000, 32000, or 48000 (VAD requirement) |

## Known Issues / TODOs

1. **torch not in dependencies** - Should be added to pyproject.toml or documented as manual install
2. **webrtcvad deprecation warning** - Uses deprecated `pkg_resources` (cosmetic, still works)
3. **No tests** - `tests/` directory exists but is empty
4. **Error handling** - While improved, could add more specific error types

## How to Test

1. Start server: `python -m src.main`
2. Open http://localhost:8000
3. Click "Start Session"
4. Allow microphone access
5. Speak - should see transcription appear
6. Check server console for any errors

## Architecture Notes

- **WebSocket flow**: Browser captures audio -> sends to `/ws/audio` -> VAD filters speech -> buffer accumulates -> transcription manager processes -> result saved to session -> sent back to client
- **Event bus**: Internal pub/sub for decoupled components (`src/core/events.py`)
- **Async throughout**: All I/O is async (database, transcription, WebSocket)

## Files Modified in This Session

1. `src/api/routes/websocket.py` - Exception handling and logging
2. `src/api/app.py` - Pre-initialization and startup logging
