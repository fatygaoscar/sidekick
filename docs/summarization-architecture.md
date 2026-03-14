# Summarization Architecture

Engineer-facing reference for how Sidekick turns a transcript version into a draft or saved summary.

This document is the source of truth for the current summarization architecture. If you change the summarization flow, prompt assembly, transcript shaping, summary persistence model, or the default runtime path, update this file in the same change.

## Scope

This document covers the active production summarization path:

- transcript version selection
- transcript assembly from stored segments
- template and prompt resolution
- cohesive two-pass summarization
- optional topic-identified topic-segmented summarization
- draft summary persistence
- refine and save flows
- export-time behavior

This document does not treat `src/summarization/pipeline/` as the default architecture. That package still exists, but it is not the normal product path for meeting summaries.

## High-Level Flow

Normal workspace flow:

1. The workspace persists title/template/custom prompt settings.
2. The user starts `POST /api/recordings/{id}/summary-job`.
3. The backend selects a transcript version and rebuilds a timestamped transcript from stored segments.
4. `SummarizationManager.summarize()` routes to the configured pipeline strategy.
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
  - `POST /api/recordings/{id}/summary-draft` branches from the currently selected summary version when the workspace requests a draft.

### Summarization engine

- `src/summarization/manager.py`
  - backend selection
  - pipeline strategy selection
  - runtime provider switching for future requests
  - provider diagnostics
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
  - topic-identification / topic-extraction prompt contracts for `topic_segmented_v1`
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

Context selection remains the same architecture regardless of provider. Provider differences may influence the effective budget or whether a given transcript can stay in `full_transcript` mode, but they do not change the core routing model.

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

Export formatting rules:

- exported notes now write a stable latest file under `Meetings/YYYY/YYYY-MM/Title.md`; previous exported versions are copied into `Meetings/YYYY/YYYY-MM/_versions/Title/vN.md`
- `Meeting Info` appears first as a collapsed callout containing template, recorded/exported timestamps, duration, processing time when available, and the short `Sidekick ID` plus summary/transcript version labels
- the meeting summary content appears immediately after that top metadata callout
- revision history appears immediately after the summary when present
- revision history, prompt audit blocks, and transcript are rendered as Obsidian foldable callouts collapsed by default
- `Meeting Info` is also rendered as a collapsed Obsidian foldable callout so the note opens directly into the summary
- exported notes include YAML frontmatter with full machine IDs and the small query-safe field set (`meeting_date`, `meeting_month`, `template_key`, `recording_duration_minutes`, `tags`, `sidekick_export_status`) so Bases/Dataview can query meetings without depending on long filenames or week-based folders
- `sidekick_export_status` is stored as a string (`latest` / `archived`) instead of a boolean checkbox so users do not accidentally break Base filters; older notes can be repaired with `scripts/repair_obsidian_export_status.py`
- use foldable callouts instead of raw HTML `<details>` blocks so markdown content inside the collapsed sections still renders correctly in Obsidian

## Backend Runtime

The default architecture supports multiple backends behind the same cohesive summarizer.

Normal runtime today supports:

- `OllamaBackend`
- `OpenAIBackend`

Both backends feed the same summarization architecture.

### Ollama runtime notes

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

### OpenAI runtime notes

OpenAI uses the same cohesive summarizer and prompt flow, but differs at the transport boundary:

- no local context override is required for the API call itself
- JSON-returning assistant tasks should use structured JSON mode where supported
- provider diagnostics should capture request IDs and readiness failures

The architecture does not split into a second OpenAI-specific summary pipeline.

## Structured Outputs and Schema Gating

Schema gating is not the default strategy for the final markdown summary path.

Current state:

- pass 1 draft generation is prose/markdown-oriented
- pass 2 editorial polish is prose/markdown-oriented
- chunked extraction uses schema-shaped prompts plus validation/repair
- chat/search/apply assistant paths are the best fit for provider-enforced JSON output

Why schema gating helps:

- it improves output-shape reliability for structured tasks
- it reduces malformed JSON and repair retries
- it makes failures easier to diagnose as schema/transport/refusal problems instead of free-form output drift

Where it is most useful:

- chunk extraction records in `chunked_extraction`
- workspace chat JSON payloads
- apply-to-summary JSON payloads
- transcript-grounded search answer JSON payloads

Why it is not the default for final summaries:

