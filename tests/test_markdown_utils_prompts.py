import unittest

from src.core.markdown_utils import build_obsidian_markdown


class MarkdownUtilsPromptTests(unittest.TestCase):
    def test_obsidian_markdown_places_meeting_info_in_collapsed_callout(self):
        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Custom",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="12s",
            transcript="[00:00] Speaker: Hello",
        )

        self.assertIn("> [!info]- Meeting Info", markdown)
        self.assertIn("> **Template**: Custom", markdown)
        self.assertIn("> **Recorded**: March 7, 2026 at 9am (CST)", markdown)
        self.assertIn("> **Exported**: March 7, 2026 at 9:05am (CST)", markdown)
        self.assertIn("> **Meeting Length**: 5 min", markdown)
        self.assertIn("> **Processing Time**: 12s", markdown)
        self.assertLess(markdown.index("> [!info]- Meeting Info"), markdown.index("## Summary"))

    def test_obsidian_markdown_omits_processing_time_from_meeting_info_when_unavailable(self):
        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Custom",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="N/A",
            transcript="[00:00] Speaker: Hello",
        )

        self.assertIn("> [!info]- Meeting Info", markdown)
        self.assertNotIn("Processing Time", markdown)

    def test_obsidian_markdown_includes_folded_callouts_for_prompt_audit_and_transcript(self):
        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Custom",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="12s",
            transcript="[00:00] Speaker: Hello",
            pass1_system_prompt="system one",
            pass1_user_prompt="user one",
            pass2_system_prompt="system two",
            pass2_user_prompt="user two",
        )

        self.assertIn("> [!note]- Transcript", markdown)
        self.assertNotIn("<details", markdown)
        self.assertIn("> [!note]- Pass 1: System Prompt", markdown)
        self.assertIn("> [!note]- Pass 1: User Prompt", markdown)
        self.assertIn("> [!note]- Pass 2: System Prompt", markdown)
        self.assertIn("> [!note]- Pass 2: User Prompt", markdown)
        self.assertIn("> ```text\n> system one\n> ```", markdown)
        self.assertIn("> ```text\n> user two\n> ```", markdown)
        self.assertIn("> ```text\n> [00:00] Speaker: Hello\n> ```", markdown)
        self.assertNotIn("Pass 1 Prompt: System Prompt", markdown)
        self.assertNotIn("Pass 2 Prompt: User Prompt", markdown)

    def test_obsidian_markdown_omits_large_runtime_payloads_from_prompt_audit(self):
        pass1_user_prompt = """## Style Contract
Be concise.

## Source Context (full_transcript)
[00:00] Oscar: Hello
[00:05] Pam: Hi

Follow the Style Contract above exactly.
Do not add extra sections.
"""
        pass2_user_prompt = """You are editing a custom meeting summary draft for clarity and accuracy.

Draft:
## Summary
- Long draft body

Return ONLY the final edited output."""

        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Custom",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="12s",
            transcript="[00:00] Speaker: Hello",
            pass1_system_prompt="system one",
            pass1_user_prompt=pass1_user_prompt,
            pass2_system_prompt="system two",
            pass2_user_prompt=pass2_user_prompt,
        )

        self.assertIn("[Omitted from note: transcript/context payload]", markdown)
        self.assertIn("[Omitted from note: draft summary payload]", markdown)
        self.assertNotIn("[00:05] Pam: Hi", markdown)
        self.assertNotIn("- Long draft body", markdown)
        self.assertIn("Follow the Style Contract above exactly.", markdown)
        self.assertIn("Return ONLY the final edited output.", markdown)

    def test_obsidian_markdown_includes_revision_history_section(self):
        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Meeting",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="12s",
            transcript="[00:00] Speaker: Hello",
            revision_history=[
                {
                    "created_at": "2026-03-11T20:15:00Z",
                    "route": "evidence_needed",
                    "used_transcript_context": True,
                    "transcript_version_number": 2,
                    "instruction": "Add more technical detail from the transcript.",
                }
            ],
        )

        self.assertIn("> [!abstract]- Revision History", markdown)
        self.assertIn("Transcript-backed", markdown)
        self.assertIn("Transcript v2", markdown)
        self.assertIn("Add more technical detail from the transcript.", markdown)

    def test_obsidian_markdown_places_revision_history_after_summary_and_before_prompts(self):
        markdown = build_obsidian_markdown(
            content="## Summary\n- Test",
            template_label="Meeting",
            recorded_at="March 7, 2026 at 9am (CST)",
            exported_at="March 7, 2026 at 9:05am (CST)",
            duration_str="5 min",
            processing_time_str="12s",
            transcript="[00:00] Speaker: Hello",
            pass1_system_prompt="system one",
            revision_history=[
                {
                    "instruction": "Tighten the action items.",
                }
            ],
        )

        summary_index = markdown.index("## Summary")
        info_index = markdown.index("> [!info]- Meeting Info")
        revision_index = markdown.index("> [!abstract]- Revision History")
        pass1_index = markdown.index("> [!note]- Pass 1: System Prompt")

        self.assertLess(info_index, summary_index)
        self.assertLess(summary_index, revision_index)
        self.assertLess(revision_index, pass1_index)


if __name__ == "__main__":
    unittest.main()
