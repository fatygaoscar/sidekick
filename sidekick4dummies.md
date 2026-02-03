# Sidekick for Dummies

A comprehensive guide to understanding the Sidekick project - what it does, how it works, and what we've built.

---

## What is Sidekick?

**Sidekick** is a personal audio transcription assistant. Think of it as your own private meeting recorder that:

1. **Listens** to your microphone in real-time
2. **Transcribes** everything you say into text
3. **Lets you mark** important moments with a button press
4. **Generates AI summaries** of your meetings

It's like having a secretary who takes notes, highlights the important parts, and writes up a summary afterwards.

---

## The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR BROWSER                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │ Microphone  │───▶│ Audio.js    │───▶│ WebSocket Client    │  │
│  │ (your voice)│    │ (captures)  │    │ (sends audio)       │  │
│  └─────────────┘    └─────────────┘    └──────────┬──────────┘  │
│                                                    │             │
│  ┌─────────────────────────────────────────────────┼───────────┐│
│  │              Live Transcript Display            │           ││
│  │  [00:15] Hello everyone, welcome to the meeting │           ││
│  │  [00:23] Today we'll discuss the Q4 roadmap  ◀──┘           ││
│  │  [00:31] [IMPORTANT] The deadline is December 15th          ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                              │ WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PYTHON SERVER                               │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐       │
│  │ Audio Buffer │───▶│    VAD       │───▶│   Whisper    │       │
│  │ (collects)   │    │ (detects     │    │ (transcribes)│       │
│  │              │    │  speech)     │    │              │       │
│  └──────────────┘    └──────────────┘    └──────┬───────┘       │
│                                                  │               │
│  ┌──────────────┐    ┌──────────────┐           │               │
│  │   SQLite     │◀───│   Session    │◀──────────┘               │
│  │   Database   │    │   Manager    │                           │
│  └──────────────┘    └──────────────┘                           │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  Summarization Engine                     │   │
│  │   ┌─────────┐   ┌─────────┐   ┌───────────┐              │   │
│  │   │ Ollama  │   │ OpenAI  │   │ Anthropic │              │   │
│  │   │ (local) │   │ (cloud) │   │  (cloud)  │              │   │
│  │   └─────────┘   └─────────┘   └───────────┘              │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Core Concepts

### Sessions
A **session** is a period of time when Sidekick is listening. You start a session, talk, and end the session. Everything in between is recorded and transcribed.

### Meetings
A **meeting** is a special segment within a session. You mark meetings with "Key Start" and "Key Stop" buttons. This is useful because:
- You might have one long session but multiple meetings
- Summaries are generated per-meeting
- It helps organize your transcripts

### Important Markers
When someone says something critical, hit the "Important" button. This:
- Marks the current moment
- Flags the next 60 seconds of conversation
- Tells the AI summarizer "pay extra attention here"

### Modes
Different contexts for your transcription:
- **Work** - Professional settings (has Meeting submode)
- **Personal** - Personal notes and reminders
- **Brainstorm** - Free-form idea capture

---

## Project Structure Explained

```
sidekick/
├── pyproject.toml          # Python project config & dependencies
├── .env.example            # Template for configuration
├── README.md               # Basic project readme
├── LEARN.md                # Lessons learned during development
│
├── config/
│   ├── settings.py         # All app settings (loaded from .env)
│   └── modes.yaml          # Mode definitions (Work, Personal, etc.)
│
├── src/                    # All Python backend code
│   ├── main.py             # Entry point - starts the server
│   │
│   ├── api/                # Web server (FastAPI)
│   │   ├── app.py          # Creates the FastAPI application
│   │   └── routes/
│   │       ├── websocket.py   # Real-time audio streaming
│   │       ├── sessions.py    # REST API for sessions/meetings
│   │       └── modes.py       # REST API for mode management
│   │
│   ├── audio/              # Audio processing
│   │   ├── capture.py      # Microphone capture (server-side)
│   │   ├── buffer.py       # Collects audio chunks
│   │   └── vad.py          # Voice Activity Detection
│   │
│   ├── transcription/      # Speech-to-text
│   │   ├── base.py         # Interface definition
│   │   ├── whisper_local.py   # Local transcription (faster-whisper)
│   │   ├── whisper_api.py     # Cloud transcription (OpenAI)
│   │   └── manager.py      # Switches between backends
│   │
│   ├── summarization/      # AI summaries
│   │   ├── base.py         # Interface definition
│   │   ├── ollama_backend.py  # Local LLM (Ollama)
│   │   ├── openai_backend.py  # OpenAI GPT
│   │   ├── anthropic_backend.py # Claude
│   │   ├── prompts.py      # Summary prompt templates
│   │   └── manager.py      # Switches between backends
│   │
│   ├── sessions/           # Data management
│   │   ├── models.py       # Database tables (SQLAlchemy)
│   │   ├── repository.py   # Database operations
│   │   └── manager.py      # Session lifecycle
│   │
│   └── core/               # Shared infrastructure
│       ├── events.py       # Event bus (pub/sub)
│       └── exceptions.py   # Custom error types
│
├── web/                    # Frontend (browser)
│   ├── index.html          # Main page
│   ├── css/styles.css      # Styling (dark theme)
│   └── js/
│       ├── app.js          # Main application logic
│       ├── audio.js        # Microphone capture
│       └── websocket.js    # Server communication
│
├── data/                   # Database storage
│   └── sidekick.db         # SQLite database (created on first run)
│
└── tests/                  # Test files (empty for now)
```

