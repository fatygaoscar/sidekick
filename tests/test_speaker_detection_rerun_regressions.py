"""Regression tests for diarization-only speaker detection reruns."""

import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _run_without_executor_shutdown(awaitable):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


class _SpeakerDetectionRepositoryDouble:
    def __init__(self, source_version_id: str, source_segments: list[SimpleNamespace]) -> None:
        self._source_version = SimpleNamespace(id=source_version_id, meeting_id="meeting-1")
        self._source_segments = list(source_segments)
        self._persisted: dict[str, list[SimpleNamespace]] = {}
        self.version_updates: list[dict] = []
        self.has_transcription_updates: list[tuple[str, bool]] = []
        self.reindexed_sessions: list[str] = []

    async def get_session(self, session_id: str):
        return SimpleNamespace(id=session_id, ended_at=True)

    async def get_transcript_version_for_session(self, session_id: str, transcript_version_id: str):
        if transcript_version_id == self._source_version.id:
            return self._source_version
        return None

    async def get_segments(self, *, session_id: str | None = None, transcript_version_id: str | None = None):
        if transcript_version_id == self._source_version.id:
            return list(self._source_segments)
        return list(self._persisted.get(str(transcript_version_id), []))

    async def delete_segments_for_transcript_version(self, transcript_version_id: str) -> None:
        self._persisted[str(transcript_version_id)] = []

    async def bulk_add_segments(
        self,
        *,
        session_id: str,
        meeting_id: str,
        transcript_version_id: str,
        segments: list[dict],
    ) -> None:
        self._persisted[str(transcript_version_id)] = [
            SimpleNamespace(**segment) for segment in segments
        ]

    async def set_session_has_transcription(self, session_id: str, value: bool) -> None:
        self.has_transcription_updates.append((session_id, value))

    async def reindex_session_transcript_search(self, session_id: str) -> None:
        self.reindexed_sessions.append(session_id)

    async def update_transcript_version(self, transcript_version_id: str, **values):
        self.version_updates.append(
            {"transcript_version_id": transcript_version_id, **values}
        )
        return SimpleNamespace(
            id=transcript_version_id,
            version_number=2,
            **values,
        )


class SpeakerDetectionRerunRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sessions = importlib.import_module("src.api.routes.sessions")

    def test_run_speaker_detection_job_backfills_short_unassigned_gap(self):
        source_segments = [
            SimpleNamespace(text="Hello", start_time=0.0, end_time=1.0, words=None),
            SimpleNamespace(text="Uh huh", start_time=1.1, end_time=1.2, words=None),
            SimpleNamespace(text="Continue", start_time=1.3, end_time=2.0, words=None),
            SimpleNamespace(text="Wrap up", start_time=3.0, end_time=4.0, words=None),
        ]
        repository = _SpeakerDetectionRepositoryDouble("source-tv", source_segments)
        original_jobs = dict(self.sessions._SPEAKER_DETECTION_JOBS)

        try:
            self.sessions._SPEAKER_DETECTION_JOBS.clear()
            job = self.sessions._create_speaker_detection_job(
                "session-1",
                transcript_version_id="target-tv",
                transcript_version_number=2,
            )

            with patch.object(self.sessions, "get_session_audio_path", return_value="/tmp/session-1.wav"), patch.object(
                self.sessions,
                "ensure_session_audio_path",
                return_value="/tmp/session-1.wav",
            ), patch.object(
                self.sessions,
                "diarize",
                return_value=[
                    (0.0, 1.0, "SPEAKER_00"),
                    (1.3, 2.0, "SPEAKER_00"),
                    (3.0, 4.0, "SPEAKER_01"),
                ],
            ), patch.object(
                self.sessions,
                "_list_speaker_profiles_safe",
                return_value=[],
            ):
                _run_without_executor_shutdown(
                    self.sessions._run_speaker_detection_job(
                        job_id=job["job_id"],
                        session_id="session-1",
                        source_transcript_version_id="source-tv",
                        target_transcript_version_id="target-tv",
                        expected_speaker_count=2,
                        repository=repository,
                    )
                )

            persisted = repository.get_segments(session_id="session-1", transcript_version_id="target-tv")
            persisted_segments = _run_without_executor_shutdown(persisted)

            self.assertEqual(
                [segment.speaker_cluster for segment in persisted_segments],
                ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00", "SPEAKER_01"],
            )
            self.assertIn({"transcript_version_id": "target-tv", "status": "ready", "speaker_review_required": True, "speaker_review_completed_at": None, "diarization_actual_speaker_count": 2, "diarization_unassigned_segment_count": 0, "diarization_unassigned_segment_ratio": 0.0, "repair_quality_gate_passed": True}, repository.version_updates)
            self.assertEqual(repository.has_transcription_updates, [("session-1", True)])
            self.assertEqual(repository.reindexed_sessions, ["session-1"])
            self.assertEqual(self.sessions._SPEAKER_DETECTION_JOBS[job["job_id"]]["status"], "completed")
        finally:
            self.sessions._SPEAKER_DETECTION_JOBS.clear()
            self.sessions._SPEAKER_DETECTION_JOBS.update(original_jobs)


if __name__ == "__main__":
    unittest.main()
