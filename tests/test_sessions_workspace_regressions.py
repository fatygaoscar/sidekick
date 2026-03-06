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

    def test_workspace_state_allows_summary_when_attendees_can_resolve_speakers(self):
        session = SimpleNamespace(has_transcription=True)
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=None,
            attendees="Oscar, Jane",
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
        self.assertTrue(state["can_resolve_speakers_from_attendees"])

    def test_summary_job_gate_allows_pending_review_when_attendees_exist(self):
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=None,
            attendees="Oscar, Jane",
        )

        self.assertFalse(self.export._speaker_review_blocks_summary(meeting))

    def test_summary_job_gate_blocks_pending_review_without_attendees(self):
        meeting = SimpleNamespace(
            speaker_review_required=True,
            speaker_review_completed_at=None,
            attendees="  ",
        )

        self.assertTrue(self.export._speaker_review_blocks_summary(meeting))


if __name__ == "__main__":
    unittest.main()
