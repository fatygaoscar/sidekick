# Meeting Summarization Chunking Audit

## Scope

This audit focuses on the active production summarization path in `src/summarization/cohesive.py`, specifically the fallback behavior when the full transcript does not fit inside the configured context window.

It also reviews the older `src/summarization/pipeline/` package because it still contains a chunked extraction/merge design that is useful for comparison, benchmarking, and future refactors.

## Executive Summary

Chunked summarization was performing worse than full-context summarization for predictable reasons:

1. The active chunked path compressed each chunk into short prose instead of structured evidence.
2. The final summary pass synthesized summaries-of-summaries rather than deduplicating structured records.
3. Chunk boundaries were timestamp-aware but not speaker-turn-aware or sentence-aware.
4. Overlap was too small to reliably preserve decision/action continuity across boundaries.
5. Malformed or non-JSON chunk outputs must be retried through a JSON repair step; otherwise chunk evidence silently disappears.
6. Benchmark scripts did not consistently record the exact model configuration, making regressions hard to attribute.

## Current Active Path

### Full-context mode

When the transcript fits the budget:

- the full transcript is passed directly into the cohesive summarizer
- speaker labels are preserved in the transcript text
- the model sees the original local context and cross-topic dependencies

### Chunked fallback mode: previous failure pattern

Previously, when the transcript exceeded the context budget:

- the transcript was split into overlapping character chunks at timestamp boundaries
- each chunk was compressed into unconstrained prose
- the final pass summarized the concatenated prose extractions

This created multiple information-loss points:

- tentative decisions often vanished
- ambiguous ownership got flattened away
- repeated chunk summaries encouraged deduplication by omission
- the merge pass lost evidence provenance

## Item-by-Item Audit

### 1. Chunk creation

Previous behavior:

- active path: char-based chunking with timestamp boundary preference
- no hard guarantee for speaker-turn boundaries
- no sentence-boundary logic
- no paragraph model

Assessment:

- better than raw fixed-width splitting
- still semantically weak
- can split a multi-line speaker turn if a later timestamp starts near the target boundary

Exact algorithm before this patch:

- target chunk size by characters
- scan backward to the last timestamp marker inside the chunk window
- split there
- start the next chunk with a small fixed overlap

Risk:

- decision/action continuity depends on whether the decision spans a boundary and whether the later chunk preserves enough setup

### 2. Overlap

Previous behavior:

- active path used `400` overlap chars on `8000` char chunks
- that is roughly `5%`

Assessment:

- too small for meeting summaries
- often not enough to preserve the setup plus commitment plus owner assignment

Recommended target:

- `10-15%` overlap
- or enough to preserve at least one to three speaker turns across the boundary

Implemented in this pass:

- default overlap raised to about `12%`
- bounded between `400` and `1200` chars
- chunk diagnostics now record overlap chars and approximate speaker turns

### 3. First-pass prompt

Previous behavior:

- active path used an unconstrained prose extractor
- it explicitly asked for concise prose, max 200 words
- that is compressive and lossy

Why this hurts:

- the model optimizes for salience, not recall
- tentative decisions and ambiguous ownership are the first things to disappear
- later synthesis only sees what survived prose compression

Implemented in this pass:

- chunk extraction now requests structured evidence, not polished prose
- schema fields:
  - `discussion_points`
  - `decisions`
  - `action_items`
  - `open_questions`
  - `risks`
  - `unresolved_items`
  - `notable_quotes_or_context`
- prompt explicitly says:
  - do not omit tentative decisions
  - preserve ambiguous ownership as `null`
  - preserve timestamps and speaker labels

### 4. Merge pass

Previous behavior:

- the final pass received only chunk prose summaries
- this is effectively "summarize summaries"

Assessment:

- high information-loss risk
- deduplication becomes implicit and opaque
- omissions are hard to detect because the final pass has no structured evidence to compare

Implemented in this pass:

- chunk outputs are now JSON-like structured records
- the final rollup sees per-segment evidence records instead of prose compression
- the prompt explicitly instructs the merge step to deduplicate conservatively and preserve tentative items
- non-JSON or malformed chunk responses are treated as parse failures and rewritten into schema-valid JSON before falling back to an empty record

Remaining gap:

- the active path still relies on LLM synthesis for final rollup instead of deterministic schema merge logic
- the older `pipeline/` package contains deterministic merge ideas, but the schema does not yet fully match the new active chunk record schema

### 5. Speaker-label preservation

Ingestion path:

- transcript assembly preserves timestamps and speaker labels
- format: `[MM:SS] SPEAKER_XX: text`

Example before preprocessing:

```text
[12:34] SPEAKER_01: I can take the API migration this week.
```

After attendee resolution, if a speaker map is found:

