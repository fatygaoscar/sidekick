import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.core.obsidian_exports import (
    copy_obsidian_markdown_with_frontmatter_updates,
    extract_tags_from_frontmatter,
    find_note_by_summary_id,
    read_frontmatter,
    resolve_latest_export_target,
)


class ObsidianExportsTests(unittest.TestCase):
    def test_find_note_by_summary_id_returns_matching_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir)
            note = vault / "Meetings" / "2026" / "2026-03" / "Custom Name.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                "---\nsidekick_summary_id: \"sum-123\"\n---\n\n# Test\n",
                encoding="utf-8",
            )

            result = find_note_by_summary_id(
                obsidian_vault_path=str(vault),
                summary_id="sum-123",
            )

            self.assertEqual(result, "Meetings/2026/2026-03/Custom Name.md")

    def test_extract_tags_from_frontmatter_supports_multiline_yaml_lists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "note.md"
            note.write_text(
                "---\n"
                "tags:\n"
                "  - client/acme\n"
                "  - followup\n"
                "---\n",
                encoding="utf-8",
            )

            self.assertEqual(
                extract_tags_from_frontmatter(note),
                ["client/acme", "followup"],
            )

    def test_extract_tags_from_frontmatter_supports_inline_lists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            note = Path(tmpdir) / "note.md"
            note.write_text(
                "---\n"
                'tags: ["client/acme", "followup"]\n'
                "---\n",
                encoding="utf-8",
            )

            self.assertEqual(
                extract_tags_from_frontmatter(note),
                ["client/acme", "followup"],
            )

    def test_copy_obsidian_markdown_with_frontmatter_updates_rewrites_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir)
            source = vault / "Meetings" / "2026" / "2026-03" / "Goals Touchbase.md"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                "---\n"
                'sidekick_summary_id: "sum-123"\n'
                "sidekick_is_latest_export: true\n"
                "sidekick_export_status: \"latest\"\n"
                "---\n\n"
                "# Test\n",
                encoding="utf-8",
            )

            copied = copy_obsidian_markdown_with_frontmatter_updates(
                obsidian_vault_path=str(vault),
                source_relative_path="Meetings/2026/2026-03/Goals Touchbase.md",
                destination_relative_path="Meetings/2026/2026-03/_versions/Goals Touchbase/v12.md",
                frontmatter_updates={"sidekick_export_status": "archived"},
                remove_frontmatter_keys=("sidekick_is_latest_export",),
            )

            archived = vault / "Meetings" / "2026" / "2026-03" / "_versions" / "Goals Touchbase" / "v12.md"
            self.assertTrue(copied)
            self.assertEqual(
                read_frontmatter(archived).get("sidekick_export_status"),
                "archived",
            )
            self.assertNotIn("sidekick_is_latest_export", read_frontmatter(archived))

    def test_resolve_latest_export_target_does_not_reuse_renamed_path(self):
        local_started_at = datetime(2026, 3, 11, 11, 31)

        target = resolve_latest_export_target(
            vault_path=None,
            meeting_id="meeting-1",
            title="Goals Touchbase",
            local_started_at=local_started_at,
            preferred_relative_path="Meetings/2026/2026-03/Goals Touchbase - Final Draft.md",
        )

        self.assertEqual(target.relative_path, "Meetings/2026/2026-03/Goals Touchbase.md")

    def test_resolve_latest_export_target_reuses_managed_path(self):
        local_started_at = datetime(2026, 3, 11, 11, 31)

        target = resolve_latest_export_target(
            vault_path=None,
            meeting_id="meeting-1",
            title="Goals Touchbase",
            local_started_at=local_started_at,
            preferred_relative_path="Meetings/2026/2026-03/Goals Touchbase.md",
        )

        self.assertEqual(target.relative_path, "Meetings/2026/2026-03/Goals Touchbase.md")


if __name__ == "__main__":
    unittest.main()
