import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "capture_week_folder_meetings.py"
SPEC = importlib.util.spec_from_file_location("capture_week_folder_meetings", SCRIPT_PATH)
capture_script = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = capture_script
SPEC.loader.exec_module(capture_script)


class CaptureWeekFolderMeetingsTests(unittest.TestCase):
    def test_parse_legacy_week_note_root_file(self):
        note = capture_script._parse_legacy_week_note(
            "Meetings/2026 Week 11/11 Wed 1131 - Goals Touchbase (v10).md",
            expected_year=2026,
        )

        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.clean_title, "Goals Touchbase")
        self.assertEqual(note.version_number, 10)
        self.assertFalse(note.in_archive)
        self.assertEqual(note.derived_date.isoformat(), "2026-03-11")

    def test_parse_legacy_week_note_archive_file(self):
        note = capture_script._parse_legacy_week_note(
            "Meetings/2026 Week 10/archive/06 Fri 1502 - Review New Inventory Planner (v9).md",
            expected_year=2026,
        )

        self.assertIsNotNone(note)
        assert note is not None
        self.assertTrue(note.in_archive)
        self.assertEqual(note.clean_title, "Review New Inventory Planner")
        self.assertEqual(note.version_number, 9)
        self.assertEqual(note.derived_date.isoformat(), "2026-03-06")

    def test_select_latest_note_prefers_unversioned_root(self):
        notes = [
            capture_script._parse_legacy_week_note(
                "Meetings/2026 Week 11/11 Wed 1131 - Goals Touchbase.md",
                expected_year=2026,
            ),
            capture_script._parse_legacy_week_note(
                "Meetings/2026 Week 11/11 Wed 1131 - Goals Touchbase (v10).md",
                expected_year=2026,
            ),
        ]
        latest, reasons = capture_script._select_latest_note([note for note in notes if note is not None])

        self.assertEqual(reasons, [])
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIsNone(latest.version_number)

    def test_select_latest_note_promotes_highest_version_when_needed(self):
        notes = [
            capture_script._parse_legacy_week_note(
                "Meetings/2026 Week 10/archive/02 Mon 0932 - PBI Testing (v37).md",
                expected_year=2026,
            ),
            capture_script._parse_legacy_week_note(
                "Meetings/2026 Week 10/02 Mon 0932 - PBI Testing (v38).md",
                expected_year=2026,
            ),
        ]
        latest, reasons = capture_script._select_latest_note([note for note in notes if note is not None])

        self.assertEqual(reasons, [])
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.version_number, 38)

    def test_select_latest_note_promotes_single_archive_unversioned_note(self):
        notes = [
            capture_script._parse_legacy_week_note(
                "Meetings/2026 Week 10/archive/06 Fri 1746 - Product Marketing.md",
                expected_year=2026,
            )
        ]
        latest, reasons = capture_script._select_latest_note([note for note in notes if note is not None])

        self.assertEqual(reasons, [])
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIsNone(latest.version_number)
        self.assertTrue(latest.in_archive)

    def test_merge_or_prepend_frontmatter_preserves_body_without_frontmatter(self):
        markdown = "## Summary\n- Note body"
        rewritten = capture_script._merge_or_prepend_frontmatter(
            markdown,
            frontmatter_updates={
                "type": "meeting-note",
                "meeting_date": "2026-03-11",
                "tags": [],
            },
        )

        self.assertTrue(rewritten.startswith("---\n"))
        self.assertIn('meeting_date: "2026-03-11"', rewritten)
        self.assertIn("## Summary\n- Note body", rewritten)


if __name__ == "__main__":
    unittest.main()
