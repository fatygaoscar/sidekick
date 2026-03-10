"""Regression tests for the WhisperX local transcription cutover."""

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import types

import numpy as np

from src.transcription.base import AlignedTranscriptSegment, FileTranscriptionResult


class _RepositoryDouble:
    def __init__(self) -> None:
        self.added_segments: list[SimpleNamespace] = []
        self.has_transcription_updates: list[tuple[str, bool]] = []
        self.reindexed_sessions: list[str] = []
        self.meeting_updates: list[dict] = []
        self.transcript_version_updates: list[dict] = []

    async def delete_segments_for_session(self, session_id: str) -> None:
        self.added_segments.clear()

    async def add_segment(
        self,
        *,
        session_id: str,
        meeting_id: str,
        transcript_version_id: str | None = None,
        text: str,
        start_time: float,
        end_time: float,
        confidence: float | None = None,
        speaker: str | None = None,
        speaker_cluster: str | None = None,
        is_important: bool = False,
    ):
        segment = SimpleNamespace(
            id=f"seg-{len(self.added_segments) + 1}",
            session_id=session_id,
            meeting_id=meeting_id,
            transcript_version_id=transcript_version_id,
            text=text,
            start_time=start_time,
            end_time=end_time,
            confidence=confidence,
            speaker=speaker,
            speaker_cluster=speaker_cluster,
            is_important=is_important,
        )
        self.added_segments.append(segment)
        return segment

    async def set_session_has_transcription(self, session_id: str, value: bool) -> None:
        self.has_transcription_updates.append((session_id, value))

    async def reindex_session_transcript_search(self, session_id: str) -> None:
        self.reindexed_sessions.append(session_id)

    async def get_segments(
        self,
        session_id: str | None = None,
        transcript_version_id: str | None = None,
    ):
        return list(self.added_segments)

    async def update_meeting_settings(self, meeting_id: str, **values):
        self.meeting_updates.append({"meeting_id": meeting_id, **values})

    async def delete_segments_for_transcript_version(self, transcript_version_id: str) -> None:
        self.added_segments.clear()

    async def update_transcript_version(self, transcript_version_id: str, **values):
        self.transcript_version_updates.append(
            {"transcript_version_id": transcript_version_id, **values}
        )
        return SimpleNamespace(id=transcript_version_id, **values)


class _TranscriptionManagerDouble:
    async def transcribe_file(self, _file_path, progress_callback=None):
        if progress_callback:
            progress_callback(0.15, "Transcribing audio")
            progress_callback(0.45, "Aligning words")
            progress_callback(0.70, "Assigning speakers")
        return FileTranscriptionResult(
            text="Hello world Goodbye now",
            duration_seconds=3.5,
            language="en",
            confidence=None,
            segments=[
                AlignedTranscriptSegment(
                    start=0.0,
                    end=1.5,
                    text="Hello world",
                    speaker="SPEAKER_00",
                    speaker_cluster="SPEAKER_00",
                ),
                AlignedTranscriptSegment(
                    start=1.6,
                    end=3.4,
                    text="Goodbye now",
                    speaker="SPEAKER_01",
                    speaker_cluster="SPEAKER_01",
                ),
            ],
        )


class WhisperXRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.export = importlib.import_module("src.api.routes.export")
        cls.manager_module = importlib.import_module("src.transcription.manager")
        cls.websocket = importlib.import_module("src.api.routes.websocket")
        cls.settings_module = importlib.import_module("config.settings")
        cls.whisperx_local = importlib.import_module("src.transcription.whisperx_local")

    def test_manager_maps_local_backend_to_whisperx_engine(self):
        settings = self.settings_module.Settings()
        manager = self.manager_module.TranscriptionManager(settings)

        engine = manager._create_engine(self.settings_module.TranscriptionBackend.LOCAL)

        self.assertIsInstance(engine, self.whisperx_local.WhisperXLocalEngine)

    def test_local_websocket_preview_is_disabled(self):
        handler = self.websocket.AudioWebSocketHandler(
            websocket=SimpleNamespace(client="test-client"),
            session_manager=SimpleNamespace(),
            transcription_manager=SimpleNamespace(),
        )

        self.assertFalse(handler._live_preview_enabled())

    def test_websocket_attach_session_acknowledges_current_session(self):
        session_manager = SimpleNamespace(
            current_session=SimpleNamespace(
                id="session-1",
                started_at=datetime.now(UTC),
            )
        )
        handler = self.websocket.AudioWebSocketHandler(
            websocket=SimpleNamespace(client="test-client"),
            session_manager=session_manager,
            transcription_manager=SimpleNamespace(),
        )
        handler._send_json = AsyncMock()

        asyncio.run(handler._handle_command(json.dumps({
            "command": "attach_session",
            "session_id": "session-1",
        })))

        self.assertEqual(handler._attached_session_id, "session-1")
        payload = handler._send_json.await_args_list[-1].args[0]
        self.assertEqual(payload["type"], "session_attached")
        self.assertEqual(payload["session_id"], "session-1")

    def test_websocket_ignores_audio_until_session_is_attached(self):
        session_manager = SimpleNamespace(
            current_session=SimpleNamespace(id="session-1")
        )
        handler = self.websocket.AudioWebSocketHandler(
            websocket=SimpleNamespace(client="test-client"),
            session_manager=session_manager,
            transcription_manager=SimpleNamespace(),
        )
        handler._buffer.add_chunk = AsyncMock()

        asyncio.run(handler._handle_audio(b"\x00\x00" * 8))

        handler._buffer.add_chunk.assert_not_awaited()

    def test_transcribe_and_persist_session_uses_engine_segments_directly(self):
        repository = _RepositoryDouble()
        manager = _TranscriptionManagerDouble()
        session = SimpleNamespace(id="session-1", ended_at=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = Path(tmpdir) / "session-1.wav"
            audio_path.write_bytes(b"RIFF")
            with patch.object(self.export, "get_session_audio_path", return_value=audio_path), patch.object(
                self.export,
                "ensure_session_audio_path",
                return_value=audio_path,
            ):
                transcript, duration = asyncio.run(
                    self.export._transcribe_and_persist_session(
                        session_id="session-1",
                        session=session,
                        repository=repository,
                        transcription_manager=manager,
                        primary_meeting_id="meeting-1",
                        transcript_version_id="tv-1",
                    )
                )

        self.assertEqual(duration, 3.5)
        self.assertEqual(transcript, "[00:00] Attendee A: Hello world\n[00:01] Attendee B: Goodbye now")
        self.assertEqual(len(repository.added_segments), 2)
        self.assertEqual(repository.added_segments[0].speaker_cluster, "SPEAKER_00")
        self.assertEqual(repository.added_segments[1].speaker_cluster, "SPEAKER_01")
        self.assertEqual(repository.has_transcription_updates, [("session-1", True)])
        self.assertEqual(repository.reindexed_sessions, ["session-1"])
        self.assertTrue(repository.transcript_version_updates)

    def test_whisperx_local_streaming_preview_is_not_supported(self):
        engine = self.whisperx_local.WhisperXLocalEngine()

        with self.assertRaises(RuntimeError):
            asyncio.run(engine.transcribe(audio=[], sample_rate=16000))

    def test_whisperx_local_file_transcription_uses_pyav_decode(self):
        engine = self.whisperx_local.WhisperXLocalEngine()
        engine._initialized = True

        diarization_inputs = []

        def fake_transcribe(audio, batch_size, language=None):
            self.assertIsInstance(audio, np.ndarray)
            return {
                "language": "en",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "Hello world"},
                ],
            }

        def fake_align(_segments, _align_model, _metadata, audio, _device, return_char_alignments=False):
            self.assertFalse(return_char_alignments)
            self.assertIsInstance(audio, np.ndarray)
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Hello world",
                        "words": [{"start": 0.0, "end": 1.0, "word": "Hello"}],
                    },
                ],
            }

        def fake_assign_word_speakers(_diarize_segments, aligned_result):
            return {
                "segments": [
                    {
                        **aligned_result["segments"][0],
                        "speaker": "SPEAKER_00",
                    },
                ],
            }

        def fake_diarization_pipeline(audio):
            diarization_inputs.append(audio)
            self.assertIsInstance(audio, np.ndarray)
            return object()

        fake_whisperx = types.ModuleType("whisperx")
        fake_whisperx.align = fake_align
        fake_whisperx.assign_word_speakers = fake_assign_word_speakers

        def fail_load_audio(_audio_file):
            raise AssertionError("whisperx.load_audio should not be called")

        fake_whisperx.load_audio = fail_load_audio

        engine._model = SimpleNamespace(transcribe=fake_transcribe)

        with patch.object(
            self.whisperx_local,
            "decode_audio_to_mono_float32",
            return_value=np.ones(16000, dtype=np.float32),
        ) as decode_mock, patch.object(
            self.whisperx_local,
            "duration_seconds_from_audio",
            return_value=1.0,
        ), patch.object(
            engine,
            "_get_align_model",
            return_value=("align-model", {"language": "en", "type": "torchaudio", "dictionary": {}}),
        ), patch.object(
            engine,
            "_get_diarization_pipeline",
            return_value=fake_diarization_pipeline,
        ), patch.dict(sys.modules, {"whisperx": fake_whisperx}):
            result = engine._transcribe_file_sync("sample.wav", None)

        decode_mock.assert_called_once_with("sample.wav", sample_rate=16000)
        self.assertEqual(result.duration_seconds, 1.0)
        self.assertEqual(result.text, "Hello world")
        self.assertEqual(result.segments[0].speaker_cluster, "SPEAKER_00")
        self.assertEqual(len(diarization_inputs), 1)
