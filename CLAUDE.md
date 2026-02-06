# Claude Code Instructions

## Quick Start

```bash
# One-click start (venv, deps, ngrok, server)
./start.sh

# Or manually:
source venv/bin/activate
python -m src.main
```

## Mobile/External Access

The app requires HTTPS for microphone access on mobile devices. Use the start script which handles ngrok automatically, or run manually:

```bash
~/.local/bin/ngrok http 8000
# Or with a static domain:
~/.local/bin/ngrok http 8000 --domain=your-domain.ngrok-free.app
```

For WSL2, direct IP access from other devices requires port forwarding (already configured if using start.sh with ngrok).

## Project Structure

- `src/main.py` - Entry point (uvicorn server on port 8000)
- `src/api/app.py` - FastAPI app factory
- `src/api/routes/websocket.py` - WebSocket audio streaming
- `src/audio/` - Audio buffering and VAD
- `src/transcription/` - Whisper backends (local/API)
- `src/summarization/` - AI summary backends (Ollama/OpenAI/Claude)
- `web/` - Frontend static files
- `config/settings.py` - App configuration

## Key Commands

```bash
# Run server
python -m src.main

# Install dependencies
pip install -e .

# Run tests
pytest

# Check ngrok status
curl http://localhost:4040/api/tunnels
```

## Environment

- Python 3.12 in `venv/`
- Config via `.env` (see `.env.example`)
- SQLite database in `data/`
