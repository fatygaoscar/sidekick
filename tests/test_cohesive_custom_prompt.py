import unittest

from src.summarization.cohesive import _build_pass1_prompt, _build_pass2_prompt


class CohesiveCustomPromptTests(unittest.TestCase):
    def test_custom_pass1_does_not_force_action_items_table(self):
        prompt = _build_pass1_prompt(
            template="custom",
            template_contract="## Output\n- Keep it simple.",
            context_text="[00:00] Attendee: Hello",
            context_mode="full_transcript",
            custom_instructions=None,
        )

        self.assertIn("Follow the Style Contract above exactly.", prompt)
        self.assertNotIn("For Action Items, always use a markdown table", prompt)
        self.assertNotIn("Use the same section headers (##) as specified in the Style Contract.", prompt)

    def test_custom_pass2_preserves_scope_without_forcing_tables(self):
        prompt = _build_pass2_prompt("custom", "## Output\n- Keep it simple.")

        self.assertIn("Do not add new sections, tables, or extra structure", prompt)
        self.assertIn("If it is concise, keep it concise.", prompt)
        self.assertNotIn("Ensure Action Items is a clean markdown table", prompt)


if __name__ == "__main__":
    unittest.main()
