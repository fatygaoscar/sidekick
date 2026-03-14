# Todo

## Plan

- [x] Stabilize the workspace settings prompt editor on mobile by removing app-driven scroll/reflow during active typing.
- [x] Update workspace prompt editing logic to avoid textarea auto-resize and full workspace reloads while prompt edit mode is active.
- [x] Adjust mobile textarea CSS so the prompt editor keeps a fixed height and scrolls internally.
- [x] Verify the updated behavior with targeted static checks and repo-appropriate test commands.

## Review

- Changed `web/js/recording-workspace.js` so prompt editing uses `focus({ preventScroll: true })` instead of mobile `scrollIntoView`, skips textarea auto-resize on mobile, keeps autosave local while prompt edit mode is active, and refreshes the workspace only after edit mode ends when a fresh load is still needed.
- Changed `web/css/styles.css` so the mobile workspace prompt textarea uses a fixed height with internal scrolling instead of growing and reflowing the page.
- Bumped the cache-busting asset query strings in `web/index.html` and `web/recordings.html` for the updated CSS and workspace JS.
- Verified with `node --check web/js/recording-workspace.js`.
- Verified with `./venv/bin/python -m unittest tests.test_sessions_workspace_regressions` (`66` tests passed).
- `python3 -m unittest tests.test_sessions_workspace_regressions` failed in the system interpreter because `fastapi` is not installed there; the project virtualenv test run passed.
- Residual risk: this was validated with static checks and existing regression tests only; the exact mobile keyboard/viewport behavior still needs manual confirmation on an actual phone or device emulator.

## Topic-Segmented MVP Plan

- [x] Add a config- and app-settings-backed summarization pipeline strategy with `cohesive` as the default and optional per-run override support for summary jobs.
- [x] Implement a simple deterministic topic segmentation stage for the new `topic_segmented_v1` pipeline.
- [x] Implement strict per-topic JSON extraction with explicit action-item exclusion rules and lightweight repair/warning reporting.
- [x] Implement deterministic markdown rendering with topic-local action items and compact top-level summary bullets.
- [x] Persist the structured topic-segmented artifact and warnings in summary workflow metadata, then cover the new path with targeted regression tests and checks.

## Topic-Segmented MVP Review

- Added `summarization_pipeline_strategy` config and persisted app settings support so the runtime default stays on `cohesive`, can be flipped without redeploying, and can be overridden per summary job request.
- Added a new `topic_segmented_v1` summarization path in `SummarizationManager` plus `src/summarization/topic_segmented.py` for simple transcript segmentation, strict per-topic JSON extraction, deterministic markdown rendering, and workflow metadata storage.
- Extended the summary-job API to accept and report an optional per-run `pipeline_strategy`, and updated app startup plus settings serialization so the saved global default is respected consistently.
- Added a lightweight settings-page control for switching the global summary pipeline between `cohesive` and `topic_segmented_v1`.
- Added regression coverage for topic-segmented rendering/segmentation behavior, manager runtime state, and the new settings serialization surface.
- Verified with `python3 -m compileall src`.
- Verified with `node --check web/js/settings.js`.
- Verified with `./venv/bin/python -m unittest tests.test_sessions_workspace_regressions` (`70` tests passed).
- Residual risk: the new topic segmentation and extraction heuristics are intentionally simple for MVP and still need evaluation against real meeting transcripts to tune split thresholds and warning usefulness.

## Topic Quality Refinement Plan

- [x] Replace the single-next-turn split heuristic with a deterministic lookahead-window boundary check and smarter adjacent merge-to-cap behavior.
- [x] Add a constrained topic-label prompt plus deterministic fallback labeling, and route topic-segmented summaries through it before Pass 1 extraction.
- [x] Persist boundary and label strategy metadata/warnings for topic-segmented runs.
- [x] Add targeted regression tests for the new boundary heuristic, label prompt helpers, and label fallback behavior.
- [x] Verify with compile and regression test commands, then document review notes and residual risks.

## Topic Quality Refinement Review

