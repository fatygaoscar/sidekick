import importlib.util
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from src.core.obsidian_rewrite import build_saved_summary_markdown


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rewrite_meetings_to_modern_export.py"
SPEC = importlib.util.spec_from_file_location("rewrite_meetings_to_modern_export", SCRIPT_PATH)
rewrite_script = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = rewrite_script
SPEC.loader.exec_module(rewrite_script)


class ObsidianRewriteTests(unittest.TestCase):
    def test_build_saved_summary_markdown_uses_current_export_shape(self):
        markdown = build_saved_summary_markdown(
            meeting_id="meeting-1",
            summary_id="summary-1",
            transcript_version_id="tv-1",
            summary_version_number=4,
            transcript_version_number=2,
            meeting_title="Goals Touchbase",
            template_key="meeting",
            template_label="General Meeting",
            summary_content="## Summary\n- Test",
            processing_duration_seconds=12.0,
            workflow_data_json=None,
            pass1_system_prompt=None,
            pass1_user_prompt=None,
            pass2_system_prompt=None,
            pass2_user_prompt=None,
            session_started_at=datetime(2026, 3, 11, 16, 31, tzinfo=UTC),
            session_timezone_name="America/Chicago",
            session_timezone_offset_minutes=-300,
            transcript_segments=[
                SimpleNamespace(
                    text="Hello",
                    start_time=0.0,
                    end_time=15.0,
                    is_important=False,
                    speaker="Speaker 1",
                    speaker_cluster="SPEAKER_00",
                )
            ],
            tags=["client/acme"],
            export_status="latest",
            exported_at_source=datetime(2026, 3, 11, 18, 0, tzinfo=UTC),
        )

        self.assertIn('sidekick_export_status: "latest"', markdown)
        self.assertIn('tags: ["client/acme"]', markdown)
        self.assertIn("> [!info]- Meeting Info", markdown)
        self.assertIn("> [!note]- Transcript", markdown)

    def test_expected_export_status_uses_versions_path(self):
        self.assertEqual(
            rewrite_script._expected_export_status("Meetings/2026/2026-03/_versions/Test/v2.md"),
            "archived",
        )
        self.assertEqual(
            rewrite_script._expected_export_status("Meetings/2026/2026-03/Test.md"),
            "latest",
        )

    def test_frontmatter_is_safe_rejects_unknown_keys(self):
        self.assertTrue(rewrite_script._frontmatter_is_safe({"sidekick_summary_id": "sum-1"}))
        self.assertFalse(rewrite_script._frontmatter_is_safe({"custom_field": "value"}))

    def test_evaluate_note_skips_when_summary_content_is_missing(self):
        entry = rewrite_script._evaluate_note(
            row={
                "summary_id": "sum-1",
                "meeting_id": "meeting-1",
                "summary_content": "## Summary\n- Canonical",
                "obsidian_relative_path": "Meetings/2026/2026-03/Test.md",
                "pass1_system_prompt": None,
                "pass1_user_prompt": None,
                "pass2_system_prompt": None,
                "pass2_user_prompt": None,
            },
            relative_path="Meetings/2026/2026-03/Test.md",
            note_text="## Summary\n- Manually changed",
            frontmatter={"sidekick_summary_id": "sum-1"},
            transcript="",
            expected_markdown="## Summary\n- Canonical",
        )

        self.assertEqual(entry.status, "skip")
        self.assertIn("summary_content_mismatch", entry.skip_reasons)


if __name__ == "__main__":
    unittest.main()
