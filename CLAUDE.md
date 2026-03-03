# Sidekick Agent Handoff

## Current Runtime

```bash
# Managed lifecycle
./start.sh
./start.sh --ngrok
./start.sh --cloudflare
./restart.sh
./stop.sh
./status.sh
./debug.sh
```

Manual run:

```bash
source venv/bin/activate
python -m src.main
```

## Product Flow

1. Record audio in browser
2. Stop recording
3. Name recording, select template, optionally enter attendees + edit prompt
4. Process/export to Obsidian markdown with real-time progress

## Architecture: Two Transcription Pipelines

### 1) Live Preview Pipeline (optional UX)

`microphone stream -> websocket chunks -> live preview text`

- `src/api/routes/websocket.py`
- Controlled by `.env`: `LIVE_TRANSCRIPTION_PREVIEW=true|false`
- Used only for in-session preview — not the source of truth for export

### 2) Export Pipeline (authoritative)

`saved audio file -> transcription -> diarization -> cohesive summary -> markdown`

- `src/api/routes/export.py`
- Uses saved session audio from `data/audio/`
- Async job-based with real-time progress
- Rebuilds transcript segments at export time from authoritative audio

### 3) Summarization

`transcript -> optional speaker pre-pass -> two-pass cohesive summary -> markdown`

- All templates use `generate_cohesive_summary()` in `src/summarization/cohesive.py`
- **Speaker pre-pass**: if attendees are provided, one dedicated LLM call resolves `SPEAKER_XX` labels to real names before any summarization pass
- **Pass 1 (draft)**: template style contract + attendees context → draft following exact section structure
- **Pass 2 (polish)**: editorial rewrite preserving all `##` headers from draft
- **Retry pass**: triggered if artifacts or repeated sentences detected in pass 2 output
- Context budget: full transcript when it fits; compressed evidence pack fallback for long meetings; chunked extraction for very long meetings

Pipeline modules (`src/summarization/pipeline/`) remain in codebase but are **not invoked from export**.

## Summary Templates

Defined in `src/summarization/prompts.py`:

| Template | Description |
|----------|-------------|
| **General Meeting** | Summary, Key Decisions, Action Items table, Discussion Notes (default) |
| **1-on-1** | Summary, Highlights, Feedback, Goals, Action Items table |
| **Standup** | Per-person Done/Doing/Blocked, Team Blockers, Action Items table |
| **Working Session** | High-detail technical log — decisions, SQL notes, open questions |
| **Custom** | User-provided prompt |

UI template chooser order:
1. `meeting` (General Meeting)
2. `one_on_one`
3. `standup`
4. `working_session`
5. `custom`

Legacy templates (constants kept for backward compat, not in UI): `strategic_review`, `brainstorm`, `interview`, `lecture`

## UX Conventions

- Keep one primary action per step; avoid duplicate entry points.
- Recording list cards: `View` and `Delete` only.
- Re-summarize/export initiated from the recording view modal, not cards.
- Download affordances in the view modal (`Download Audio`, `Download Transcript`).
- Template chooser shows 5 templates in the order above.
- Keep `General Meeting` as default unless explicit product changes requested.
- Attendees field (optional) in both export modals — used for speaker name resolution.

## Key Config (Current)

```
TRANSCRIPTION_BACKEND=local
WHISPER_MODEL_SIZE=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16

SUMMARIZATION_BACKEND=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_THINK=false
OLLAMA_CONTEXT_LENGTH=40960
SUMMARIZATION_TIMEOUT_SECONDS=300

HF_TOKEN=<huggingface_read_token>
DIARIZATION_ENABLED=true

OBSIDIAN_VAULT_PATH=/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault
```

**Model notes**:
- `qwen3.5:9b` = 6.6GB, 100% VRAM on RTX 5070 Ti (16GB), 40K context fits comfortably
- `OLLAMA_THINK=false` is critical — think mode adds thousands of tokens per call for no benefit in summarization
- Larger models (14b+) need reduced `OLLAMA_CONTEXT_LENGTH` to avoid CPU/GPU split (RAM spill = 40%+ CPU usage)
- Ollama runs on **Windows host**; Sidekick in **WSL** with mirrored networking → `127.0.0.1:11434` works directly

## Output Format (Current)

- Filename: `YYYY-MM-DD-HHMM - [Title] [Template].md`
- Metadata block: Template, Recorded date, Exported date, Duration
- Summary body follows template section structure
- Collapsible full transcript (with `SPEAKER_XX:` or resolved real names)

## Data Locations

- DB: `data/sidekick.db`
- Audio: `data/audio/{session_id}.webm`
- Chunk storage: `data/audio/chunks/{session_id}/{client_id}/` (temporary)
- Sidekick logs/PID: `data/sidekick.log`, `data/sidekick.pid`

## Important Endpoints

