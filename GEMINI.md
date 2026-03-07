# Sidekick Project Mandates (Gemini)

This document contains foundational mandates for the Gemini CLI agent working on Sidekick. These instructions take absolute precedence over general defaults.

## Engineering Standards

- **Obsidian First:** All summarization logic must prioritize scannability in Obsidian. Use bullet points (`-`), nested indentation (2 spaces), and blank lines between sections. **No long paragraphs.**
- **Local AI Integrity:** Ensure all AI operations (transcription, diarization, summarization) remain compatible with local backends (faster-whisper, pyannote, Ollama).
- **Mobile optimization:** Maintain the iPhone-friendly UI. Do not re-introduce internal scrollbars or oversized text. Modals must expand and scroll as a single unit.
- **Workspace Motion Split:** Keep the current device-specific tab-row behavior: polished hide/reveal on desktop, simpler direct tracking on mobile for touch-scroll stability.
- **Transactional DB:** Ensure all database updates (segments, summaries, items) are performed within transactions.
- **Versioning:** Never overwrite existing Obsidian notes. Always append ` (v2)`, ` (v3)`, etc., if a summary already exists.

## UI/UX Rules

- **Unified Modals:** The History View and Post-Recording Review modals must share identical functionality (Refine, Edit, Undo, Metadata).
- **Metadata Visibility:** Always display "Exported At" and "Processing Time" for summaries.
- **One Primary Action:** Keep the UI focused. Avoid duplicate buttons or confusing navigation paths.
- **No paragraphs:** Strictly enforce the "bullet-point first" rule in the `SYSTEM_PROMPT` and all template instructions.

## Technical Guardrails

- **Git Safety:** Always check `.gitignore` before `git add .`. Never commit `data/` or `.env`.
- **Port Checking:** Use the `connect()` method for checking port availability to avoid WSL false positives.
- **Ollama Optimization:** Maintain `OLLAMA_CONTEXT_LENGTH=32768` for `qwen3:8b`. Use dynamic `num_ctx` calculation based on input size to speed up Ollama initialization for short meetings. `OLLAMA_NUM_GPU=99` forces all layers to GPU — never remove this.
- **Ollama Think:** Always pass `think: false` to Ollama for summarization. Note: this only works for `qwen3` models. `qwen3.5` models always think regardless.
- **temperature=0.3:** Always pass `temperature: 0.3` to Ollama for consistent, factual summarization output.
- **Speaker Labels:** Manual speaker mapping in the workspace is the source of truth. User-facing transcript and summary output must never expose raw `SPEAKER_XX`; use fallback labels like `Attendee`, `Attendee A`, `Attendee B` when unresolved.
- **Rename Flow:** Recording rename lives in the workspace title after opening a recording; do not reintroduce card-level rename controls.
- **Database Migrations:** Use the `init_db` pattern in `src/sessions/repository.py` to automatically backfill schema changes for existing users.

## Project Context

- **Environment:** Sidekick runs in WSL (Ubuntu) with mirrored networking to a Windows host running Ollama.
- **Audio Loading:** Uses PyAV (bundled FFmpeg) for diarization. No system `ffmpeg` install is required.
- **Markdown Core:** All Obsidian note assembly must go through `src/core/markdown_utils.py`.
