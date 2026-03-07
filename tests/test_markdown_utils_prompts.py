import unittest

from src.core.markdown_utils import build_obsidian_markdown


class MarkdownUtilsPromptTests(unittest.TestCase):
    def test_obsidian_markdown_includes_prompt_code_blocks_and_collapsed_transcript(self):
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

        self.assertIn("<summary>Transcript</summary>", markdown)
        self.assertNotIn("<details open>", markdown)
        self.assertIn("### Pass 1: System Prompt", markdown)
        self.assertIn("### Pass 1: User Prompt", markdown)
        self.assertIn("### Pass 2: System Prompt", markdown)
        self.assertIn("### Pass 2: User Prompt", markdown)
        self.assertIn("```text\nsystem one\n```", markdown)
        self.assertIn("```text\nuser two\n```", markdown)
        self.assertIn("```text\n[00:00] Speaker: Hello\n```", markdown)
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


if __name__ == "__main__":
    unittest.main()
