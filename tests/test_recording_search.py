"""Integration and service tests for unified recording search."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import text

from src.search.service import RecordingSearchService
from src.sessions.repository import Repository


class RepositorySearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.repo = Repository(f"sqlite+aiosqlite:///{self.db_path}")
        await self.repo.init_db()

    async def asyncTearDown(self):
        await self.repo.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_search_transcript_segments_indexes_completed_recordings(self):
        session = await self.repo.create_session()
        meeting = await self.repo.create_meeting(session.id, title="Inventory Planner Rollout")
        await self.repo.add_segment(
            session_id=session.id,
            meeting_id=meeting.id,
            text="Greg said the inventory planner rollout should stay phased.",
            start_time=10.0,
            end_time=18.0,
            speaker="Greg",
            speaker_cluster="SPEAKER_01",
        )
        await self.repo.set_session_has_transcription(session.id, True)
        await self.repo.end_session(session.id)
        await self.repo.reindex_session_transcript_search(session.id)

        results = await self.repo.search_transcript_segments(
            query='"inventory planner rollout" OR inventory OR planner OR rollout',
            speaker="greg",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["speaker"], "Greg")
        self.assertEqual(results[0]["meeting_title"], "Inventory Planner Rollout")

    async def test_search_transcript_segments_respects_date_filters(self):
        older = await self.repo.create_session()
        recent = await self.repo.create_session()
        older_meeting = await self.repo.create_meeting(older.id, title="Old Review")
        recent_meeting = await self.repo.create_meeting(recent.id, title="Recent Review")

        await self.repo.add_segment(
            session_id=older.id,
            meeting_id=older_meeting.id,
            text="Inventory planner rollout started last month.",
            start_time=0.0,
            end_time=8.0,
        )
        await self.repo.add_segment(
            session_id=recent.id,
            meeting_id=recent_meeting.id,
            text="Inventory planner rollout starts next week.",
            start_time=0.0,
            end_time=8.0,
        )
        await self.repo.set_session_has_transcription(older.id, True)
        await self.repo.set_session_has_transcription(recent.id, True)
        await self.repo.end_session(older.id)
        await self.repo.end_session(recent.id)

        async with self.repo._session_factory() as db:
            older_started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)
            recent_started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
            await db.execute(
                text("UPDATE sessions SET started_at = :started_at WHERE id = :session_id"),
                {"started_at": older_started_at, "session_id": older.id},
            )
            await db.execute(
                text("UPDATE sessions SET started_at = :started_at WHERE id = :session_id"),
                {"started_at": recent_started_at, "session_id": recent.id},
            )
            await db.commit()

        await self.repo.reindex_session_transcript_search(older.id)
        await self.repo.reindex_session_transcript_search(recent.id)

        results = await self.repo.search_transcript_segments(
            query='"inventory planner rollout" OR inventory OR planner OR rollout',
            date_from=datetime.now(UTC).date() - timedelta(days=2),
            date_to=datetime.now(UTC).date(),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["meeting_title"], "Recent Review")

    async def test_update_meeting_title_refreshes_search_index(self):
        session = await self.repo.create_session()
        meeting = await self.repo.create_meeting(session.id, title="Old Planner Title")
        await self.repo.add_segment(
            session_id=session.id,
            meeting_id=meeting.id,
            text="We discussed launch timing.",
            start_time=0.0,
            end_time=5.0,
        )
        await self.repo.set_session_has_transcription(session.id, True)
        await self.repo.end_session(session.id)
        await self.repo.reindex_session_transcript_search(session.id)

        initial = await self.repo.search_transcript_segments(query='planner')
        self.assertEqual(len(initial), 1)

        await self.repo.update_meeting_title(meeting.id, "Launch Notes")

        refreshed = await self.repo.search_transcript_segments(query='planner')
        self.assertEqual(refreshed, [])

    async def test_search_transcript_segments_only_indexes_latest_ready_transcript_version(self):
        session = await self.repo.create_session()
        meeting = await self.repo.create_meeting(session.id, title="Versioned Search")
        version_one = await self.repo.create_transcript_version(
            session_id=session.id,
            meeting_id=meeting.id,
            version_number=1,
            status="ready",
            source_type="initial_transcription",
        )
        version_two = await self.repo.create_transcript_version(
            session_id=session.id,
            meeting_id=meeting.id,
            version_number=2,
            status="ready",
            source_type="retranscription",
            parent_version_id=version_one.id,
        )
        await self.repo.add_segment(
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=version_one.id,
            text="Legacy planner wording that should disappear from search.",
            start_time=0.0,
            end_time=4.0,
        )
        await self.repo.add_segment(
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=version_two.id,
            text="Latest planner wording that should stay searchable.",
            start_time=0.0,
            end_time=4.0,
        )
        await self.repo.set_session_has_transcription(session.id, True)
        await self.repo.end_session(session.id)
        await self.repo.reindex_session_transcript_search(session.id)

        old_results = await self.repo.search_transcript_segments(query="legacy")
        new_results = await self.repo.search_transcript_segments(query="latest")

        self.assertEqual(old_results, [])
        self.assertEqual(len(new_results), 1)
        self.assertEqual(new_results[0]["transcript_version_id"], version_two.id)

    async def test_workspace_chat_thread_persists_messages_and_aggregates_feedback(self):
        session = await self.repo.create_session()
        meeting = await self.repo.create_meeting(session.id, title="Chat Feedback")
        thread = await self.repo.get_or_create_workspace_chat_thread(session.id, meeting.id)
        self.assertIsNotNone(thread.id)

        await self.repo.add_workspace_chat_message(
            thread_id=thread.id,
            session_id=session.id,
            meeting_id=meeting.id,
            role="assistant",
            message_type="assistant_answer",
            content="Add the blocker callout to the summary.",
            intent_label="surface_risk",
            intent_confidence=0.92,
            suggests_summary_change=True,
            suggested_change_kind="add",
            apply_ready=True,
            applied_at=datetime.now(UTC).replace(tzinfo=None),
            template_key="meeting",
        )

        messages = await self.repo.list_workspace_chat_messages(thread.id)
        aggregates = await self.repo.aggregate_workspace_chat_feedback(applied_only=True)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].intent_label, "surface_risk")
        self.assertEqual(aggregates["total_messages"], 1)
        self.assertEqual(aggregates["by_intent"]["surface_risk"], 1)

    async def test_app_settings_bootstrap_and_update(self):
        settings = await self.repo.get_app_settings()
        self.assertIsNotNone(settings)
        self.assertFalse(settings.workspace_chat_enabled)

        updated = await self.repo.update_app_settings(workspace_chat_enabled=True)
        reloaded = await self.repo.get_app_settings()

        self.assertTrue(updated.workspace_chat_enabled)
        self.assertTrue(reloaded.workspace_chat_enabled)


class RecordingSearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_returns_cited_answer_and_window_segment_ids(self):
        class FakeRepository:
            async def search_transcript_segments(self, **kwargs):
                return [
                    {
                        "segment_id": "seg-2",
                        "session_id": "session-1",
                        "meeting_id": "meeting-1",
                        "transcript_version_id": "tv-2",
                        "text": "Greg said keep the rollout phased until exceptions stabilize.",
                        "start_time": 62.0,
                        "end_time": 70.0,
                        "is_important": 0,
                        "speaker": "Greg",
                        "speaker_cluster": "SPEAKER_01",
                        "meeting_title": "Inventory Review",
                        "session_started_at": datetime(2026, 3, 5, 20, 14),
                        "timezone_name": "America/Chicago",
                        "timezone_offset_minutes": 360,
                        "rank": 0.1,
                    }
                ]

            async def get_segments(self, session_id=None, meeting_id=None, transcript_version_id=None, important_only=False):
                if transcript_version_id != "tv-2":
                    return []
                return [
                    SimpleNamespace(
                        id="seg-1",
                        start_time=55.0,
                        end_time=61.0,
                        text="Pam asked about the rollout window.",
                        speaker="Pam",
                        speaker_cluster="SPEAKER_00",
                    ),
                    SimpleNamespace(
                        id="seg-2",
                        start_time=62.0,
                        end_time=70.0,
                        text="Greg said keep the rollout phased until exceptions stabilize.",
                        speaker="Greg",
                        speaker_cluster="SPEAKER_01",
                    ),
                    SimpleNamespace(
                        id="seg-3",
                        start_time=71.0,
                        end_time=80.0,
                        text="He also said stores with custom rules should wait.",
                        speaker="Greg",
                        speaker_cluster="SPEAKER_01",
                    ),
                ]

        class FakeSummarizationManager:
            async def answer_question_with_citations(self, **kwargs):
                return {
                    "answer": "Greg said the rollout should stay phased until exceptions stabilize.",
                    "citations": [0],
                    "confidence": "high",
                    "answer_type": "direct_answer",
                    "reasoning_note": "One recording clearly answers the question.",
                    "follow_up_queries": [
                        "What decision was made about the rollout?",
                        "Show action items related to the rollout",
                    ],
                }

        service = RecordingSearchService(FakeRepository(), FakeSummarizationManager())

        result = await service.search(query="What did Greg say about inventory planner rollout?")

        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["answer_type"], "direct_answer")
        self.assertEqual(result["reasoning_note"], "One recording clearly answers the question.")
        self.assertEqual(len(result["follow_up_queries"]), 2)
        self.assertEqual(result["results"][0]["transcript_segment_ids"], ["seg-1", "seg-2", "seg-3"])
        self.assertEqual(result["results"][0]["transcript_version_id"], "tv-2")
        self.assertTrue(result["results"][0]["is_cited"])
        self.assertEqual(len(result["groups"]), 1)
        self.assertTrue(result["groups"][0]["has_cited_evidence"])
        self.assertEqual(result["groups"][0]["snippets"][0]["transcript_segment_ids"], ["seg-1", "seg-2", "seg-3"])
        self.assertIn("Greg said", result["answer"])

    async def test_service_groups_results_by_recording_and_cited_snippet_first(self):
        class FakeRepository:
            async def search_transcript_segments(self, **kwargs):
                return [
                    {
                        "segment_id": "seg-2",
                        "session_id": "session-1",
                        "meeting_id": "meeting-1",
                        "transcript_version_id": "tv-1",
                        "text": "Decision: keep pricing flat for Q2.",
                        "start_time": 20.0,
                        "end_time": 28.0,
                        "is_important": 1,
                        "speaker": "Ava",
                        "speaker_cluster": "SPEAKER_01",
                        "meeting_title": "Pricing Review",
                        "session_started_at": datetime(2026, 3, 7, 18, 0),
                        "timezone_name": "America/Chicago",
                        "timezone_offset_minutes": 360,
                        "rank": 0.05,
                    },
                    {
                        "segment_id": "seg-3",
                        "session_id": "session-1",
                        "meeting_id": "meeting-1",
                        "transcript_version_id": "tv-1",
                        "text": "Action item: write pricing FAQ.",
                        "start_time": 35.0,
                        "end_time": 42.0,
                        "is_important": 0,
                        "speaker": "Ben",
                        "speaker_cluster": "SPEAKER_02",
                        "meeting_title": "Pricing Review",
                        "session_started_at": datetime(2026, 3, 7, 18, 0),
                        "timezone_name": "America/Chicago",
                        "timezone_offset_minutes": 360,
                        "rank": 0.2,
                    },
                    {
                        "segment_id": "seg-9",
                        "session_id": "session-2",
                        "meeting_id": "meeting-2",
                        "transcript_version_id": "tv-2",
                        "text": "We revisited pricing, but no new decision was made.",
                        "start_time": 14.0,
                        "end_time": 22.0,
                        "is_important": 0,
                        "speaker": "Cara",
                        "speaker_cluster": "SPEAKER_03",
                        "meeting_title": "Follow-up Sync",
                        "session_started_at": datetime(2026, 3, 8, 18, 0),
                        "timezone_name": "America/Chicago",
                        "timezone_offset_minutes": 360,
                        "rank": 0.1,
                    },
                ]

            async def get_segments(self, session_id=None, meeting_id=None, transcript_version_id=None, important_only=False):
                if session_id == "session-1":
                    return [
                        SimpleNamespace(id="seg-1", start_time=12.0, end_time=19.0, text="We reviewed Q2 pricing options.", speaker="Ava", speaker_cluster="SPEAKER_01"),
                        SimpleNamespace(id="seg-2", start_time=20.0, end_time=28.0, text="Decision: keep pricing flat for Q2.", speaker="Ava", speaker_cluster="SPEAKER_01"),
                        SimpleNamespace(id="seg-3", start_time=35.0, end_time=42.0, text="Action item: write pricing FAQ.", speaker="Ben", speaker_cluster="SPEAKER_02"),
                    ]
                return [
                    SimpleNamespace(id="seg-8", start_time=8.0, end_time=13.0, text="We revisited pricing assumptions.", speaker="Cara", speaker_cluster="SPEAKER_03"),
                    SimpleNamespace(id="seg-9", start_time=14.0, end_time=22.0, text="We revisited pricing, but no new decision was made.", speaker="Cara", speaker_cluster="SPEAKER_03"),
                ]

        class FakeSummarizationManager:
            async def answer_question_with_citations(self, **kwargs):
                return {
                    "answer": "The clearest pricing decision was to keep pricing flat for Q2.",
                    "citations": [1],
                    "confidence": "high",
                    "answer_type": "multi_recording",
                    "reasoning_note": "Two meetings mention pricing, but only one contains a clear decision.",
                    "follow_up_queries": [],
                }

        service = RecordingSearchService(FakeRepository(), FakeSummarizationManager())

        result = await service.search(query="What was the pricing decision?")

        self.assertEqual(len(result["groups"]), 2)
        first_group = result["groups"][0]
        self.assertEqual(first_group["session_id"], "session-1")
        self.assertTrue(first_group["has_cited_evidence"])
        self.assertTrue(first_group["snippets"][0]["is_cited"])
        self.assertIn("pricing", first_group["match_reason"].lower())
        self.assertEqual(len(result["follow_up_queries"]), 3)

    async def test_service_returns_grouped_evidence_when_llm_answer_fails(self):
        class FakeRepository:
            async def search_transcript_segments(self, **kwargs):
                return [
                    {
                        "segment_id": "seg-1",
                        "session_id": "session-1",
                        "meeting_id": "meeting-1",
                        "transcript_version_id": "tv-1",
                        "text": "Sam flagged a latency spike during rollout.",
                        "start_time": 10.0,
                        "end_time": 18.0,
                        "is_important": 1,
                        "speaker": "Sam",
                        "speaker_cluster": "SPEAKER_01",
                        "meeting_title": "Latency Review",
                        "session_started_at": datetime(2026, 3, 9, 18, 0),
                        "timezone_name": "America/Chicago",
                        "timezone_offset_minutes": 360,
                        "rank": 0.05,
                    }
                ]

            async def get_segments(self, session_id=None, meeting_id=None, transcript_version_id=None, important_only=False):
                return [
                    SimpleNamespace(id="seg-1", start_time=10.0, end_time=18.0, text="Sam flagged a latency spike during rollout.", speaker="Sam", speaker_cluster="SPEAKER_01"),
                ]

        class FakeSummarizationManager:
            async def answer_question_with_citations(self, **kwargs):
                raise RuntimeError("model unavailable")

        service = RecordingSearchService(FakeRepository(), FakeSummarizationManager())

        result = await service.search(query="Did Sam mention latency?")

        self.assertIsNone(result["answer"])
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["answer_type"], "partial")
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["recording_title"], "Latency Review")


if __name__ == "__main__":
    unittest.main()
