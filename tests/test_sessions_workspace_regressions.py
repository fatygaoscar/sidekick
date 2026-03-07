"""Regression tests for sessions workspace behavior."""

import importlib
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


class SessionsWorkspaceRegressionTests(unittest.TestCase):
    """Guardrails for router import order and stale-state behavior."""

    @classmethod
    def setUpClass(cls):
        cls.sessions = importlib.import_module("src.api.routes.sessions")
        cls.export = importlib.import_module("src.api.routes.export")

    def test_sessions_router_imports_cleanly(self):
        self.assertTrue(hasattr(self.sessions, "UpdateRecordingSettingsRequest"))
        self.assertTrue(hasattr(self.sessions, "UpdateSpeakerAssignmentsRequest"))

    def test_summary_not_stale_when_review_was_never_required(self):
        meeting = SimpleNamespace(
            speaker_review_required=False,
            speaker_review_completed_at=datetime.now(UTC),
            template_key="meeting",
            custom_prompt=None,
            attendees=None,
        )
        summary = SimpleNamespace(
            created_at=datetime.now(UTC) - timedelta(days=1),
            template_key="meeting",
            custom_prompt=None,
            attendees_snapshot=None,
        )

        self.assertFalse(self.sessions._summary_is_out_of_date(meeting, summary))

    def test_summary_stale_after_required_speaker_review(self):
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=datetime.now(UTC),
            template_key="meeting",
            custom_prompt=None,
            attendees=None,
        )
        summary = SimpleNamespace(
            created_at=datetime.now(UTC) - timedelta(days=1),
            template_key="meeting",
            custom_prompt=None,
            attendees_snapshot=None,
        )

        self.assertTrue(self.sessions._summary_is_out_of_date(meeting, summary))
        self.assertEqual(
            self.sessions._summary_out_of_date_reason(meeting, summary),
            "Speaker assignments changed after this summary was generated.",
        )

    def test_summary_serialization_tracks_stale_state_per_version(self):
        meeting = SimpleNamespace(
            speaker_review_required=False,
            speaker_review_completed_at=None,
            template_key="strategic_review",
            custom_prompt="Focus on risks.",
        )
        matching_summary = SimpleNamespace(
            id="sum-1",
            meeting_id="meeting-1",
            content="Latest summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            processing_duration_seconds=12.0,
            template="Strategic Review",
            template_key="strategic_review",
            custom_prompt="Focus on risks.",
            status="saved",
            source_type="generated",
            saved_to_obsidian_at=datetime.now(UTC) - timedelta(minutes=50),
            obsidian_relative_path="Sidekick/Latest.md",
        )
        older_summary = SimpleNamespace(
            id="sum-2",
            meeting_id="meeting-1",
            content="Older summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(days=1),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="generated",
            saved_to_obsidian_at=datetime.now(UTC) - timedelta(days=1),
            obsidian_relative_path="Sidekick/v1.md",
        )

        latest_payload = self.sessions._serialize_summary(matching_summary, meeting)
        older_payload = self.sessions._serialize_summary(older_summary, meeting)

        self.assertFalse(latest_payload["summary_out_of_date"])
        self.assertIsNone(latest_payload["summary_out_of_date_reason"])
        self.assertTrue(older_payload["summary_out_of_date"])
        self.assertEqual(
            older_payload["summary_out_of_date_reason"],
            "Summary settings changed to a different template.",
        )

    def test_update_recording_settings_can_clear_custom_prompt(self):
        request = self.sessions.UpdateRecordingSettingsRequest(custom_prompt=None)

        self.assertIn("custom_prompt", request.model_fields_set)

        normalized_custom_prompt = self.sessions.UNSET
        if "custom_prompt" in request.model_fields_set:
            normalized_custom_prompt = (
                request.custom_prompt.strip() or None
                if request.custom_prompt is not None
                else None
            )

        self.assertIsNone(normalized_custom_prompt)

    def test_workspace_state_allows_summary_when_speaker_review_is_pending(self):
        session = SimpleNamespace(has_transcription=True)
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=None,
        )

        state = self.sessions._build_recording_workspace_state(
            session=session,
            meeting=meeting,
            segments=[],
            saved_summaries=[],
            draft_summary=None,
            latest_saved_summary=None,
        )

        self.assertTrue(state["requires_speaker_review"])
        self.assertTrue(state["can_generate_summary"])
        self.assertNotIn("can_resolve_speakers_from_attendees", state)

    def test_summary_job_gate_does_not_block_pending_review(self):
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=None,
        )

        self.assertFalse(self.export._speaker_review_blocks_summary(meeting))

    def test_workspace_transcript_humanizes_unresolved_speakers(self):
        segments = [
            SimpleNamespace(
                id="seg-1",
                start_time=0.0,
                end_time=5.0,
                text="We should ship on Friday.",
                speaker="SPEAKER_00",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-2",
                start_time=5.0,
                end_time=9.0,
                text="I can own the rollout.",
                speaker="SPEAKER_01",
                speaker_cluster="SPEAKER_01",
                is_important=False,
            ),
        ]

        transcript = self.sessions._serialize_transcript_segments(segments)

        self.assertEqual(transcript[0]["speaker"], "Attendee A")
        self.assertEqual(transcript[1]["speaker"], "Attendee B")

    def test_workspace_transcript_marks_truly_unresolved_speakers_with_question_mark(self):
        segments = [
            SimpleNamespace(
                id="seg-1",
                start_time=0.0,
                end_time=0.3,
                text="Hey Pam.",
                speaker="Oscar",
                speaker_cluster="SPEAKER_01",
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-2",
                start_time=0.4,
                end_time=3.36,
                text="Hi, how are you?",
                speaker=None,
                speaker_cluster=None,
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-3",
                start_time=3.56,
                end_time=4.16,
                text="Good, how are you?",
                speaker="Oscar",
                speaker_cluster="SPEAKER_01",
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-4",
                start_time=4.32,
                end_time=5.89,
                text="Pretty good.",
                speaker=None,
                speaker_cluster=None,
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-5",
                start_time=7.4,
                end_time=20.0,
                text="Let's pull up the planner.",
                speaker="Pam",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            ),
        ]

        transcript = self.sessions._serialize_transcript_segments(segments)

        self.assertEqual(transcript[1]["speaker"], "?")
        self.assertEqual(transcript[3]["speaker"], "?")

    def test_workspace_transcript_inferrs_short_same_speaker_gap(self):
        segments = [
            SimpleNamespace(
                id="seg-1",
                start_time=0.0,
                end_time=1.0,
                text="The jobs start at four in the morning.",
                speaker="Pam",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-2",
                start_time=1.16,
                end_time=3.06,
                text="So I took a few liberties.",
                speaker=None,
                speaker_cluster=None,
                is_important=False,
            ),
            SimpleNamespace(
                id="seg-3",
                start_time=3.9,
                end_time=5.4,
                text="Only because it felt cleaner.",
                speaker="Pam",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            ),
        ]

        transcript = self.sessions._serialize_transcript_segments(segments)

        self.assertEqual(transcript[1]["speaker"], "Pam")

    def test_export_transcript_uses_attendee_label_for_single_unresolved_speaker(self):
        segments = [
            SimpleNamespace(
                start_time=0.0,
                end_time=5.0,
                text="I will handle the follow-up.",
                speaker="SPEAKER_00",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            )
        ]

        transcript, _ = self.export._segments_to_transcript(segments)

        self.assertIn("Attendee: I will handle the follow-up.", transcript)
        self.assertNotIn("SPEAKER_00", transcript)

    def test_export_transcript_keeps_truly_unresolved_segments_unattributed(self):
        segments = [
            SimpleNamespace(
                start_time=0.0,
                end_time=0.3,
                text="Hey Pam.",
                speaker="Oscar",
                speaker_cluster="SPEAKER_01",
                is_important=False,
            ),
            SimpleNamespace(
                start_time=0.4,
                end_time=1.97,
                text="Pretty good.",
                speaker=None,
                speaker_cluster=None,
                is_important=False,
            ),
            SimpleNamespace(
                start_time=3.48,
                end_time=18.0,
                text="Let's walk through the planner.",
                speaker="Pam",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            ),
        ]

        transcript, _ = self.export._segments_to_transcript(segments)

        self.assertIn("[00:00] Oscar: Hey Pam.", transcript)
        self.assertIn("[00:00] Pretty good.", transcript)
        self.assertNotIn("?: Pretty good.", transcript)


if __name__ == "__main__":
    unittest.main()
