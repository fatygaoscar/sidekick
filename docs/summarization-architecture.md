# Summarization Architecture

Engineer-facing reference for how Sidekick turns a transcript version into a draft or saved summary.

This document is the source of truth for the current summarization architecture. If you change the summarization flow, prompt assembly, transcript shaping, summary persistence model, or the default runtime path, update this file in the same change.

## Scope

This document covers the active production summarization path:

- transcript version selection
- transcript assembly from stored segments
- template and prompt resolution
- cohesive two-pass summarization
- draft summary persistence
- refine and save flows
- export-time behavior

This document does not treat `src/summarization/pipeline/` as the default architecture. That package still exists, but it is not the normal product path for meeting summaries.

## High-Level Flow

Normal workspace flow:

1. The workspace persists title/template/custom prompt settings.
2. The user starts `POST /api/recordings/{id}/summary-job`.
3. The backend selects a transcript version and rebuilds a timestamped transcript from stored segments.
4. `SummarizationManager.summarize()` runs the cohesive summarizer.
5. The result is written as a `draft` summary tied to that transcript version.
6. The user can manually edit, AI-revise, or save the draft.
7. Saving promotes the draft to a `saved` summary and optionally writes the Obsidian note.

Legacy/direct export flow:

1. The user starts `POST /api/recordings/{id}/export-obsidian-job`.
2. The backend transcribes if needed, summarizes using the same manager, creates a draft summary row, and defers vault write until save.

## Core Components

### API and orchestration

- `src/api/routes/sessions.py`
  - `PATCH /api/recordings/{id}/settings` persists workspace title/template/custom prompt state.
- `src/api/routes/export.py`
  - `_run_summary_job()` is the standard summary-draft generation path.
  - `_run_export_pipeline()` is the direct export path that also performs summarization when needed.
  - `/summary-drafts/*` endpoints cover manual edit, AI revise, and save.

### Summarization engine

- `src/summarization/manager.py`
  - backend selection
  - timeout handling
  - normal summarize path
  - summary refinement path
- `src/summarization/cohesive.py`
  - transcript preprocessing
  - context selection
  - chunked extraction fallback
  - pass 1 draft generation
  - pass 2 editorial polish
  - retry behavior
- `src/summarization/prompts.py`
  - template contracts and display metadata
- `src/summarization/ollama_backend.py`
  - normal local backend implementation

### Persistence and export

- `src/sessions/models.py`
  - `Meeting`
  - `TranscriptVersion`
  - `Summary`
- `src/sessions/repository.py`
  - transcript version creation/backfill
  - draft replacement
  - draft-to-saved promotion
- `src/core/markdown_utils.py`
  - final Obsidian markdown assembly

## Data Model

### Meeting

`Meeting` stores recording-level defaults:

- `title`
- `template_key`
- `custom_prompt`

These are the defaults for new or legacy transcript-version state, but they are not enough on their own to explain a specific summary after transcript versioning was introduced.

### TranscriptVersion

`TranscriptVersion` is the workspace state that matters for summarization. It stores:

- version number
- parent version
- transcription/diarization metadata
- `template_key`
- `custom_prompt`
- speaker-review state

Every meaningful summary operation is expected to be scoped to one transcript version.

### Summary

`Summary` stores both product output and audit metadata:

- `status`: `draft` or `saved`
- `source_type`: `generated`, `resummarized`, `manual_edit`, `ai_revised`, etc.
- `parent_summary_id`: lineage from previously saved versions
- `transcript_version_id`
- `template` and `template_key`
- `custom_prompt`
- `processing_duration_seconds`
- `pass1_system_prompt`, `pass1_user_prompt`
- `pass2_system_prompt`, `pass2_user_prompt`
- `workflow_data_json`
- save/export metadata for Obsidian

This is why saved summaries are reproducible and auditable after the fact.

## Input Selection Before Summarization

### Settings resolution

The workspace persists settings through `PATCH /api/recordings/{id}/settings`.

Rules:

- Title updates live on the `Meeting`.
- If a transcript version is selected, `template_key` and `custom_prompt` are stored on that `TranscriptVersion`.
- If no transcript version exists yet, the prompt settings fall back to the `Meeting`.

This makes summary configuration transcript-version-specific once transcription exists.