- `GET /` main UI
- `GET /recordings` history UI
- `GET /api/templates` list templates with prompts
- `GET /api/recordings` list recordings
- `GET /api/recordings/{id}` recording details
- `PUT /api/recordings/{id}/audio` upload full audio blob
- `PUT /api/recordings/{id}/audio/chunks/{index}` chunked upload (requires `X-Client-ID`)
- `POST /api/recordings/{id}/audio/finalize` finalize chunks (requires `X-Client-ID`)
- `GET /api/recordings/{id}/audio` stream/download audio
- `POST /api/recordings/{id}/export-obsidian-job` async export with progress
- `POST /api/recordings/{id}/transcription-job` transcription only (no summary)
- `GET /api/export-jobs/{job_id}` poll export job status
- `GET /api/transcription-jobs/{job_id}` poll transcription job status
- `WS /ws/audio` live stream + optional live preview

## Handoff Notes (2026-03-03)

### Speaker Diarization (implemented and live)

- `src/transcription/diarize.py` — pyannote.audio 4.0.4
- Audio loaded via **PyAV** (bundled FFmpeg) to avoid system FFmpeg dependency
- pyannote 4.x API: result is `DiarizeOutput`; use `.exclusive_speaker_diarization.itertracks(yield_label=True)`
- Three HuggingFace gated repos require license acceptance (one-time):
  - `pyannote/speaker-diarization-3.1`
  - `pyannote/segmentation-3.0`
  - `pyannote/speaker-diarization-community-1`
- `TranscriptSegment.speaker` column stores assigned label per segment
- `repository.update_segments_speakers()` bulk-updates speakers in one transaction
- Re-summarize path runs diarization when: `DIARIZATION_ENABLED=true` AND `has_speakers=False` on existing segments
- Non-blocking on failure — falls back to no-speaker transcript gracefully

### Speaker Name Resolution (attendees field)

- `ExportRequest.attendees: Optional[str]` — comma-separated names (e.g. "Oscar, Pam, Mike")
- `cohesive.py: _resolve_speaker_map()` — dedicated pre-pass LLM call using first ~3000 chars of transcript
- Returns `{"SPEAKER_00": "Oscar", "SPEAKER_01": "Pam", ...}` via JSON extraction
- `_apply_speaker_map()` does string replace across full transcript before Pass 1
- Separates speaker identification from summarization — LLM not asked to do both at once
- UI: Attendees field in both `web/index.html` (naming modal) and `web/recordings.html` (re-summarize modal)

### Summarization LLM call count

For a typical meeting (fits in context):
1. Speaker pre-pass (if attendees provided)
2. Pass 1 — draft
3. Pass 2 — editorial polish
4. Retry (if artifacts/repetition detected — uncommon)

For long meetings (compressed pack fallback):
1. Speaker pre-pass
2. N × chunk extraction (8K char chunks)
3. Pass 1
4. Pass 2

### Working Session template restored

- `working_session` added back to `TEMPLATE_INFO` in `prompts.py`
- Full template content was already present; just re-exposed in UI
- Now shows as 4th option in chooser (before Custom)

### start.sh port check fix (WSL)

- `port_is_available()` now uses `connect()` instead of `bind()` for port availability check
- `bind()` gave false positives in WSL mirrored mode
- `connect()` accurately reflects whether something is actually listening

## Handoff Notes (2026-03-03, earlier)

### Cohesive summarization (single-pass)

- Removed pipeline routing from `export.py`; all templates use `SummarizationManager.summarize()` → `generate_cohesive_summary()`
- Template prompt in `prompts.py` is the "style contract" defining section structure
- Two-pass: draft (follows style contract) → editorial polish (preserves all `##` headers)

### Ollama artifact cleanup

- `ollama_backend.py` strips: complete `<think>...</think>` blocks, orphan `</think>`, control tokens (`<|...|>`)
- `OLLAMA_THINK=false` passed in options to suppress chain-of-thought generation

### Circular import fix (cohesive.py)

- `cohesive.py` uses `TYPE_CHECKING` + string annotations for `StructuredItems` to avoid import cycle with pipeline package

### Model / context history

| Model | VRAM | Context | Notes |
|-------|------|---------|-------|
| qwen3.5:35b-a3b | CPU+GPU split | — | Stalled under load |
| qwen3.5:27b | 54/46 split | — | Too slow |
| qwen3.5:9b-q8_0 | split | — | 10.7GB, split |
| qwen2.5:14b | 100% GPU | 40960 | 18GB total with KV, 44% CPU |
| qwen3.5:4b | 100% GPU | 40960 | 2.5GB, 64K context, quality concerns |
| **qwen3.5:9b** | **100% GPU** | **40960** | **Current — 6.6GB, best balance** |

## Notes

- Obsidian Sync is near-real-time, not truly instant.
- `./stop.sh` can stop managed or detected unmanaged Sidekick processes.
- First startup with large-v3 Whisper model may be slow (downloads ~3GB).
- First export with diarization enabled downloads pyannote models (~1GB, cached after).
- `get_settings()` is LRU-cached — always `./restart.sh` after `.env` changes.
