"""Regression tests for sessions workspace behavior."""

import asyncio
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch
import wave
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException


class SessionsWorkspaceRegressionTests(unittest.TestCase):
    """Guardrails for router import order and stale-state behavior."""

    @classmethod
    def setUpClass(cls):
        cls.sessions = importlib.import_module("src.api.routes.sessions")
        cls.export = importlib.import_module("src.api.routes.export")
        cls.audio_clips = importlib.import_module("src.audio.clips")

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

    def test_get_app_settings_returns_serialized_flags(self):
        repository = SimpleNamespace(
            get_app_settings=AsyncMock(
                return_value=SimpleNamespace(workspace_chat_enabled=True)
            )
        )

        payload = asyncio.run(
            self.sessions.get_app_settings(repository=repository)
        )

        self.assertEqual(payload, {"settings": {"workspace_chat_enabled": True}})

    def test_update_app_settings_requires_at_least_one_field(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                self.sessions.update_app_settings(
                    self.sessions.UpdateAppSettingsRequest(),
                    repository=SimpleNamespace(update_app_settings=AsyncMock()),
                )
            )

        self.assertEqual(ctx.exception.status_code, 422)

    def test_update_app_settings_persists_feature_flags(self):
        repository = SimpleNamespace(
            update_app_settings=AsyncMock(
                return_value=SimpleNamespace(workspace_chat_enabled=True)
            )
        )

        payload = asyncio.run(
            self.sessions.update_app_settings(
                self.sessions.UpdateAppSettingsRequest(workspace_chat_enabled=True),
                repository=repository,
            )
        )

        self.assertEqual(payload, {"settings": {"workspace_chat_enabled": True}})
        repository.update_app_settings.assert_awaited_once()

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

    def test_active_transcription_job_lookup_ignores_completed_jobs(self):
        original_jobs = dict(self.export._TRANSCRIPTION_JOBS)
        try:
            self.export._TRANSCRIPTION_JOBS.clear()
            active_job = self.export._create_transcription_job("session-1")
            self.export._update_transcription_job(
                active_job["job_id"],
                status="running",
                stage="transcribing",
                message="Transcribing",
            )

            self.assertEqual(
                self.export._find_active_transcription_job("session-1")["job_id"],
                active_job["job_id"],
            )

            self.export._update_transcription_job(
                active_job["job_id"],
                status="completed",
                stage="completed",
                message="Transcription complete",
            )

            self.assertIsNone(self.export._find_active_transcription_job("session-1"))
        finally:
            self.export._TRANSCRIPTION_JOBS.clear()
            self.export._TRANSCRIPTION_JOBS.update(original_jobs)

    def test_start_transcription_job_reuses_existing_active_job(self):
        original_jobs = dict(self.export._TRANSCRIPTION_JOBS)
        try:
            self.export._TRANSCRIPTION_JOBS.clear()
            active_job = self.export._create_transcription_job("session-1")
            self.export._update_transcription_job(
                active_job["job_id"],
                status="running",
                stage="transcribing",
                message="Transcribing",
            )

            async def get_session(_session_id):
                return SimpleNamespace(id="session-1", meetings=[])

            response = asyncio.run(
                self.export.start_transcription_job(
                    "session-1",
                    repository=SimpleNamespace(get_session=get_session),
                    transcription_manager=SimpleNamespace(),
                )
            )

            self.assertEqual(response.job_id, active_job["job_id"])
            self.assertEqual(response.status, "running")
            self.assertEqual(len(self.export._TRANSCRIPTION_JOBS), 1)
        finally:
            self.export._TRANSCRIPTION_JOBS.clear()
            self.export._TRANSCRIPTION_JOBS.update(original_jobs)

    def test_revise_summary_draft_returns_updated_draft(self):
        draft = SimpleNamespace(id="draft-1", status="draft", content="Original summary")
        updated = SimpleNamespace(id="draft-1", content="Revised summary")
        repository = SimpleNamespace(
            get_summary=AsyncMock(return_value=draft),
            update_summary=AsyncMock(return_value=updated),
        )
        summarization_manager = SimpleNamespace(
            refine_summary=AsyncMock(return_value="Revised summary")
        )

        payload = asyncio.run(
            self.export.revise_summary_draft(
                "draft-1",
                self.export.DraftReviseRequest(instruction="Tighten the takeaways."),
                repository=repository,
                summarization_manager=summarization_manager,
            )
        )

        self.assertEqual(
            payload,
            {"draft_summary_id": "draft-1", "content": "Revised summary"},
        )
        summarization_manager.refine_summary.assert_awaited_once_with(
            instruction="Tighten the takeaways.",
            current_summary="Original summary",
        )
        repository.update_summary.assert_awaited_once_with(
            "draft-1",
            content="Revised summary",
            source_type="ai_revised",
        )

    def test_revise_summary_draft_returns_conflict_when_draft_disappears(self):
        draft = SimpleNamespace(id="draft-1", status="draft", content="Original summary")
        repository = SimpleNamespace(
            get_summary=AsyncMock(return_value=draft),
            update_summary=AsyncMock(return_value=None),
        )
        summarization_manager = SimpleNamespace(
            refine_summary=AsyncMock(return_value="Revised summary")
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                self.export.revise_summary_draft(
                    "draft-1",
                    self.export.DraftReviseRequest(instruction="Tighten the takeaways."),
                    repository=repository,
                    summarization_manager=summarization_manager,
                )
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            ctx.exception.detail,
            "Draft changed while AI revision was running. Reload the workspace and try again.",
        )

    def test_post_workspace_chat_message_returns_serialized_turns(self):
        session = SimpleNamespace(id="session-1", has_transcription=True)
        meeting = SimpleNamespace(id="meeting-1", template_key="meeting")
        transcript_version = SimpleNamespace(id="tv-1", version_number=1, template_key="meeting")
        selected_summary = SimpleNamespace(
            id="sum-1",
            content="Current summary",
            status="saved",
            source_type="generated",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            saved_to_obsidian_at=None,
            obsidian_relative_path=None,
        )
        user_message = SimpleNamespace(
            id="chat-user-1",
            thread_id="thread-1",
            role="user",
            message_type="user_question",
            content="What did Greg decide?",
            transcript_version_id="tv-1",
            summary_id="sum-1",
            citations_json=None,
            retrieval_windows_json=None,
            intent_label=None,
            intent_confidence=None,
            suggests_summary_change=False,
            suggested_change_kind=None,
            apply_ready=False,
            applied_summary_id=None,
            applied_draft_summary_id=None,
            metadata_json=None,
            created_at=datetime.now(UTC),
        )
        assistant_message = SimpleNamespace(
            id="chat-assistant-1",
            thread_id="thread-1",
            role="assistant",
            message_type="assistant_answer",
            content="Greg decided to keep the rollout phased.",
            transcript_version_id="tv-1",
            summary_id="sum-1",
            citations_json="[0]",
            retrieval_windows_json='[{"timestamp":"[01:02]","speaker":"Greg","transcript_segment_ids":["seg-1"]}]',
            intent_label="clarify_decision",
            intent_confidence=0.91,
            suggests_summary_change=True,
            suggested_change_kind="clarify",
            apply_ready=True,
            applied_summary_id=None,
            applied_draft_summary_id=None,
            metadata_json='{"confidence":"high"}',
            created_at=datetime.now(UTC),
        )
        repository = SimpleNamespace(
            get_app_settings=AsyncMock(return_value=SimpleNamespace(workspace_chat_enabled=True)),
            get_session=AsyncMock(return_value=session),
            get_primary_meeting=AsyncMock(return_value=meeting),
            ensure_transcript_versions=AsyncMock(return_value=[transcript_version]),
            get_latest_transcript_version=AsyncMock(return_value=transcript_version),
            get_summaries=AsyncMock(return_value=[selected_summary]),
            get_draft_summary=AsyncMock(return_value=None),
        )
        fake_service = SimpleNamespace(
            send_message=AsyncMock(return_value=(user_message, assistant_message))
        )

        with patch.object(self.sessions, "WorkspaceChatService", return_value=fake_service):
            payload = asyncio.run(
                self.sessions.post_workspace_chat_message(
                    "session-1",
                    self.sessions.WorkspaceChatMessageRequest(content="What did Greg decide?"),
                    repository=repository,
                    summarization_manager=SimpleNamespace(),
                )
            )

        self.assertEqual(payload["user_message"]["id"], "chat-user-1")
        self.assertEqual(payload["assistant_message"]["citations"], [0])
        self.assertTrue(payload["assistant_message"]["apply_ready"])
        fake_service.send_message.assert_awaited_once()

    def test_apply_workspace_chat_message_returns_serialized_draft(self):
        session = SimpleNamespace(id="session-1", has_transcription=True)
        meeting = SimpleNamespace(id="meeting-1", template_key="meeting")
        transcript_version = SimpleNamespace(id="tv-1", version_number=1, template_key="meeting")
        selected_summary = SimpleNamespace(
            id="sum-1",
            content="Current summary",
            status="saved",
            source_type="generated",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            saved_to_obsidian_at=None,
            obsidian_relative_path=None,
        )
        draft_summary = SimpleNamespace(
            id="draft-1",
            content="Updated summary",
            status="draft",
            source_type="chat_applied",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            saved_to_obsidian_at=None,
            obsidian_relative_path=None,
        )
        system_message = SimpleNamespace(
            id="system-1",
            thread_id="thread-1",
            role="system",
            message_type="apply_event",
            content="Applied the assistant suggestion to the current draft.",
            transcript_version_id="tv-1",
            summary_id="draft-1",
            citations_json=None,
            retrieval_windows_json=None,
            intent_label=None,
            intent_confidence=None,
            suggests_summary_change=False,
            suggested_change_kind=None,
            apply_ready=False,
            applied_summary_id="sum-1",
            applied_draft_summary_id="draft-1",
            metadata_json=None,
            created_at=datetime.now(UTC),
        )
        assistant_message = SimpleNamespace(
            id="chat-assistant-1",
            session_id="session-1",
            role="assistant",
            message_type="assistant_answer",
            apply_ready=True,
            content="Add Greg's decision.",
        )
        repository = SimpleNamespace(
            get_app_settings=AsyncMock(return_value=SimpleNamespace(workspace_chat_enabled=True)),
            get_session=AsyncMock(return_value=session),
            get_primary_meeting=AsyncMock(return_value=meeting),
            ensure_transcript_versions=AsyncMock(return_value=[transcript_version]),
            get_latest_transcript_version=AsyncMock(return_value=transcript_version),
            get_summaries=AsyncMock(return_value=[selected_summary]),
            get_draft_summary=AsyncMock(return_value=None),
            get_workspace_chat_message=AsyncMock(return_value=assistant_message),
        )
        fake_service = SimpleNamespace(
            apply_message_to_summary=AsyncMock(
                return_value={
                    "draft_summary": draft_summary,
                    "changed": True,
                    "reason": None,
                    "system_message": system_message,
                }
            )
        )

        with patch.object(self.sessions, "WorkspaceChatService", return_value=fake_service):
            payload = asyncio.run(
                self.sessions.apply_workspace_chat_message(
                    "session-1",
                    "chat-assistant-1",
                    self.sessions.WorkspaceChatApplyRequest(summary_id="sum-1"),
                    repository=repository,
                    summarization_manager=SimpleNamespace(),
                )
            )

        self.assertTrue(payload["changed"])
        self.assertEqual(payload["draft_summary"]["id"], "draft-1")
        self.assertEqual(payload["system_message"]["message_type"], "apply_event")
        fake_service.apply_message_to_summary.assert_awaited_once()

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

    def test_speaker_cards_include_clip_url(self):
        segments = [
            SimpleNamespace(
                id="seg-1",
                start_time=12.5,
                end_time=14.0,
                text="Let's ship this Friday.",
                speaker="SPEAKER_00",
                speaker_cluster="SPEAKER_00",
                is_important=False,
            )
        ]

        cards = self.sessions._build_speaker_cards(segments, "session-1", "tv-1")

        self.assertEqual(len(cards), 1)
        self.assertEqual(
            cards[0]["clip_url"],
            "/api/recordings/session-1/speaker-clips/SPEAKER_00/audio?transcript_version_id=tv-1",
        )

    def test_ensure_speaker_clip_generates_cached_wav(self):
        session_id = "clip-test-session"
        self.audio_clips.cleanup_speaker_clip_cache(session_id)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                source_path = Path(tmpdir) / "source.wav"
                with wave.open(str(source_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(16000)
                    wav_file.writeframes(b"\x00\x00" * 16000)

                clip_path = self.audio_clips.ensure_speaker_clip(
                    audio_path=source_path,
                    session_id=session_id,
                    transcript_version_id="tv-1",
                    speaker_key="SPEAKER_00",
                    start_time=0.10,
                    end_time=0.35,
                )
                cached_path = self.audio_clips.ensure_speaker_clip(
                    audio_path=source_path,
                    session_id=session_id,
                    transcript_version_id="tv-1",
                    speaker_key="SPEAKER_00",
                    start_time=0.10,
                    end_time=0.35,
                )

                self.assertEqual(clip_path, cached_path)
                self.assertTrue(clip_path.exists())
                with wave.open(str(clip_path), "rb") as wav_file:
                    self.assertEqual(wav_file.getnchannels(), 1)
                    self.assertEqual(wav_file.getframerate(), 16000)
                    self.assertGreater(wav_file.getnframes(), 0)
        finally:
            self.audio_clips.cleanup_speaker_clip_cache(session_id)

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
