"""Regression tests for chunked cohesive summarization helpers."""

import json
import unittest

from src.summarization import cohesive


class CohesiveSummarizationRegressionTests(unittest.IsolatedAsyncioTestCase):
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