### Transcript version selection

`_run_summary_job()` resolves the transcript version in this order:

1. explicit `transcript_version_id` from the request
2. latest transcript version for the session

If no ready transcript version exists, summary generation is rejected.

### Transcript assembly

The model does not summarize raw DB rows directly. `src/api/routes/export.py::_segments_to_transcript()` converts stored segments into a timestamped plain-text transcript:

- format: `[MM:SS] Speaker: text`
- preserves `[IMPORTANT]` markers
- orders segments by start time
- computes meeting duration from the latest segment end time

This assembled transcript string is the main input to the summarizer.

## Speaker Handling

Speaker handling is intentionally conservative and user-facing:

1. Stored segment speaker labels are used when available.
2. Very short unlabeled gaps may receive strict output-only inference from adjacent segments if both sides clearly indicate the same identity.
3. Raw diarization labels like `SPEAKER_00` are never allowed to dominate user-facing output.
4. Generic speaker labels are replaced with stable fallbacks such as `Attendee`, `Attendee A`, `Attendee B`.

Two different helper paths matter:

- `infer_strict_segment_speakers()` fills only very high-confidence missing labels for transcript assembly.
- `humanize_transcript_speaker_labels()` rewrites remaining generic speaker tokens before the model sees the transcript.

This keeps the summarizer grounded in stable speaker names without fabricating real identities.

## Template and Prompt Resolution

Template behavior lives in `src/summarization/prompts.py`.

Important rules:

- `normalize_template_key()` maps empty or legacy keys onto active user-facing templates.
- `TEMPLATE_INFO` provides the UI label and description.
- `get_template_content()` returns the raw template contract shown in the workspace and used by the cohesive summarizer.

Public templates today:

- `meeting`
- `strategic_review`
- `working_session`
- `custom`

For `custom`, the user's prompt text becomes the effective style contract. For non-custom templates, the template contract comes from `get_template_content()` and any user custom instructions are injected as an additional instruction block.

## SummarizationManager

`SummarizationManager.summarize()` is the main entry point.

Normal behavior:

1. Initialize the active backend if needed.
2. Normalize the template key.
3. Emit summarization start events.
4. If explicit `system_prompt` or `user_prompt` overrides are provided, use the legacy one-pass path.
5. Otherwise call `generate_cohesive_summary()`.
6. Wrap the result in `SummarizationResult`.
7. Emit completion or error events.

`SummarizationManager._summarize_with_timeout()` wraps every backend call with `SUMMARIZATION_TIMEOUT_SECONDS`.

## Cohesive Summarizer

`src/summarization/cohesive.py::generate_cohesive_summary()` is the default architecture.

### Step 1: preprocess transcript

- Humanize raw speaker labels.
- Build a `speaker_map` for audit/debug use.
- Emit early progress.

### Step 2: choose a context mode

The summarizer estimates an approximate character budget from the configured context length.

Possible modes:

- `full_transcript`
  - use the full transcript directly when it fits
- `compressed_pack`
  - build a smaller context using structured evidence and selected snippets
- `chunked_extraction`
  - split transcript into overlapping chunks, extract schema-constrained records per chunk, and summarize from those extracted records

#### Chunked extraction details

Chunk fallback exists so long meetings can still be summarized without blindly truncating the transcript.

Current behavior:

- split into overlapping character chunks
- prefer chunk boundaries at timestamp markers
- extract structured JSON-like evidence for each chunk
- validate schema shape
- repair malformed JSON through a retry prompt
- retry semantically empty outputs if the chunk contains obvious decision/action/question/risk cues
- merge the rendered chunk records into the final summarization context

This path is lossy compared with full-transcript mode, but much better than summarizing arbitrary truncated text.

### Step 3: pass 1 draft generation

Pass 1 is a constrained drafting step.

Inputs:

- template contract
- selected context text
- context mode
- optional additional user instructions

Key pass-1 rules:

- obey the exact template section structure
- keep only relevant material
- do not turn recommendations into action items
- do not guess owners
- use transcript-derived names or fallback attendee labels only
- compress related discussion into fewer, stronger bullets

### Step 4: pass 2 editorial polish

Pass 2 edits the draft rather than re-summarizing from scratch.

It focuses on:

