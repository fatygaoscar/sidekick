# Sidekick - Personal Audio Transcription Assistant

A Python-based web application that provides real-time audio transcription with mode-based behavior, meeting controls, and AI-powered summarization.

## Features

- **Real-time audio transcription** from microphone via browser
- **Mode system** - Work mode with Meeting sub-mode
- **Meeting controls** - Key Start/Key Stop buttons to delineate meeting time
- **Important marker** - Button to flag discussions for emphasis in summaries
- **AI summarization** - Configurable backends (local Ollama or cloud APIs)

## Tech Stack

- **Backend**: Python + FastAPI (async WebSocket support)
- **Frontend**: HTML/CSS/JS (vanilla JS with WebSocket)
- **Transcription**: faster-whisper (local default) / OpenAI Whisper API (cloud option)
- **Summarization**: Ollama (local) / OpenAI / Claude (configurable)
- **Database**: SQLite with SQLAlchemy async

## Quick Start

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e .

# Copy and configure environment
cp .env.example .env
# Edit .env as needed

# Run the application
python -m src.main
```

Then open http://localhost:8000 in your browser.

## Configuration

See `.env.example` for all available configuration options including:
- Transcription backend (local faster-whisper or OpenAI API)
- Summarization backend (Ollama, OpenAI, or Anthropic)
- Audio settings
- API keys

## Usage

1. Start the application
2. Click "Start Session" to begin recording
3. Use "Key Start" to mark the beginning of a meeting
4. Press "Mark Important" to flag key moments
5. Use "Key Stop" to end the meeting
6. Click "Generate Summary" to create an AI-powered summary