---

## How Data Flows

### 1. Audio Capture (Browser → Server)
```
Browser Microphone
    ↓
Web Audio API (AudioWorklet)
    ↓
Convert to 16-bit PCM @ 16kHz
    ↓
WebSocket binary message
    ↓
Python server receives bytes
```

### 2. Transcription (Server)
```
Raw audio bytes
    ↓
Audio Buffer (collects chunks)
    ↓
Voice Activity Detection (is someone speaking?)
    ↓
When ready: send to Whisper
    ↓
Whisper returns text + timestamps
    ↓
Save to database
    ↓
Send back to browser via WebSocket
```

### 3. Summarization (Server)
```
User clicks "Generate Summary"
    ↓
Fetch all transcript segments for meeting
    ↓
Wrap important sections in [IMPORTANT START/END] tags
    ↓
Send to LLM with summary prompt
    ↓
LLM generates structured summary
    ↓
Save to database
    ↓
Display in browser modal
```

---

## Database Schema

Five tables store all the data:

### sessions
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Unique identifier |
| mode | string | "work", "personal", "brainstorm" |
| submode | string | "meeting", "focus", or null |
| started_at | datetime | When session began |
| ended_at | datetime | When session ended |
| is_active | boolean | Currently running? |

### meetings
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Unique identifier |
| session_id | UUID | Parent session |
| title | string | Optional meeting title |
| key_start | datetime | When "Key Start" pressed |
| key_stop | datetime | When "Key Stop" pressed |
| is_active | boolean | Currently running? |

### transcript_segments
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Unique identifier |
| session_id | UUID | Parent session |
| meeting_id | UUID | Parent meeting (optional) |
| text | string | The transcribed text |
| start_time | float | Seconds from session start |
| end_time | float | Seconds from session start |
| is_important | boolean | Flagged as important? |
| confidence | float | Transcription confidence |

### important_markers
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Unique identifier |
| session_id | UUID | Parent session |
| meeting_id | UUID | Parent meeting (optional) |
| marked_at | datetime | When button was pressed |
| duration_seconds | int | How long to flag (default 60) |
| note | string | Optional note |

### summaries
| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Unique identifier |
| meeting_id | UUID | Which meeting |
| content | string | The generated summary |
| backend | string | "ollama", "openai", "anthropic" |
| model | string | Specific model used |
| prompt_tokens | int | Tokens in prompt |
| completion_tokens | int | Tokens in response |

---

## Event System

Components communicate through an **event bus** (pub/sub pattern):

```python
# Events that flow through the system:
AUDIO_CHUNK_RECEIVED      # New audio arrived
AUDIO_VAD_SPEECH_START    # Someone started talking
AUDIO_VAD_SPEECH_END      # Silence detected

TRANSCRIPTION_STARTED     # Whisper processing began
TRANSCRIPTION_SEGMENT     # New text available
TRANSCRIPTION_COMPLETED   # Whisper finished
TRANSCRIPTION_ERROR       # Something went wrong

SESSION_STARTED           # User started session
SESSION_ENDED             # User ended session
SESSION_MODE_CHANGED      # Mode switched

MEETING_STARTED           # Key Start pressed
MEETING_ENDED             # Key Stop pressed

IMPORTANT_MARKED          # Important button pressed

SUMMARIZATION_STARTED     # AI generating summary
SUMMARIZATION_COMPLETED   # Summary ready
SUMMARIZATION_ERROR       # Summary failed

WEBSOCKET_CONNECTED       # Browser connected
WEBSOCKET_DISCONNECTED    # Browser disconnected
```

---

## API Endpoints

### WebSocket
- `WS /ws/audio` - Bidirectional audio/transcript streaming

### REST API
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /health | Health check |
| GET | /api/modes | List all modes |
| GET | /api/modes/current | Get current mode |
| POST | /api/modes/change | Change mode |
| POST | /api/sessions | Start new session |
| GET | /api/sessions/current | Get active session |
| DELETE | /api/sessions/current | End session |
| POST | /api/sessions/{id}/meetings | Key Start |
| PUT | /api/sessions/{id}/meetings/{mid}?action=stop | Key Stop |
| POST | /api/sessions/{id}/important | Mark important |
| POST | /api/meetings/{id}/summarize | Generate summary |
| GET | /api/meetings/{id}/transcript | Get transcript |
| GET | /api/meetings/{id}/summaries | Get all summaries |