- better Obsidian formatting
- bullet structure and spacing
- table cleanup
- repetition removal
- preserving factual detail
- not upgrading tentative language into stronger commitment
- removing vague or non-committed action items

For short transcripts, pass 2 is skipped when the draft looks clean enough. This is a latency optimization, not a different architecture.

### Step 5: retry if output quality is bad

The summarizer retries once when it detects:

- leaked think/control tokens
- repeated sentences
- missing `## Action Items` despite structured actions being present

The retry reuses the editorial path with stricter cleanup constraints.

### Prompt audit

The cohesive summarizer returns a `prompt_audit` payload containing:

- pass 1 system prompt
- pass 1 user prompt
- pass 2 system prompt
- pass 2 user prompt

These fields are persisted on `Summary` and included in the Obsidian export.

## Backend Runtime

The normal backend is `OllamaBackend`.

Each call:

- uses chat-format system/user messages
- passes `num_ctx`
- passes `num_gpu`
- passes temperature and other sampling options
- sets `think=false` when configured
- sets `keep_alive=0`

Response cleanup strips:

- complete `<think>...</think>` blocks
- orphan `</think>` tails
- tokenizer/control-token tails

This backend is local-only in the normal Sidekick setup.

## Draft Lifecycle

### Generate draft

`_run_summary_job()`:

1. resolves transcript version
2. rebuilds transcript from stored segments
3. calls `summarization_manager.summarize()`
4. measures processing duration
5. finds the latest saved summary for that transcript version
6. writes a replacement `draft` summary

Important behavior:

- there is only one active draft per meeting/transcript-version pair
- draft regeneration deletes older drafts for that same scope
- `source_type` is `generated` for the first summary and `resummarized` when a saved parent already exists

### Manual edit

`PATCH /api/summary-drafts/{id}` updates draft content and marks the draft as `manual_edit`.

### AI revise

`SummarizationManager.refine_summary()` is intentionally separate from full summarization.

It edits an existing summary using:

- a narrow editing system prompt
- the instruction
- the current summary

It does not rebuild from the transcript. This is fast, but it means AI revise is an editing pass over existing summary text, not a full regeneration.

### Save

Saving a draft:

1. builds export metadata from the draft and transcript version
2. optionally writes the Obsidian note
3. promotes the draft to `saved`
4. records save/export metadata

The draft-to-saved promotion preserves:

- transcript version linkage
- template metadata
- processing duration
- prompt audit fields

## Export Path

The export path uses the same summarization core, but the orchestration is slightly different.

`_run_export_pipeline()`:

1. persists title/template/custom prompt first
2. reuses existing transcript segments when authoritative transcription already exists
3. otherwise transcribes and persists transcript segments
4. unloads WhisperX to free VRAM
5. calls `summarization_manager.summarize()`
6. creates/replaces a draft summary row
7. returns build parameters needed for a later vault write

This means export is not a separate summary algorithm. It is another orchestration path around the same manager and cohesive summarizer.

## What Is Not The Default Path

`SummarizationManager.process_with_pipeline()` and the code under `src/summarization/pipeline/` still exist, but they are not the normal product path for meeting summaries.

Treat that package as experimental/deprecated unless a future change explicitly restores it as the default architecture.

## Operational Invariants

These are the rules future changes should preserve unless intentionally redesigned:

- summaries are scoped to transcript versions
- template/custom prompt state should be reproducible for a given summary
- user-facing output must not expose raw `SPEAKER_XX` labels
- action item owners must come from transcript evidence or remain `TBD`
- prompt audit fields must remain persisted for generated summaries
- the cohesive summarizer is the default path for normal meeting summaries
- export should not use a different summary algorithm than workspace draft generation without explicit documentation

## Change Checklist

If you change summarization architecture, update this document when any of the following change:

- default summarization entrypoints
- transcript-to-prompt shaping
- speaker handling rules
- context selection logic
- chunking or extraction schema
- pass 1 / pass 2 / retry behavior
- prompt audit persistence
- summary versioning or draft/save lifecycle
- default backend/runtime assumptions

At minimum, keep these files aligned:

- `docs/summarization-architecture.md`
- `AGENTS.md`
- `CLAUDE.md`
- `GEMINI.md`

