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


if __name__ == "__main__":
    unittest.main()