- Replaced the old single-next-turn split rule in `src/summarization/topic_segmented.py` with a deterministic lookahead-window heuristic that compares the current segment tail against the next few turns, keeps hard time-gap/transition-phrase splits, and avoids splitting on speaker change alone.
- Replaced size-only topic-cap merging with a deterministic adjacent merge choice that prefers semantically closer neighbors.
- Added a dedicated topic-label prompt in `src/summarization/prompts.py` plus label prompt wiring and deterministic fallback/cleanup logic in `src/summarization/topic_segmented.py`.
- Updated `SummarizationManager` so topic-segmented runs generate labels before Pass 1 extraction, keep prompt audit separation from the cohesive pipeline, and persist `boundary_version`, `label_strategy`, and label-candidate metadata in workflow data.
- Added regression coverage for label prompt templating, generic-label fallback, lookahead segmentation stability, and windowed-drift splitting.
- Verified with `python3 -m compileall src`.
- Verified with `./venv/bin/python -m unittest tests.test_sessions_workspace_regressions` (`76` tests passed).
- Residual risk: label quality now depends on one extra small model call per topic, so the main remaining unknown is how consistently each configured backend follows the short-label prompt on real meetings.

## Topic Quality Refinement v2 Plan

- [x] Add a deterministic business-topic start filter so casual preamble is skipped from user-facing topic segmentation.
- [x] Move topic labeling to after Pass 1 extraction so labels are generated from extracted content rather than raw transcript wording.
- [x] Tighten deterministic boundary formation and adjacent merge behavior so nearby but distinct business discussions split more reliably.
- [x] Add per-topic QA checks for business-label quality, coherence, filler leakage, and action/topic alignment, then persist the results in workflow metadata.
- [x] Add targeted regression coverage for preamble skipping, post-extraction labeling, and topic-quality warnings, then rerun compile and regression tests.

## Topic Quality Refinement v2 Review

- Added a deterministic business-start detector in `src/summarization/topic_segmented.py` so casual greetings and pre-meeting chatter are skipped from user-facing topic segmentation and recorded only in segmentation metadata.
- Moved topic label generation to after Pass 1 extraction, added a new post-extraction label prompt in `src/summarization/prompts.py`, and switched fallback labeling to derive from extracted content instead of transcript token frequency.
- Tightened deterministic boundary scoring with richer continuity signals and removed generic follow-up vocabulary from family-overlap scoring so adjacent business topics split more reliably.
- Added per-topic QA checks for business-label quality, coherence, filler leakage, and action/topic mismatch, and persisted those checks in topic-segmented workflow metadata.
- Updated regression coverage for preamble skipping, post-extraction label prompting, extracted-content label fallback, and topic-quality warnings.
- Verified with `python3 -m compileall src`.
- Verified with `./venv/bin/python -m unittest tests.test_sessions_workspace_regressions` (`79` tests passed).
- Residual risk: the remaining quality variability is mostly in the post-extraction label model call and in threshold tuning for borderline agenda-setting turns that are very short or ambiguous.

## Topic Identification Prompt Plan

- [x] Add a transcript-level business-topic identification prompt that runs after deterministic business-start filtering and before per-topic extraction.
- [x] Normalize topic-identification results into validated, non-overlapping topic spans with deterministic fallback to the current segmentation path.
- [x] Remove the active post-extraction label-generation step from the main topic-segmented flow and use Step 1 topic names as the authoritative labels.
- [x] Persist topic-identification audit/metadata and keep topic-quality checks attached to the final labeled topics.
- [x] Add regression coverage for topic-identification prompt helpers, topic-span normalization, and fallback behavior, then rerun compile and regression tests.

## Topic Identification Prompt Review

- Added a transcript-level topic-identification prompt in `src/summarization/prompts.py` and helper wiring in `src/summarization/topic_segmented.py` so `topic_segmented_v1` can identify real business topics before doing per-topic extraction.
- Added normalization of model-provided topic spans into validated, non-overlapping topic slices with deterministic fallback to the existing segmentation heuristics when the topic-identification output is unusable.
- Refactored `SummarizationManager` so Step 1 topic identification now provides the authoritative topic labels and boundaries, while Step 2 remains the structured extraction pass for each topic.
- Removed the active post-extraction label-generation loop from the main topic-segmented flow; extracted-content label helpers remain only as local fallback/tested utilities.
- Persisted topic-identification strategy metadata, prompt audit fields, and topic-quality checks in workflow data.
- Added regression coverage for the new topic-identification prompt helper and topic-span normalization alongside the existing topic-quality and segmentation tests.
- Verified with `python3 -m compileall src`.
- Verified with `./venv/bin/python -m unittest tests.test_sessions_workspace_regressions` (`81` tests passed).
- Residual risk: topic quality now depends more on Step 1 prompt compliance, so the main remaining unknown is how consistently each backend returns clean topic ranges and business-relevant topic names on long or messy meetings.
