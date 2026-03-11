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
        cls.audio_storage = importlib.import_module("src.audio.storage")

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

    def test_summary_serialization_includes_revision_history(self):
        meeting = SimpleNamespace(
            speaker_review_required=False,
            speaker_review_completed_at=None,
            template_key="meeting",
            custom_prompt=None,
            attendees=None,
        )
        summary = SimpleNamespace(
            id="sum-1",
            meeting_id="meeting-1",
            content="Latest summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="ai_revised",
            saved_to_obsidian_at=datetime.now(UTC) - timedelta(minutes=50),
            obsidian_relative_path="Sidekick/Latest.md",
            workflow_data_json='{"revision_history":[{"instruction":"Add more detail","used_transcript_context":true}]}',
        )

        payload = self.sessions._serialize_summary(summary, meeting)

        self.assertEqual(len(payload["revision_history"]), 1)
        self.assertEqual(payload["revision_history"][0]["instruction"], "Add more detail")
        self.assertTrue(payload["latest_revision"]["used_transcript_context"])

    def test_summary_serialization_marks_saved_copy_and_version_labels(self):
        meeting = SimpleNamespace(
            speaker_review_required=False,
            speaker_review_completed_at=None,
            template_key="meeting",
            custom_prompt=None,
            attendees=None,
        )
        snapshot = SimpleNamespace(
            id="sum-2",
            meeting_id="meeting-1",
            content="Saved copy",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(minutes=5),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="manual_edit",
            saved_to_obsidian_at=None,
            obsidian_relative_path=None,
            workflow_data_json=None,
        )
        exported = SimpleNamespace(
            id="sum-1",
            meeting_id="meeting-1",
            content="Exported summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="generated",
            saved_to_obsidian_at=datetime.now(UTC) - timedelta(hours=1),
            obsidian_relative_path="Sidekick/v1.md",
            workflow_data_json=None,
        )

        snapshot_payload = self.sessions._serialize_summary(snapshot, meeting)

        self.assertEqual(snapshot_payload["save_kind"], "saved_copy")
        self.assertEqual(
            self.sessions._summary_version_label(snapshot, [snapshot, exported]),
            "v2 (Saved Copy)",
        )
        self.assertEqual(
            self.sessions._summary_version_label(exported, [snapshot, exported]),
            "v1 (Latest Exported)",
        )

    def test_summary_serialization_tolerates_malformed_workflow_data(self):
        meeting = SimpleNamespace(
            speaker_review_required=False,
            speaker_review_completed_at=None,
            template_key="meeting",
            custom_prompt=None,
            attendees=None,
        )
        summary = SimpleNamespace(
            id="sum-1",
            meeting_id="meeting-1",
            content="Latest summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="generated",
            saved_to_obsidian_at=datetime.now(UTC) - timedelta(minutes=50),
            obsidian_relative_path="Sidekick/Latest.md",
            workflow_data_json="{not-json",
        )

        payload = self.sessions._serialize_summary(summary, meeting)

        self.assertEqual(payload["revision_history"], [])
        self.assertIsNone(payload["latest_revision"])

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
                return_value=SimpleNamespace(
                    workspace_chat_enabled=True,
                    summarization_backend="openai",
                    recording_capture_mode="whole_room",
                )
            )
        )
        summarization_manager = SimpleNamespace(
            runtime_state=lambda: {
                "selected_backend": "openai",
                "active_backend": "openai",
                "applies_to": "new_requests_only",
                "providers": {},
            }
        )

        payload = asyncio.run(
            self.sessions.get_app_settings(
                repository=repository,
                summarization_manager=summarization_manager,
            )
        )

        self.assertEqual(
            payload,
            {
                "settings": {
                    "workspace_chat_enabled": True,
                    "summarization_backend": "openai",
                    "recording_capture_mode": "whole_room",
                },
                "summarization": {
                    "selected_backend": "openai",
                    "active_backend": "openai",
                    "applies_to": "new_requests_only",
                    "providers": {},
                },
            },
        )

    def test_update_app_settings_requires_at_least_one_field(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                self.sessions.update_app_settings(
                    self.sessions.UpdateAppSettingsRequest(),
                    repository=SimpleNamespace(update_app_settings=AsyncMock()),
                    summarization_manager=SimpleNamespace(
                        active_backend_type=self.sessions.SumBackendEnum.OLLAMA
                    ),
                )
            )

        self.assertEqual(ctx.exception.status_code, 422)

    def test_update_app_settings_persists_feature_flags(self):
        repository = SimpleNamespace(
            update_app_settings=AsyncMock(
                return_value=SimpleNamespace(
                    workspace_chat_enabled=True,
                    summarization_backend="ollama",
                    recording_capture_mode="whole_room",
                )
            )
        )
        summarization_manager = SimpleNamespace(
            active_backend_type=self.sessions.SumBackendEnum.OLLAMA,
            runtime_state=lambda: {
                "selected_backend": "ollama",
                "active_backend": "ollama",
                "applies_to": "new_requests_only",
                "providers": {},
            },
        )

        payload = asyncio.run(
            self.sessions.update_app_settings(
                self.sessions.UpdateAppSettingsRequest(workspace_chat_enabled=True),
                repository=repository,
                summarization_manager=summarization_manager,
            )
        )

        self.assertEqual(
            payload,
            {
                "settings": {
                    "workspace_chat_enabled": True,
                    "summarization_backend": "ollama",
                    "recording_capture_mode": "whole_room",
                },
                "summarization": {
                    "selected_backend": "ollama",
                    "active_backend": "ollama",
                    "applies_to": "new_requests_only",
                    "providers": {},
                },
            },
        )
        repository.update_app_settings.assert_awaited_once()

    def test_update_app_settings_switches_backend_after_successful_probe(self):
        repository = SimpleNamespace(
            update_app_settings=AsyncMock(
                return_value=SimpleNamespace(
                    workspace_chat_enabled=False,
                    summarization_backend="openai",
                    recording_capture_mode="whole_room",
                )
            )
        )
        probe = SimpleNamespace(ready=True, message="Ready")
        summarization_manager = SimpleNamespace(
            active_backend_type=self.sessions.SumBackendEnum.OLLAMA,
            probe_backend=AsyncMock(return_value=probe),
            switch_backend=AsyncMock(),
            runtime_state=lambda: {
                "selected_backend": "openai",
                "active_backend": "openai",
                "applies_to": "new_requests_only",
                "providers": {},
            },
        )

        payload = asyncio.run(
            self.sessions.update_app_settings(
                self.sessions.UpdateAppSettingsRequest(summarization_backend="openai"),
                repository=repository,
                summarization_manager=summarization_manager,
            )
        )

        self.assertEqual(payload["settings"]["summarization_backend"], "openai")
        summarization_manager.probe_backend.assert_awaited_once_with(self.sessions.SumBackendEnum.OPENAI)
        summarization_manager.switch_backend.assert_awaited_once_with(self.sessions.SumBackendEnum.OPENAI)

    def test_update_app_settings_persists_recording_capture_mode(self):
        repository = SimpleNamespace(
            update_app_settings=AsyncMock(
                return_value=SimpleNamespace(
                    workspace_chat_enabled=False,
                    summarization_backend="ollama",
                    recording_capture_mode="single_speaker",
                )
            )
        )
        summarization_manager = SimpleNamespace(
            active_backend_type=self.sessions.SumBackendEnum.OLLAMA,
            runtime_state=lambda: {
                "selected_backend": "ollama",
                "active_backend": "ollama",
                "applies_to": "new_requests_only",
                "providers": {},
            },
        )

        payload = asyncio.run(
            self.sessions.update_app_settings(
                self.sessions.UpdateAppSettingsRequest(recording_capture_mode="single_speaker"),
                repository=repository,
                summarization_manager=summarization_manager,
            )
        )

        self.assertEqual(payload["settings"]["recording_capture_mode"], "single_speaker")
        repository.update_app_settings.assert_awaited_once()

    def test_update_app_settings_rejects_invalid_recording_capture_mode(self):
        repository = SimpleNamespace(update_app_settings=AsyncMock())
        summarization_manager = SimpleNamespace(
            active_backend_type=self.sessions.SumBackendEnum.OLLAMA,
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                self.sessions.update_app_settings(
                    self.sessions.UpdateAppSettingsRequest(recording_capture_mode="unsupported"),
                    repository=repository,
                    summarization_manager=summarization_manager,
                )
            )

        self.assertEqual(ctx.exception.status_code, 422)
        repository.update_app_settings.assert_not_called()

    def test_update_app_settings_rejects_unready_backend(self):
        repository = SimpleNamespace(update_app_settings=AsyncMock())
        probe = SimpleNamespace(ready=False, message="OPENAI_API_KEY is not configured.")
        summarization_manager = SimpleNamespace(
            active_backend_type=self.sessions.SumBackendEnum.OLLAMA,
            probe_backend=AsyncMock(return_value=probe),
            switch_backend=AsyncMock(),
        )

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                self.sessions.update_app_settings(
                    self.sessions.UpdateAppSettingsRequest(summarization_backend="openai"),
                    repository=repository,
                    summarization_manager=summarization_manager,
                )
            )

        self.assertEqual(ctx.exception.status_code, 503)
        summarization_manager.switch_backend.assert_not_called()

    def test_end_session_by_id_reports_ended_for_active_session(self):
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=SimpleNamespace(id="session-1", is_active=True))
        )
        session_manager = SimpleNamespace(
            end_session_by_id=AsyncMock(return_value=SimpleNamespace(id="session-1", is_active=False))
        )

        payload = asyncio.run(
            self.sessions.end_session_by_id(
                "session-1",
                repository=repository,
                session_manager=session_manager,
            )
        )

        self.assertEqual(payload, {"status": "ended", "session_id": "session-1"})

    def test_end_session_by_id_reports_already_ended_for_inactive_session(self):
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=SimpleNamespace(id="session-1", is_active=False))
        )
        session_manager = SimpleNamespace(
            end_session_by_id=AsyncMock(return_value=SimpleNamespace(id="session-1", is_active=False))
        )

        payload = asyncio.run(
            self.sessions.end_session_by_id(
                "session-1",
                repository=repository,
                session_manager=session_manager,
            )
        )

        self.assertEqual(payload, {"status": "already_ended", "session_id": "session-1"})

    def test_complete_recording_marks_session_ready_after_chunk_assembly(self):
        session = SimpleNamespace(
            id="session-1",
            is_active=True,
            recording_status="recording",
            audio_status="chunking",
            audio_error=None,
            finalized_at=None,
        )
        ready_session = SimpleNamespace(
            id="session-1",
            is_active=False,
            recording_status="ready",
            audio_status="finalized",
            audio_error=None,
            finalized_at=datetime.now(UTC),
        )
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            get_primary_meeting=AsyncMock(return_value=SimpleNamespace(id="meeting-1")),
            update_session_recording_state=AsyncMock(return_value=session),
            mark_session_recording_ready=AsyncMock(return_value=ready_session),
        )
        session_manager = SimpleNamespace(
            end_session_by_id=AsyncMock(return_value=ready_session)
        )
        final_audio = Path("/tmp/session-1.webm")

        with patch.object(
            self.sessions,
            "get_session_audio_path",
            side_effect=[None, final_audio],
        ), patch.object(
            self.sessions,
            "_recover_recording_audio",
            AsyncMock(return_value=(final_audio, {
                "client_id": "client-1",
                "available_count": 3,
                "highest_index": 2,
                "expected_count": 3,
                "missing_indices": [],
                "is_contiguous": True,
                "is_complete": True,
            })),
        ), patch.object(
            self.sessions,
            "get_chunk_upload_summary",
            return_value={
                "client_id": "client-1",
                "available_count": 3,
                "highest_index": 2,
                "expected_count": 3,
                "missing_indices": [],
                "is_contiguous": True,
                "is_complete": True,
            },
        ):
            payload = asyncio.run(
                self.sessions.complete_recording(
                    "session-1",
                    self.sessions.CompleteRecordingRequest(
                        client_id="client-1",
                        mime_type="audio/webm",
                        expected_chunks=3,
                    ),
                    repository=repository,
                    session_manager=session_manager,
                )
            )

        self.assertTrue(payload["workspace_ready"])
        self.assertEqual(payload["audio_status"], "finalized")
        self.assertEqual(payload["audio_url"], "/api/recordings/session-1/audio")
        self.assertTrue(payload["recoverable_from_chunks"])
        self.assertEqual(payload["chunk_summary"]["available_count"], 3)
        session_manager.end_session_by_id.assert_awaited_once_with("session-1")
        repository.mark_session_recording_ready.assert_awaited_once_with("session-1")

    def test_complete_recording_reports_missing_chunks_without_fake_success(self):
        session = SimpleNamespace(
            id="session-1",
            is_active=True,
            recording_status="recording",
            audio_status="chunking",
            audio_error=None,
            finalized_at=None,
        )
        finalizing_session = SimpleNamespace(
            id="session-1",
            is_active=True,
            recording_status="finalizing",
            audio_status="chunking",
            audio_error=None,
            finalized_at=None,
        )
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            get_primary_meeting=AsyncMock(return_value=SimpleNamespace(id="meeting-1")),
            update_session_recording_state=AsyncMock(return_value=finalizing_session),
            mark_session_recording_ready=AsyncMock(),
        )
        session_manager = SimpleNamespace(
            end_session_by_id=AsyncMock(),
        )

        with patch.object(
            self.sessions,
            "get_session_audio_path",
            return_value=None,
        ), patch.object(
            self.sessions,
            "_recover_recording_audio",
            AsyncMock(return_value=(None, {
                "client_id": "client-1",
                "available_count": 2,
                "highest_index": 3,
                "expected_count": 4,
                "missing_indices": [1, 2],
                "is_contiguous": False,
                "is_complete": False,
            })),
        ):
            payload = asyncio.run(
                self.sessions.complete_recording(
                    "session-1",
                    self.sessions.CompleteRecordingRequest(
                        client_id="client-1",
                        mime_type="audio/webm",
                        expected_chunks=4,
                    ),
                    repository=repository,
                    session_manager=session_manager,
                )
            )

        self.assertFalse(payload["workspace_ready"])
        self.assertEqual(payload["reason"], "missing_chunks")
        self.assertEqual(payload["missing_chunks"], [1, 2])
        self.assertTrue(payload["recoverable_from_chunks"])
        self.assertEqual(payload["chunk_summary"]["available_count"], 2)
        session_manager.end_session_by_id.assert_not_awaited()
        repository.mark_session_recording_ready.assert_not_awaited()

    def test_recover_recording_audio_reuses_completion_path(self):
        payload = {
            "session_id": "session-1",
            "workspace_ready": True,
            "has_audio": True,
        }

        with patch.object(
            self.sessions,
            "complete_recording",
            AsyncMock(return_value=payload.copy()),
        ) as complete_mock:
            result = asyncio.run(
                self.sessions.recover_recording_audio(
                    "session-1",
                    self.sessions.CompleteRecordingRequest(
                        client_id="client-1",
                        expected_chunks=3,
                    ),
                    repository=SimpleNamespace(),
                    session_manager=SimpleNamespace(),
                )
            )

        self.assertTrue(result["recovered"])
        complete_mock.assert_awaited_once()

    def test_assemble_chunks_keeps_chunk_storage_for_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(
                self.audio_storage,
                "get_settings",
                return_value=SimpleNamespace(data_dir=data_dir),
            ):
                self.audio_storage.write_chunk("session-1", "client-1", 0, b"abc")
                self.audio_storage.write_chunk("session-1", "client-1", 1, b"def")

                final_path = self.audio_storage.assemble_chunks(
                    "session-1",
                    "client-1",
                    2,
                    "webm",
                )

                self.assertIsNotNone(final_path)
                self.assertTrue(final_path.exists())
                chunk_dir = self.audio_storage.get_chunk_storage_dir("session-1", "client-1")
                self.assertTrue(chunk_dir.exists())
                self.assertEqual(sorted(p.name for p in chunk_dir.glob("*.chunk")), ["000000.chunk", "000001.chunk"])

    def test_recover_session_audio_from_chunks_rebuilds_audio_from_retained_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            with patch.object(
                self.audio_storage,
                "get_settings",
                return_value=SimpleNamespace(data_dir=data_dir),
            ):
                self.audio_storage.write_chunk("session-1", "client-1", 0, b"abc")
                self.audio_storage.write_chunk("session-1", "client-1", 1, b"def")
                self.audio_storage.write_session_chunk_meta("session-1", {
                    "client_id": "client-1",
                    "extension": "webm",
                    "expected_chunks": 2,
                })

                final_path = self.audio_storage.recover_session_audio_from_chunks(
                    "session-1",
                    client_id="client-1",
                    expected_count=2,
                    preferred_extension="webm",
                )

                self.assertIsNotNone(final_path)
                self.assertTrue(final_path.exists())
                self.assertEqual(final_path.read_bytes(), b"abcdef")
                chunk_dir = self.audio_storage.get_chunk_storage_dir("session-1", "client-1")
                self.assertTrue(chunk_dir.exists())

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

    def test_transcription_job_uses_existing_primary_meeting_for_initial_version(self):
        primary_meeting = SimpleNamespace(
            id="meeting-1",
            key_start=datetime.now(UTC),
            template_key="working_session",
            custom_prompt="Focus on implementation details.",
        )
        session = SimpleNamespace(
            id="session-1",
            meetings=[primary_meeting],
        )
        transcript_version = SimpleNamespace(
            id="tv-1",
            version_number=1,
            status="processing",
        )
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            create_meeting=AsyncMock(),
            ensure_transcript_versions=AsyncMock(return_value=[]),
            get_latest_transcript_version=AsyncMock(return_value=None),
            get_transcript_version_for_session=AsyncMock(return_value=None),
            create_transcript_version=AsyncMock(return_value=transcript_version),
            update_transcript_version=AsyncMock(return_value=transcript_version),
        )
        transcription_manager = SimpleNamespace(
            active_engine=SimpleNamespace(name="whisperx-test"),
            unload=AsyncMock(),
        )

        with patch.object(
            self.export,
            "_transcribe_and_persist_session",
            AsyncMock(return_value=("Transcript", 12.0)),
        ):
            asyncio.run(
                self.export._run_transcription_job(
                    job_id="job-1",
                    session_id="session-1",
                    repository=repository,
                    transcription_manager=transcription_manager,
                    mode="initial",
                )
            )

        repository.create_meeting.assert_not_awaited()
        repository.create_transcript_version.assert_awaited_once_with(
            session_id="session-1",
            meeting_id="meeting-1",
            version_number=1,
            status="processing",
            source_type="initial_transcription",
            template_key=self.export.normalize_template_key("working_session"),
            custom_prompt="Focus on implementation details.",
        )
        transcription_manager.unload.assert_awaited_once_with()

    def test_transcription_job_logs_unexpected_exceptions_with_traceback(self):
        primary_meeting = SimpleNamespace(
            id="meeting-1",
            key_start=datetime.now(UTC),
            template_key="meeting",
            custom_prompt=None,
        )
        session = SimpleNamespace(
            id="session-1",
            meetings=[primary_meeting],
        )
        transcript_version = SimpleNamespace(
            id="tv-1",
            version_number=1,
            status="processing",
        )
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            create_meeting=AsyncMock(),
            ensure_transcript_versions=AsyncMock(return_value=[]),
            get_latest_transcript_version=AsyncMock(return_value=None),
            get_transcript_version_for_session=AsyncMock(return_value=None),
            create_transcript_version=AsyncMock(return_value=transcript_version),
            update_transcript_version=AsyncMock(return_value=transcript_version),
        )

        transcription_manager = SimpleNamespace(unload=AsyncMock())

        with patch.object(
            self.export,
            "_transcribe_and_persist_session",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), patch.object(self.export.logger, "exception") as mock_exception:
            asyncio.run(
                self.export._run_transcription_job(
                    job_id="job-1",
                    session_id="session-1",
                    repository=repository,
                    transcription_manager=transcription_manager,
                    mode="initial",
                )
            )

        repository.update_transcript_version.assert_any_await("tv-1", status="failed")
        mock_exception.assert_called_once()
        transcription_manager.unload.assert_awaited_once_with()

    def test_revise_summary_draft_returns_updated_draft(self):
        draft = SimpleNamespace(
            id="draft-1",
            status="draft",
            content="Original summary",
            template_key="meeting",
            custom_prompt=None,
        )
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

        self.assertEqual(payload["draft_summary_id"], "draft-1")
        self.assertEqual(payload["content"], "Revised summary")
        self.assertEqual(payload["route"], "style_only")
        self.assertFalse(payload["used_transcript_context"])
        summarization_manager.refine_summary.assert_awaited_once_with(
            instruction="Tighten the takeaways.",
            current_summary="Original summary",
            template_key="meeting",
            custom_prompt=None,
            transcript="",
            transcript_windows=[],
            route="style_only",
        )
        update_args, update_kwargs = repository.update_summary.await_args
        self.assertEqual(update_args, ("draft-1",))
        self.assertEqual(update_kwargs["content"], "Revised summary")
        self.assertEqual(update_kwargs["source_type"], "ai_revised")
        self.assertIn("workflow_data_json", update_kwargs)

    def test_create_summary_draft_branches_from_selected_summary_and_preserves_existing_draft(self):
        transcript_version = SimpleNamespace(id="tv-1")
        meeting = SimpleNamespace(id="meeting-1")
        existing_draft = SimpleNamespace(id="draft-3")
        selected_summary = SimpleNamespace(
            id="sum-2",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
        )
        branched_draft = SimpleNamespace(id="draft-4")
        repository = SimpleNamespace(
            get_primary_meeting=AsyncMock(return_value=meeting),
            get_transcript_version_for_session=AsyncMock(return_value=transcript_version),
            get_latest_transcript_version=AsyncMock(return_value=transcript_version),
            get_draft_summary=AsyncMock(return_value=existing_draft),
            get_latest_summary=AsyncMock(),
            get_summary=AsyncMock(return_value=selected_summary),
            branch_draft_from_summary=AsyncMock(return_value=branched_draft),
        )

        payload = asyncio.run(
            self.export.create_summary_draft(
                "session-1",
                self.export.CreateDraftRequest(
                    source_summary_id="sum-2",
                    source_type="ai_revised",
                    transcript_version_id="tv-1",
                    preserve_existing_draft=True,
                ),
                repository=repository,
            )
        )

        self.assertEqual(payload["draft_summary_id"], "draft-4")
        repository.get_latest_summary.assert_not_called()
        repository.branch_draft_from_summary.assert_awaited_once_with(
            "sum-2",
            source_type="ai_revised",
            preserve_existing_draft=True,
        )

    def test_repository_branch_draft_reuses_existing_same_source_branch(self):
        source_summary = SimpleNamespace(
            id="sum-2",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
        )
        existing_draft = SimpleNamespace(
            id="draft-3",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            parent_summary_id="sum-2",
        )
        fake_repository = SimpleNamespace(
            get_summary=AsyncMock(return_value=source_summary),
            get_draft_summary=AsyncMock(return_value=existing_draft),
            save_draft_summary=AsyncMock(),
            delete_draft_summaries=AsyncMock(),
            create_draft_from_summary=AsyncMock(),
        )

        result = asyncio.run(
            importlib.import_module("src.sessions.repository").Repository.branch_draft_from_summary(
                fake_repository,
                "sum-2",
                source_type="ai_revised",
                preserve_existing_draft=True,
            )
        )

        self.assertEqual(result.id, "draft-3")
        fake_repository.save_draft_summary.assert_not_called()
        fake_repository.delete_draft_summaries.assert_not_called()
        fake_repository.create_draft_from_summary.assert_not_called()

    def test_build_summary_save_params_counts_saved_copies_in_export_version_number(self):
        now = datetime.now(UTC)
        repository = SimpleNamespace(
            get_segments=AsyncMock(return_value=[]),
            get_summaries=AsyncMock(
                return_value=[
                    SimpleNamespace(id="sum-3"),
                    SimpleNamespace(id="sum-2"),
                    SimpleNamespace(id="sum-1"),
                ]
            ),
        )
        session = SimpleNamespace(
            id="session-1",
            started_at=now,
            timezone_name="America/Chicago",
            timezone_offset_minutes=-300,
        )
        meeting = SimpleNamespace(
            id="meeting-1",
            title="Goals Touchbase",
        )

        params = asyncio.run(
            self.export._build_summary_save_params(
                repository,
                session,
                meeting,
                transcript_version_id="tv-1",
                summary_content="Summary body",
                template_label="General Meeting",
                processing_duration_seconds=12.0,
            )
        )

        self.assertIn(" (v4).md", params["relative_path"])

    def test_revise_summary_draft_returns_conflict_when_draft_disappears(self):
        draft = SimpleNamespace(
            id="draft-1",
            status="draft",
            content="Original summary",
            template_key="meeting",
            custom_prompt=None,
        )
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

    def test_workspace_obsidian_link_ignores_newer_saved_copy(self):
        now = datetime.now(UTC)
        session = SimpleNamespace(
            id="session-1",
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            timezone_name="America/Chicago",
            timezone_offset_minutes=-300,
            has_transcription=False,
            recording_status="ready",
            audio_status="finalized",
            audio_error=None,
            finalized_at=now - timedelta(hours=1),
            is_active=False,
        )
        meeting = SimpleNamespace(
            id="meeting-1",
            title="Goals Touchbase",
            template_key="meeting",
            custom_prompt=None,
        )
        transcript_version = SimpleNamespace(
            id="tv-1",
            version_number=1,
            status="completed",
            source_type="initial",
            created_at=now - timedelta(hours=2),
            speaker_review_required=False,
            speaker_review_completed_at=None,
            template_key="meeting",
            custom_prompt=None,
        )
        saved_copy = SimpleNamespace(
            id="sum-2",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            content="Internal snapshot",
            backend="ollama",
            model="qwen3:8b",
            created_at=now - timedelta(minutes=10),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="manual_edit",
            saved_to_obsidian_at=None,
            obsidian_relative_path=None,
            workflow_data_json=None,
        )
        exported = SimpleNamespace(
            id="sum-1",
            meeting_id="meeting-1",
            transcript_version_id="tv-1",
            content="Exported summary",
            backend="ollama",
            model="qwen3:8b",
            created_at=now - timedelta(hours=1),
            processing_duration_seconds=12.0,
            template="General Meeting",
            template_key="meeting",
            custom_prompt=None,
            status="saved",
            source_type="generated",
            saved_to_obsidian_at=now - timedelta(hours=1),
            obsidian_relative_path="Sidekick/Goals Touchbase.md",
            workflow_data_json=None,
        )
        repository = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
            get_primary_meeting=AsyncMock(return_value=meeting),
            ensure_transcript_versions=AsyncMock(return_value=[transcript_version]),
            get_latest_transcript_version=AsyncMock(side_effect=[transcript_version, transcript_version]),
            get_segments=AsyncMock(return_value=[]),
            get_summaries=AsyncMock(return_value=[saved_copy, exported]),
            get_draft_summary=AsyncMock(return_value=None),
            get_app_settings=AsyncMock(return_value=SimpleNamespace(workspace_chat_enabled=False)),
        )

        with patch.object(
            self.sessions,
            "get_settings",
            return_value=SimpleNamespace(
                obsidian_vault_path="/vault/SidekickVault",
                enable_debug_retranscribe=False,
            ),
        ), patch.object(
            self.sessions,
            "get_session_audio_path",
            return_value=None,
        ), patch.object(
            self.sessions,
            "ensure_session_audio_path",
            return_value=None,
        ):
            payload = asyncio.run(
                self.sessions.get_recording_workspace(
                    "session-1",
                    repository=repository,
                )
            )

        self.assertEqual(payload["saved_summaries"][0]["save_kind"], "saved_copy")
        self.assertEqual(
            payload["obsidian"]["latest_relative_path"],
            "Sidekick/Goals Touchbase.md",
        )
        self.assertIn("SidekickVault", payload["obsidian"]["open_uri"])
        self.assertIn("Goals%20Touchbase.md", payload["obsidian"]["open_uri"])

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