- the primary product output is human-readable markdown, not a fixed record schema
- the pass 1 / pass 2 architecture depends on editorial compression, selective emphasis, and formatting judgment
- schema gating can enforce shape, but not factual correctness or good prose
- forcing the final summary into a rigid schema would require a different architecture, likely a structured intermediate representation rendered to markdown afterward

Operational guidance:

- use schema gating for structured subflows when the backend supports it
- keep the final cohesive markdown summary path shared across providers unless the product intentionally moves to a JSON-first rendering architecture

### Live provider switching

The selected summarization provider is persisted in global app settings and can be changed from the settings page.

Rules:

- switching is runtime-live and does not require restart
- switching applies to future summarize/refine/chat/search/export requests
- in-flight work stays on the backend it started with
- provider selection does not change transcript versioning, prompt audit persistence, or draft/save behavior

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
- when the user explicitly branches from an older saved summary while another draft already exists, the existing draft is preserved as an in-app saved `Saved Copy` before the new draft replaces it
- saved copies do not carry Obsidian export metadata and must not replace the latest exported summary for `Open in Obsidian` links
- saved copies still consume the canonical saved-summary version number sequence, so dropdown labels and Obsidian note titles stay aligned

### Manual edit

`PATCH /api/summary-drafts/{id}` updates draft content and marks the draft as `manual_edit`.

When manual edit starts from a selected saved summary instead of the active draft, the workspace first branches a new draft from that selected version.

### Undo

The current `Undo` button is not persisted summary history.

It is a browser-session-only draft convenience:

- previous draft content is stored in client-side `summaryHistory`
- entries are pushed before revise/manual-edit/apply operations in the current session
- undo rewrites the active draft content from that in-memory stack

Important limitations:

- saving a draft does not preserve an undo stack for later
- reloading or reopening the workspace clears the draft-session undo history
- saved summaries do not currently support persistent undo

Future direction:

- if we add saved-version rollback later, prefer a dedicated `Revert to Previous Version` action based on summary lineage (`parent_summary_id`)
- do not treat that as the same thing as the current draft-only `Undo` button

### AI revise

`SummarizationManager.refine_summary()` is intentionally separate from full summarization.

It edits an existing summary using one of two internal routes:

- `style_only`: instruction + current summary
- `evidence_needed` / `uncertain`: instruction + current summary + retrieved transcript evidence windows from the active transcript version

Important behavior:

- AI revise is still an editing pass, not a full re-summarization run.
- It does not send the full transcript by default.
- The active template is used as structural guidance during revise, but not as a rigid schema.
- The reviser may merge, omit, or tighten sections when that produces a clearer summary for the actual meeting.
- For additive or factual requests, the backend retrieves compact evidence windows from the selected transcript version and only adds details grounded in those windows.
- revise always starts from the summary version currently selected in the workspace. It must not silently jump back to a different active draft or the latest saved/exported summary.
- If transcript evidence is insufficient for a factual expansion request, the revise flow should fail soft rather than invent new details.
- A lightweight structure-repair pass may run when the first revise degrades headings, markdown tables, or overall scannability.

Revision audit behavior:

- AI revise appends lightweight history entries into `Summary.workflow_data_json`.
- History entries record the instruction, route, whether transcript context was used, transcript version linkage, and lightweight evidence counts.
- That revision history is serialized into the workspace, shown in the Summary and Transcript tabs, and included in Obsidian export when present.

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
- `topic_segmented_v1` is an opt-in internal pipeline, not the default workspace path
- `topic_segmented_v1` should identify business topics before per-topic extraction and should fall back deterministically if topic identification fails
- export should not use a different summary algorithm than workspace draft generation without explicit documentation
- provider switching must not let one in-flight summary operation mix multiple backends

## Optional Topic-Segmented Path

`topic_segmented_v1` is an opt-in pipeline strategy behind the same manager and summary job orchestration.

High-level shape:

1. deterministic business-start filtering removes casual preamble
2. a transcript-level topic-identification prompt identifies real business topics and their timestamp ranges
3. each identified topic range is extracted with the strict JSON topic extraction prompt
4. deterministic markdown rendering produces topic-local sections and action items
5. if topic identification fails or returns unusable spans, the pipeline falls back to the deterministic segmentation helper

This path is intended for cases where topic-local organization matters more than the default cohesive narrative summary. It does not replace the cohesive path as the default meeting-summary architecture.

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
