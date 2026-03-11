"""Regression tests for the concise default summarization pipeline."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config.settings import Settings, SummarizationBackend
from src.summarization.base import BackendProbeResult, SummarizationResult
from src.summarization.manager import SummarizationManager
from src.summarization.pipeline.classifier import classify_items
from src.summarization.pipeline.pipeline import run_pipeline
from src.summarization.pipeline.ranking import select_for_render
from src.summarization.pipeline.render_meeting import render_meeting_summary
from src.summarization.pipeline.types import (
    EvidenceSpan,
    ExtractedItem,
    MeetingContextProfile,
    PipelineResult,
    StructuredItems,
    TopicSegment,
)
from src.summarization.pipeline.validation import validate_items, validated_to_structured


class ConciseMeetingSummarizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_keeps_backend_snapshot_for_in_flight_summary(self):
        settings = Settings(summarization_backend=SummarizationBackend.OLLAMA)
        manager = SummarizationManager(settings)
        manager._event_bus = SimpleNamespace(emit=AsyncMock())

        class FakeBackend:
            def __init__(self, name):
                self._name = name
                self._initialized = False

            @property
            def name(self):
                return self._name

            @property
            def model(self):
                return f"{self._name}-model"

            @property
            def is_local(self):
                return self._name == "ollama"

            @property
            def supports_structured_outputs(self):
                return self._name == "openai"

            async def initialize(self):
                self._initialized = True

            async def shutdown(self):
                self._initialized = False

            async def summarize(self, transcript, system_prompt=None, user_prompt=None, num_ctx=None, json_mode=False, max_output_tokens=None):
                del transcript, system_prompt, user_prompt, num_ctx, json_mode, max_output_tokens
                return SummarizationResult(content=self._name, backend=self._name, model=f"{self._name}-model")

            async def probe(self):
                return BackendProbeResult(provider=self._name, model=f"{self._name}-model", ready=True, message="Ready")

        manager._create_backend = lambda backend: FakeBackend(backend.value)

        llm_outputs = []

        async def fake_generate(**kwargs):
            llm_call = kwargs["llm_call"]
            llm_outputs.append(await llm_call("system-1", "user-1"))
            await manager.switch_backend(SummarizationBackend.OPENAI)
            llm_outputs.append(await llm_call("system-2", "user-2"))
            return "summary", "full_transcript", 2, "style", {}, {}

        with patch("src.summarization.manager.generate_cohesive_summary", new=AsyncMock(side_effect=fake_generate)):
            result = await manager.summarize(
                transcript="[00:00] Daniel: We agreed on the rollout path.",
                prompt_type="meeting",
            )

        self.assertEqual(result.backend, "ollama")
        self.assertEqual(llm_outputs, ["ollama", "ollama"])
        self.assertEqual(manager.active_backend_type, SummarizationBackend.OPENAI)

    def test_classifier_and_validation_keep_reliability_rules(self):
        items = [
            ExtractedItem(
                id="",
                type="decision",
                text="Should recommendations use the top 90% of inventory?",
                speaker="Daniel",
                status="confirmed",
                evidence=EvidenceSpan(turn_ids=["turn_0001"], quote="Should recommendations use the top 90%?"),
            ),
            ExtractedItem(
                id="",
                type="action",
                text="Follow up on return-option logic",
                speaker="SWE Lead",
                owner="Oscar",
                status="open",
                evidence=EvidenceSpan(turn_ids=["turn_0002"], quote="Someone should follow up."),
            ),
        ]

        classified = classify_items(items, known_participants=["Daniel", "SWE Lead"])
        validated, warnings = validate_items(classified)
        structured = validated_to_structured(validated)

        self.assertFalse(structured.decisions)
        self.assertEqual(structured.questions[0].text, "Should recommendations use the top 90% of inventory?")
        self.assertEqual(structured.actions[0].owner, "TBD")
        self.assertTrue(any("owner unresolved" in warning for warning in warnings))

    def test_ranking_enforces_section_budgets(self):
        structured = StructuredItems(
            decisions=[
                ExtractedItem(
                    id=f"D-{index}",
                    type="decision",
                    text=f"Decision {index}",
                    confidence=0.9,
                    importance=0.9,
                    outcome_relevance=0.9,
                    status="confirmed",
                    evidence=EvidenceSpan(turn_ids=[f"turn_{index:04d}"], quote=f"Decision {index}"),
                )
                for index in range(7)
            ],
            actions=[
                ExtractedItem(
                    id=f"A-{index}",
                    type="action",
                    text=f"Action {index}",
                    owner="TBD",
                    confidence=0.85,
                    importance=0.82,
                    outcome_relevance=0.88,
                    status="open",
                    evidence=EvidenceSpan(turn_ids=[f"turn_{index+10:04d}"], quote=f"Action {index}"),
                )
                for index in range(8)
            ],
            discussion_points=[
                ExtractedItem(
                    id=f"P-{index}",
                    type="discussion_point",
                    text=f"Discussion point {index}",
                    speaker="Daniel",
                    confidence=0.8,
                    importance=0.7,
                    outcome_relevance=0.72,
                    status="open",
                    topic_id=f"topic_{(index // 2) + 1:03d}",
                    topic_key=f"topic_{index // 2}",
                    evidence=EvidenceSpan(turn_ids=[f"turn_{index+20:04d}"], quote=f"Discussion point {index}"),
                )
                for index in range(10)
            ],
        )
        topics = [
            TopicSegment(
                topic_id=f"topic_{index:03d}",
                thread_id=f"block_{index:03d}",
                label=f"Topic {index}",
                topic_type="general_business",
                start_time=float(index),
                end_time=float(index + 1),
                turn_ids=[f"turn_{index:04d}"],
            )
            for index in range(1, 6)
        ]

        selection = select_for_render(structured, topics)

        self.assertEqual(len(selection.decisions), 5)
        self.assertEqual(len(selection.actions), 6)
        self.assertLessEqual(len(selection.discussion_topics), 3)
        self.assertLessEqual(len(selection.summary_items), 3)

    def test_render_meeting_summary_is_concise_and_compressed(self):
        context = MeetingContextProfile(
            ui_summary_mode="auto",
            primary_mode="general_business",
            render_profile="concise_default_v1",
        )
        topics = [
            TopicSegment(
                topic_id="topic_001",
                thread_id="block_001",
                label="Recommendation Logic",
                topic_type="design_or_solution",
                start_time=10.0,
                end_time=40.0,
                turn_ids=["turn_0001"],
            ),
            TopicSegment(
                topic_id="topic_002",
                thread_id="block_002",
                label="Rollout Dependencies",
                topic_type="rollout_or_process",
                start_time=80.0,
                end_time=120.0,
                turn_ids=["turn_0004"],
            ),
        ]
        items = StructuredItems(
            decisions=[
                ExtractedItem(
                    id="D-001",
                    type="decision",
                    text="Pilot the sell-down recommendation logic first",
                    status="confirmed",
                    confidence=0.92,
                    importance=0.9,
                    outcome_relevance=0.95,
                    topic_id="topic_001",
                    evidence=EvidenceSpan(turn_ids=["turn_0002"], quote="Let's pilot the sell-down logic first."),
                )
            ],
            actions=[
                ExtractedItem(
                    id="A-001",
                    type="action",
                    text="Draft the rollout notes",
                    owner="TBD",
                    due_date="Friday",
                    status="open",
                    confidence=0.82,
                    importance=0.85,
                    outcome_relevance=0.9,
                    topic_id="topic_002",
                    evidence=EvidenceSpan(turn_ids=["turn_0005"], quote="Someone should draft the rollout notes by Friday."),
                )
            ],
            constraints=[
                ExtractedItem(
                    id="C-001",
                    type="constraint",
                    text="Size-level recommendations should focus on sell down",
                    speaker="SWE Lead",
                    status="active",
                    confidence=0.9,
                    importance=0.86,
                    outcome_relevance=0.88,
                    topic_id="topic_001",
                    topic_key="recommendation_logic",
                    evidence=EvidenceSpan(turn_ids=["turn_0004"], quote="Size-level behavior should focus on sell down."),
                )
            ],
            questions=[
                ExtractedItem(
                    id="Q-001",
                    type="question",
                    text="Should inventory cutoffs be based on the top 90%?",
                    speaker="Daniel",
                    status="open",
                    confidence=0.84,
                    importance=0.72,
                    outcome_relevance=0.74,
                    topic_id="topic_001",
                    topic_key="recommendation_logic",
                    evidence=EvidenceSpan(turn_ids=["turn_0001"], quote="Should recommendations be based on the top 90% of inventory?"),
                )
            ],
            discussion_points=[
                ExtractedItem(
                    id="P-001",
                    type="discussion_point",
                    text="Use return options only in SKU-level views",
                    speaker="SWE Lead",
                    status="open",
                    confidence=0.88,
                    importance=0.78,
                    outcome_relevance=0.76,
                    topic_id="topic_001",
                    topic_key="recommendation_logic",
                    evidence=EvidenceSpan(turn_ids=["turn_0001"], quote="SKU-level views could include return options."),
                )
            ],
        )

        markdown = render_meeting_summary(items, context, topics)

        self.assertIn("## Summary", markdown)
        self.assertIn("## Key Decisions", markdown)
        self.assertIn("## Action Items", markdown)
        self.assertIn("## Discussion Notes", markdown)
        self.assertLessEqual(markdown.split("## Summary", 1)[1].split("## Key Decisions", 1)[0].count("\n- "), 3)
        self.assertIn("### Recommendation Logic", markdown)
        self.assertIn("Constraint: Size-level recommendations should focus on sell down.", markdown)
        self.assertIn("Open question: Should inventory cutoffs be based on the top 90%?.", markdown)
        self.assertIn("| TBD | Draft the rollout notes | Friday |", markdown)

    async def test_pipeline_caps_extraction_calls_by_length(self):
        transcript = "\n".join(
            f"[{index:02d}:00] Speaker: line {index} decision action follow up constraint issue with rollout dependency and additional implementation detail to increase transcript length"
            for index in range(30)
        )

        async def fake_llm(_system: str, _user: str) -> str:
            return """
            {
              "decisions": [{"text": "Use adaptive rendering", "turn_ids": ["turn_0001"], "confidence": 0.9, "importance": 0.9, "outcome_relevance": 0.9, "status": "confirmed"}],
              "actions": [{"text": "Draft rollout notes", "owner": null, "turn_ids": ["turn_0002"], "confidence": 0.85, "importance": 0.85, "outcome_relevance": 0.88, "status": "open"}],
              "constraints": [{"text": "Launch depends on final QA approval", "turn_ids": ["turn_0003"], "confidence": 0.82, "importance": 0.83, "outcome_relevance": 0.84, "status": "active"}],
              "issues": [],
              "questions": [],
              "discussion_points": []
            }
            """

        result = await run_pipeline(
            transcript=transcript,
            template="auto",
            llm_call=fake_llm,
            backend_name="fake",
            model_name="fake-model",
        )

        self.assertIn(result.routing_metadata["routing_mode"], {"two_block", "three_block"})
        self.assertLessEqual(result.routing_metadata["extraction_calls"], 3)
        self.assertIn("segmentation", result.timings_ms)
        self.assertIn("validation", result.timings_ms)

    async def test_manager_uses_cohesive_path_even_when_flag_enabled(self):
        settings = Settings(
            summarization_backend=SummarizationBackend.OLLAMA,
            summarization_meeting_structured_enabled=True,
        )
        manager = SummarizationManager(settings)
        manager._initialized = True
        manager._active_backend = SimpleNamespace(name="fake", model="fake-model")
        manager._event_bus = SimpleNamespace(emit=AsyncMock())

        with patch.object(manager, "process_with_pipeline", AsyncMock()) as process_mock, patch.object(
            manager,
            "_summarize_with_timeout",
            AsyncMock(return_value=SummarizationResult(content="fallback", backend="fake", model="fake-model")),
        ):
            result = await manager.summarize(
                transcript="[00:00] Daniel: We agreed to use concise rendering.",
                prompt_type="auto",
            )

        self.assertEqual(result.content, "fallback")
        self.assertEqual(result.workflow_data, {})
        process_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
