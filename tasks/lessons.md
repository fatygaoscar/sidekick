# Lessons

- Add short rules here after user corrections so the same mistake does not repeat.
- When adding prompts for a new summarization pipeline, create additive pipeline-specific prompt helpers/constants instead of editing the existing cohesive prompt templates.
- For user-facing topic segmentation, do not stop at structurally plausible chunks; boundary heuristics and labels need to be evaluated against whether a human would recognize the sections as coherent topics.
- For topic-based meeting notes, labels should come from extracted business content and segmentation should skip casual preamble; transcript-local wording and opening chatter are too noisy to drive user-facing topics directly.
- When topic quality is the main failure mode, do not overload the extraction prompt; split topic identification and per-topic fact extraction into separate prompt contracts so boundaries and labels are explicit and testable.
