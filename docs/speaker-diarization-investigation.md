# Speaker Diarization Investigation

## Summary

This document records the speaker-diarization work completed during the recent repair/debugging pass, the current state of the product, and the remaining open hypothesis:

- the diarization itself may now be materially better than before,
- but the speaker review experience may still be misleading because the preview clips and representative segments are not always showing the most diagnostic moments.

This is especially relevant to `Goals Touchbase - Voice Memo`, where the user expectation is:

- `Oscar`
- `Jillian`
- `Greg`

while the UI has repeatedly surfaced clusters that looked like:

- `Oscar`
- `Jillian`
- `Jillian again`

The current working theory is that some of the remaining confusion may be caused by **preview/representation quality**, not only by clustering quality.

## What Changed

### 1. Standardized diarization on `community-1`

Local authoritative diarization was simplified so Sidekick now uses:

- `pyannote/speaker-diarization-community-1`

for both:

- initial transcription
- speaker-detection reruns

WhisperX remains responsible for:

- ASR
- alignment

It is no longer the primary diarization backend for authoritative local file transcription.

### 2. Added diarization readiness checks

Sidekick now preloads diarization readiness and exposes it through runtime diagnostics so failures are explicit instead of silently falling back.

### 3. Simplified speaker repair UX

The speaker tools flow was clarified around three separate actions:

- rename speakers
- merge duplicate speakers
- re-run speaker detection

The old join-point-driven repair flow was removed from the default user experience because it was adding complexity and implying algorithmic behavior that was no longer the primary path.

### 4. Kept an audio scrubber as a listening tool

The Speakers tab keeps a scrubber so users can:

- listen through the recording
- identify voices
- sanity-check speaker assignments

This is now a review aid, not a hidden diarization parameter.

### 5. Added fast speaker-detection reruns

`Re-run Speaker Detection` was split from full retranscription so speaker-focused reruns can operate as a lighter-weight speaker-detection flow rather than always rerunning the full transcript pipeline.

### 6. Added local speaker profiles

Sidekick now supports local speaker profiles with saved voice examples:

- explicit profile creation/promotion
- multiple examples per profile
- matching diarized clusters against known profiles

This is the main mechanism for moving beyond anonymous diarization alone.

### 7. Improved profile save reliability

Profile saves were simplified and hardened:

- one example saved per click
- partial clip decode instead of repeatedly decoding full recordings
- serialized save requests to avoid GPU/timeout contention
- visible `Saving...` / `Queued...` UI states
- empty profile cleanup support

### 8. Added match correction and audit tools

Matched speaker cards now support correction, and Settings now supports manual review/removal of saved profile examples.

Important rule:

- correction adds positive evidence to the chosen profile
- it does **not** automatically delete or rewrite other profile examples

### 9. Fixed stale speaker-review completion state

Speaker inputs were previously getting locked after partial/incomplete save flows. This was fixed by making completion derive from actual unresolved speakers and by making the frontend resilient to stale completion flags.

### 10. Hardened remote workspace/media behavior

For `go.sidekickgo.app`, media playback and workspace loading were made more resilient:

- current-origin media first
- fallback media URL second
- longer remote workspace timeout
- better timing instrumentation

### 11. Added legacy workspace normalization

Older recordings with incomplete legacy metadata are now normalized on read so they behave like modern recordings:

- stale session lifecycle is normalized when final audio and ready transcript already exist
- transcript segments missing `speaker_cluster` are backfilled with stable synthetic cluster ids such as `LEGACY_SPEAKER_00`

This specifically targeted the `Power BI Refinement` March 4 workspace-open failure.

### 12. Centralized speaker attribution and review-state logic

The core speaker logic is now shared instead of being reimplemented separately in multiple paths:

- initial WhisperX transcription
- speaker-detection reruns
- transcript-version speaker review state updates

This means:

- pyannote span-to-segment assignment now comes from one shared helper
- repair quality-gate metrics come from one shared helper
- speaker-review-required state comes from one shared helper

The practical goal is not new model quality by itself. It is to reduce drift between initial transcription and rerun behavior so speaker bugs are easier to reason about and fix.

## Current Product Behavior

### Initial transcription

1. WhisperX transcribes and aligns.
2. `community-1` diarizes the full recording.
3. Sidekick applies shared speaker-attribution logic onto the aligned transcript.
4. If known local speaker profiles exist, speaker/profile matching may relabel clusters.

### Speaker review

The Speakers tab now supports:

- representative preview clips
- renaming
- merging duplicate clusters
- fast speaker detection reruns
- saving voice examples to local profiles
- correcting wrong matched profiles

### Profile learning

The system now learns explicitly from:

- `Add to Profiles`
- `Add Example`
- `Change Match`

It does **not** automatically learn from every rename or merge.

## Remaining Open Issue

### `Goals Touchbase - Voice Memo`

The core unresolved question is:

> Is diarization still truly splitting Jillian / missing Greg, or is the review panel surfacing misleading representative clips that make correct clustering look wrong?

At this point, both are plausible.

Reasons this is still open:

- the model has already been upgraded to `community-1`
- speaker profiles now exist as a second-layer identity system
- the UI is much more capable than before
- yet the user still sees “two of these sound like Jillian”

That points to two possible explanations:

1. diarization is still wrong
2. the selected preview segment for a cluster is not representative enough to let the user distinguish the underlying voice identity confidently

## Current Hypothesis: Preview Quality May Be Misleading

The speaker preview system was already improved to avoid very short greeting/backchannel clips, but it still chooses **one representative segment per cluster**.

That can still be misleading when:

- the chosen segment is low-energy or ambiguous
- the speaker joins late and the selected clip is not the clearest moment
- two speakers have similar cadence/tone in the chosen snippet
- a cluster spans multiple kinds of speaking turns and the chosen preview is not the most distinctive one

In other words:

- the cluster may be acceptable,
- but the preview may not be diagnostic enough for a human to tell that it is actually Greg.

## Likely Next Step

The next investigation should focus on **speaker preview fidelity**, not only on diarization reruns.

Recommended follow-up:

1. inspect the actual representative segments chosen for `Goals Touchbase - Voice Memo`
2. compare those preview clips against longer or alternate clips from the same cluster
3. consider surfacing:
   - more than one preview clip per speaker
   - a “next preview” button
   - a longer representative clip when confidence is low
4. verify whether the cluster that appears “wrong” is actually acoustically Greg when sampled elsewhere in the same cluster

## Decision Log

### Chosen

- one consistent local diarization backend
- local speaker profiles
- explicit human correction
- no silent destructive cleanup
- legacy workspace compatibility normalization

### Rejected / Deferred

- keeping WhisperX and pyannote as two competing primary diarization paths
- join-point-driven late-join routing as the default repair strategy
- silent profile learning from every rename
- automatic deletion of “wrong” examples
- cloud diarization fallback as the first solution

## Operational Notes

- If `community-1` is unavailable, the app should report that explicitly rather than silently degrading.
- Remote workspace/media failures should now be easier to diagnose via timing logs.
- Legacy older recordings may normalize themselves on first workspace open.

## Status

The system is substantially more robust than before, but the remaining Greg/Jillian problem is not conclusively solved.

The best current interpretation is:

- diarization quality has improved,
- identity matching is now meaningfully better,
- but the review UI may still under-represent the clusters through weak preview selection.