---

## Configuration Options

All settings come from environment variables (or `.env` file):

### Server
- `HOST` - Server bind address (default: 0.0.0.0)
- `PORT` - Server port (default: 8000)
- `DEBUG` - Enable debug mode (default: false)

### Database
- `DATABASE_URL` - SQLite connection string

### Transcription
- `TRANSCRIPTION_BACKEND` - "local" or "openai"
- `WHISPER_MODEL_SIZE` - tiny/base/small/medium/large
- `WHISPER_DEVICE` - auto/cpu/cuda
- `OPENAI_API_KEY` - For cloud transcription

### Summarization
- `SUMMARIZATION_BACKEND` - "ollama", "openai", or "anthropic"
- `OLLAMA_HOST` - Ollama server URL
- `OLLAMA_MODEL` - Which Ollama model
- `OPENAI_SUMMARIZATION_MODEL` - Which GPT model
- `ANTHROPIC_API_KEY` - For Claude
- `ANTHROPIC_SUMMARIZATION_MODEL` - Which Claude model

### Audio
- `AUDIO_SAMPLE_RATE` - Sample rate in Hz (default: 16000)
- `VAD_AGGRESSIVENESS` - 0-3, higher = more filtering

---

## Technologies Used

### Backend
| Technology | Purpose |
|------------|---------|
| **Python 3.10+** | Core language |
| **FastAPI** | Web framework with async support |
| **Uvicorn** | ASGI server |
| **SQLAlchemy 2.0** | Database ORM (async) |
| **aiosqlite** | Async SQLite driver |
| **faster-whisper** | Local speech recognition |
| **webrtcvad** | Voice activity detection |
| **Pydantic** | Settings & validation |

### Frontend
| Technology | Purpose |
|------------|---------|
| **Vanilla JavaScript** | No framework needed |
| **Web Audio API** | Microphone capture |
| **AudioWorklet** | Low-latency audio processing |
| **WebSocket** | Real-time communication |
| **CSS Variables** | Theming |

### AI/ML
| Technology | Purpose |
|------------|---------|
| **faster-whisper** | Local transcription (CTranslate2) |
| **OpenAI Whisper API** | Cloud transcription |
| **Ollama** | Local LLM inference |
| **OpenAI GPT** | Cloud summarization |
| **Anthropic Claude** | Cloud summarization |

---

## What's Been Achieved

### ✅ Completed
1. **Full project structure** with clean separation of concerns
2. **Configuration system** with environment variables
3. **Database layer** with async SQLAlchemy
4. **Audio pipeline** with VAD and buffering
5. **Dual transcription backends** (local + cloud)
6. **Triple summarization backends** (Ollama, OpenAI, Claude)
7. **Session/meeting management** with lifecycle tracking
8. **Important marker system** with time windows
9. **Event-driven architecture** for loose coupling
10. **WebSocket streaming** for real-time updates
11. **REST API** for all operations
12. **Complete web UI** with dark theme
13. **Audio visualization** in browser
14. **Export functionality** for transcripts
15. **Summary modal** with copy/download

### 🔜 Future Enhancements
- Speaker diarization (who said what)
- Multiple language support
- Search through transcripts
- Transcript editing
- Custom prompt templates
- Audio playback sync with transcript
- Mobile-responsive design improvements
- Unit and integration tests

---

## Running the Project

```bash
# 1. Navigate to project
cd /home/ozzfa/sidekick

# 2. Activate virtual environment
source venv/bin/activate

# 3. Run the server
python -m src.main

# 4. Open browser
# Go to http://localhost:8000
```

---

## Quick Reference

### Start Recording
1. Open http://localhost:8000
2. Click "Start Session"
3. Allow microphone access
4. Start talking!

### Mark a Meeting
1. Click "Key Start" when meeting begins
2. Talk...
3. Click "Important" for key moments
4. Click "Key Stop" when meeting ends

### Get a Summary
1. End the meeting (Key Stop)
2. Select summary type (Full/Quick/Action Items/Decisions)
3. Click "Generate Summary"
4. Copy or download the result

---

## File Count Summary

| Category | Files | Lines of Code (approx) |
|----------|-------|------------------------|
| Python Backend | 24 | ~2,500 |
| JavaScript Frontend | 3 | ~600 |
| HTML | 1 | ~150 |
| CSS | 1 | ~400 |
| Config/YAML | 2 | ~100 |
| Documentation | 4 | ~700 |
| **Total** | **35** | **~4,450** |

---

*This document was generated as part of the Sidekick project development.*
