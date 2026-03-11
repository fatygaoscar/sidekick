"""Regression tests for transcript-aware, template-aware summary revision helpers."""

import unittest

from src.summarization.manager import (
    classify_revision_route,
    evaluate_revision_structure,
    select_revision_evidence_windows,
)


class SummaryRevisionHelperTests(unittest.TestCase):
    def test_classify_revision_route_distinguishes_style_and_evidence_requests(self):
        self.assertEqual(classify_revision_route("Make this shorter and more executive."), "style_only")
        self.assertEqual(
            classify_revision_route("Add more technical detail about the database migration."),
            "evidence_needed",
        )
        self.assertEqual(classify_revision_route("Please revise this."), "uncertain")

    def test_select_revision_evidence_windows_returns_compact_matches_for_long_transcript(self):
        lines = [
            f"[00:{index:02d}] Speaker: filler context line {index}"
            for index in range(30)
        ]
        lines.extend(
            [
                "[00:30] Speaker: We discussed the database migration plan in detail.",
                "[00:31] Speaker: The migration includes schema backfills and index cleanup.",
                "[00:32] Speaker: We need to coordinate the rollout next week.",
            ]
        )
        transcript = "\n".join(lines * 8)

        windows = select_revision_evidence_windows(
            transcript=transcript,
            instruction="Add technical detail about the database migration.",
            current_summary="## Summary\n- Migration work was discussed.",
            route="evidence_needed",
        )

        self.assertGreaterEqual(len(windows), 1)
        self.assertTrue(any("database migration" in window["snippet"].lower() for window in windows))

    def test_evaluate_revision_structure_allows_useful_section_drift(self):
        original = """## Summary
- Strong summary

## Key Decisions
- None.

## Open Questions / Unresolved Items
- None.
"""
        revised = """## Summary
- Strong summary

## Key Takeaways
- Follow-up work was minor, so open questions were folded into the main takeaways.
"""

        valid, reason = evaluate_revision_structure(
            original_summary=original,
            revised_summary=revised,
            template_key="meeting",
        )

        self.assertTrue(valid)
        self.assertIsNone(reason)

    def test_evaluate_revision_structure_rejects_flattened_output(self):
        original = """## Summary
- Strong summary

## Key Decisions
- Decision text

## Action Items
| Owner | Action | Due |
|-------|--------|-----|
| Sam | Follow up | TBD |
"""
        revised = "This meeting covered several points and the team agreed on some next steps."

        valid, reason = evaluate_revision_structure(
            original_summary=original,
            revised_summary=revised,
            template_key="meeting",
        )

        self.assertFalse(valid)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
