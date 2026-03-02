# Sidekick - Personal Audio Transcription Assistant

A Python-based web application for recording, transcribing, and summarizing meetings with AI-powered templates and real-time progress tracking.

## Features

- **Browser-based recording** with real-time audio visualization
- **Local transcription** using faster-whisper (large-v3 model, CUDA accelerated)
- **Smart templates** for different meeting types (1-on-1, standup, strategic review, working session, etc.)
- **Editable prompts** - customize any template before export
- **Real-time progress** - watch transcription progress as segments complete
- **Obsidian integration** - exports directly to your vault with proper formatting
- **Mobile-friendly** - works on phone via ngrok tunnel

## Tech Stack

- **Backend**: Python + FastAPI (async WebSocket support)
- **Frontend**: Vanilla HTML/CSS/JS
- **Transcription**: faster-whisper (local GPU) / OpenAI Whisper API (cloud option)
- **Summarization**: Ollama (local) / OpenAI / Claude (configurable)
- **Database**: SQLite with SQLAlchemy async

## Quick Start

```bash
# Start managed background server
./start.sh

# Start with ngrok public URL for phone access
./start.sh --ngrok

# Check status
./status.sh

# Stop server
./stop.sh
```

Then open http://localhost:8000 in your browser.

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
# Transcription
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda

# Summarization
SUMMARIZATION_BACKEND=ollama
OLLAMA_MODEL=qwen3.5:35b-a3b

# Export location
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

## Usage

1. **Record** - Click the record button to start capturing audio
2. **Stop** - Click again to stop recording
3. **Select Template** - Choose from:
   - 1-on-1, Standup, Strategic Review, Working Session
   - General Meeting, Brainstorm, Interview, Lecture
   - Custom (write your own prompt)
4. **Edit Prompt** (optional) - Click "Show" to view and customize the template
5. **Process** - Watch real-time progress as your recording is transcribed and summarized
6. **Open in Obsidian** - Click to jump directly to your new note

## Templates

| Template | Best For |
|----------|----------|
| **1-on-1** | Manager/report meetings - feedback, goals, development |
| **Standup** | Daily status updates - brief, blockers-focused |
| **Strategic Review** | Leadership meetings - reports, decisions, timelines |
| **Working Session** | Technical work - high detail, decision tracking, open questions |
| **General Meeting** | Standard meetings with action items |
| **Brainstorm** | Idea generation sessions |
| **Interview** | Q&A format with assessment |
| **Lecture** | Learning sessions with key concepts |

## Architecture

Two transcription pipelines:

1. **Live Preview** (optional): Real-time transcription preview while recording
2. **Export Pipeline** (authoritative): Full transcription from saved audio at export time

This ensures consistent, high-quality exports regardless of network conditions during recording.

### Multi-Stage Summarization Pipeline

The export pipeline uses a multi-stage approach for improved accuracy:

1. **Chunking**: Splits transcript into 8-12 minute segments
2. **Extraction**: Extracts actions, decisions, risks, questions, and follow-ups from each chunk
3. **Deduplication**: Merges duplicate items across chunks
4. **Structuring**: Assigns IDs (A-001, D-001, R-001, Q-001, F-001) and validates schema
5. **Narration**: Generates narrative summary referencing all extracted items

Output includes both structured tables and flowing narrative.

## Requirements

- Python 3.10+
- CUDA-capable GPU (for local transcription)
- Ollama running locally (for local summarization)
- ngrok account (optional, for phone access)

## Data Storage

- Database: `data/sidekick.db`
- Audio files: `data/audio/`
- Logs: `data/sidekick.log`
