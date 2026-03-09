"""Regression tests for chunked cohesive summarization helpers."""

import json
import unittest

from src.summarization import cohesive
from src.summarization.prompts import MEETING_TEMPLATE


class CohesiveSummarizationRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_meeting_template_has_stricter_action_and_summary_rules(self):
        self.assertIn("2-3 bullets preferred; use 4 only if clearly necessary.", MEETING_TEMPLATE)
        self.assertIn("Do not use the Summary to restate every section.", MEETING_TEMPLATE)
        self.assertIn("At most one Summary bullet should primarily describe actions.", MEETING_TEMPLATE)
        self.assertIn("Only include concrete follow-up tasks that the meeting clearly established.", MEETING_TEMPLATE)
        self.assertIn("recommendations", MEETING_TEMPLATE)
        self.assertIn("If a task is vague, speculative, or not clearly committed, omit it rather than using \"TBD\".", MEETING_TEMPLATE)
        self.assertIn("Each bullet should summarize one important topic, tradeoff, problem, or unresolved question.", MEETING_TEMPLATE)

    def test_pass1_prompt_reinforces_action_filtering_and_topic_compression(self):
        prompt = cohesive._build_pass1_prompt(
            template="meeting",
            template_contract=MEETING_TEMPLATE,
            context_text="[00:00] Daniel: We should probably look into it.",
            context_mode="full_transcript",
            custom_instructions=None,
        )
        self.assertIn("If an item sounds like a recommendation, suggestion, or unresolved idea rather than a committed task, do not put it in Action Items.", prompt)
        self.assertIn("Compress related discussion into stronger topic bullets rather than many small bullets.", prompt)

    def test_pass2_prompt_removes_weak_actions_and_compresses_topics(self):
        prompt = cohesive._build_pass2_prompt(
            "meeting",
            "## Summary\n- Draft\n\n## Action Items\n| Owner | Action | Due |\n|-------|--------|-----|",
        )
        self.assertIn("recommendation, suggestion, question, speculative idea, or vague possibility", prompt)
        self.assertIn("If a task is too vague to act on, remove it rather than preserving it as an action item.", prompt)
        self.assertIn("Keep the Summary focused on top outcomes only", prompt)
        self.assertIn("Compress multiple bullets about the same topic into one stronger bullet where possible.", prompt)
        self.assertIn("Do not strengthen weak wording into commitment or certainty.", prompt)

    def test_extract_json_block_returns_none_for_plain_text(self):
        self.assertIsNone(cohesive._extract_json_block("Segment recap without any JSON payload"))

    async def test_chunk_record_repairs_plain_text_responses(self):
        responses = iter(
            [
                "This segment discussed API migration ownership and an open question.",
                json.dumps(
                    {
                        "segment_summary": "API migration ownership discussion",
                        "discussion_points": [
                            {
                                "timestamp": "[00:01]",
                                "speaker": "SPEAKER_00",
                                "point": "Move the API migration into this week's sprint.",
                                "status": "tentative",
                            }
                        ],
                        "decisions": [],
                        "action_items": [],
                        "open_questions": [
                            {
                                "timestamp": "[00:05]",
                                "speaker": "SPEAKER_01",
                                "question": "Who owns the migration?",
                                "context": "Ownership was not assigned yet.",
                                "owner": None,
                            }
                        ],
                        "risks": [],
                        "unresolved_items": [],
                        "notable_quotes_or_context": [],
                    }
                ),
            ]
        )
        calls: list[tuple[str, str]] = []

        async def fake_llm_call(system_prompt: str, user_prompt: str) -> str:
            calls.append((system_prompt, user_prompt))
            return next(responses)

        chunk = {
            "index": 0,
            "text": (
                "[00:01] SPEAKER_00: We should move the API migration into this week's sprint.\n"
                "[00:05] SPEAKER_01: Who owns that workstream?"
            ),
            "estimated_tokens": 32,
        }

        record, diagnostics = await cohesive._extract_chunk_record(fake_llm_call, chunk, total=1)

        self.assertEqual(len(calls), 2)
        self.assertEqual(record["segment_summary"], "API migration ownership discussion")
        self.assertEqual(record["open_questions"][0]["speaker"], "SPEAKER_01")
        self.assertEqual(diagnostics["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