```text
[12:34] Oscar: I can take the API migration this week.
```

Assessment:

- speaker labels are preserved from transcript assembly through prompt construction
- the bigger historical risk was not label stripping, but chunk prose compression failing to carry owner/speaker details forward

### 6. Inference settings

Previous benchmark gap:

- scripts did not consistently log:
  - model family
  - model tag / quantization
  - runtime
  - `num_ctx`
  - `temperature`
  - `top_p`
  - `repeat_penalty`
  - `seed`

Implemented in this pass:

- added explicit Ollama settings for:
  - `OLLAMA_TEMPERATURE`
  - `OLLAMA_TOP_P`
  - `OLLAMA_REPEAT_PENALTY`
  - `OLLAMA_SEED`
- active Ollama backend now uses those values
- benchmark scripts now log the exact generation config on every run

Truncation behavior:

- there was no explicit tokenizer-based truncation check
- the active path used a character heuristic to decide whether to fall back from full transcript to chunking

Implemented in this pass:

- benchmarks now record estimated prompt tokens for:
  - full transcript prompt
  - each chunk prompt
  - merge prompt

Remaining gap:

- token estimation is heuristic, not tokenizer-exact

### 7. Prompt consistency

Previous behavior:

- the older pipeline extracted:
  - actions
  - decisions
  - risks
  - questions
  - followups
- the active chunked fallback extracted prose instead of a schema

Assessment:

- field mismatch existed between chunk extraction and final synthesis
- final synthesis asked for a richer meeting summary than the chunk pass explicitly preserved

Implemented in this pass:

- active chunk extraction now uses a single explicit schema for chunk records
- merge prompt is aligned to that schema

Remaining gap:

- the older `pipeline/` schema is still different from the active chunk-record schema
- unifying both onto one shared schema would simplify future experiments

### 8. Output validation

Previous behavior in `pipeline/extraction.py`:

- JSON parse failure returned an empty list
- malformed output was silently accepted as "no items"

Assessment:

- this creates false negatives
- a bad model response looks identical to a chunk with no items

Implemented in this pass for the active chunked path:

- classify:
  - JSON parse failure
  - schema failure
  - semantically incomplete output
- retry/fix-up when parsing fails
- retry/fix-up when schema validation fails
- retry when output is structurally valid but suspiciously empty

Remaining gap:

- the older `pipeline/extraction.py` should receive the same retry/fix-up treatment if it will be kept alive as an experimental path

### 9. Transcript cleaning

Assessment:

- transcript construction is relatively conservative
- there is no aggressive filler-word stripping before summarization
- timestamps and speaker turns are preserved

Main risk:

- loss happened downstream during chunk compression and merge, not during transcript cleaning

### 10. Benchmarking

Implemented in this pass:

- new A/B harness: `scripts/benchmark_chunking_ab.py`
- compares:
  - `full_context_single_pass`
  - `chunked_extraction_rollup`
  - `chunked_extraction_overlap_rollup`
- benchmark scripts now persist:
  - transcript id
  - transcript size
  - estimated tokens
  - exact model/runtime settings
  - chunk strategy
  - chunk size
  - overlap size
  - prompt version
  - schema version
  - latency
  - parse/schema success

### 11. Scoring

Implemented:

- benchmark report includes a manual scoring template for:
  - `action_items_recall`
  - `action_items_precision`
  - `decisions_recall`
  - `owner_attribution_accuracy`
  - `omission_rate`
  - `hallucination_rate`
  - `readability`

- optional gold-set scoring support via `--gold-path`

Limitations:

- automatic scoring is intentionally simple
- serious evaluation still needs a maintained gold-standard set

## Recommended Next Fixes By Impact

### Highest impact

1. Replace character heuristics with token-aware budgeting using a real tokenizer.
2. Add speaker-turn-aware chunk splitting so a chunk boundary never cuts through a multi-line turn.
3. Unify the active chunk-record schema and the older `pipeline/` schema.

### Medium impact

4. Add deterministic merge logic for chunk records before final narrative synthesis.
5. Expand automatic gold-set scoring to handle owner attribution and duplicates more robustly.
6. Persist benchmark outputs in a normalized table or SQLite file for easier comparison over time.

### Lower impact

7. Add sentence-boundary awareness in addition to timestamp boundaries.
8. Expose chunk-size and overlap tuning in a debug UI or script presets.

## Files Changed In This Audit Pass

- `config/settings.py`
- `src/summarization/ollama_backend.py`
- `src/summarization/cohesive.py`
- `scripts/benchmark_utils.py`
- `scripts/benchmark_summary.py`
- `scripts/benchmark_ollama_models.py`
- `scripts/benchmark_chunking_ab.py`
