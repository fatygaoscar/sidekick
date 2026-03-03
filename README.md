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

# Restart cleanly (recommended after config/model changes)
./restart.sh

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
SUMMARIZATION_TIMEOUT_SECONDS=600
OLLAMA_CONTEXT_LENGTH=4096

# Export location
OBSIDIAN_VAULT_PATH=/path/to/your/vault
```

### Performance Setup: WSL App + Host Ollama

If Sidekick runs in WSL but summarization stalls on extraction, run Ollama on Windows host and keep the app in WSL.

1. Run/pull model on Windows host:
   - `ollama pull qwen3.5:35b-a3b`
   - Host Ollama tuning (Windows environment variables):
     - `OLLAMA_NUM_PARALLEL=1`
     - `OLLAMA_CONTEXT_LENGTH=4096`
2. From WSL, test host reachability:
   - `curl http://host.docker.internal:11434/api/tags`
   - If needed, test Windows host IP from `/etc/resolv.conf`.
3. Set `.env` in Sidekick:
   - `OLLAMA_HOST=http://host.docker.internal:11434`
   - `OLLAMA_CONTEXT_LENGTH=4096`
   - `SUMMARIZATION_TIMEOUT_SECONDS=600`
4. Restart app:
   - `./restart.sh` (or `./restart.sh --cloudflare`)

Notes:
- `qwen3.5:35b-a3b` is high quality but memory-heavy. Host Ollama avoids WSL RAM cap pressure.
- For 16GB VRAM systems, keep one summarization job at a time and avoid raising context aggressively.

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
- Ollama running locally or on Windows host reachable from WSL (for local summarization)
- ngrok account (optional, for phone access)

## Data Storage

- Database: `data/sidekick.db`
- Audio files: `data/audio/`
- Logs: `data/sidekick.log`

## Debugging

### Unified debug command (recommended)

```bash
# Monitor host Ollama from WSL (uses PowerShell under the hood)
./debug.sh ollama

# Ollama + GPU stats
./debug.sh ollama --gpu --interval 1

# Monitor a specific export job
./debug.sh export <job_id>

# Monitor latest seen export job from logs
./debug.sh export-latest

# Benchmark model latency on a real recording transcript chunk
./debug.sh benchmark --runs 2
```

### Monitor export job progress (WSL)

```bash
# Usage: ./scripts/monitor_export_job.sh <job_id> [interval_seconds] [base_url]
./scripts/monitor_export_job.sh <job_id> 1 http://127.0.0.1:8000
```

### Monitor Ollama activity (PowerShell)

You can run the PowerShell watcher from the same repo stored in WSL:

```powershell
cd \\wsl$\Ubuntu\home\ozzfa\sidekick
powershell -ExecutionPolicy Bypass -File .\scripts\monitor_ollama.ps1
```

With GPU stats:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\monitor_ollama.ps1 -ShowGpu
```

### Benchmark model performance (WSL)

```bash
# Defaults to latest recording and models:
# qwen3.5:35b-a3b,qwen3.5:27b,qwen2.5:14b
./scripts/benchmark_ollama_models.py --runs 2

# Pin recording + models
./scripts/benchmark_ollama_models.py \
  --recording-id <session_id> \
  --models qwen3.5:27b,qwen2.5:14b \
  --chunk-seconds 600 \
  --context-length 3072 \
  --runs 2
```
