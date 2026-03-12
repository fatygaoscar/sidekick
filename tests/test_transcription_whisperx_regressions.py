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
from unittest.mock import AsyncMock, MagicMock, patch
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
    async def transcribe_file(
        self,
        _file_path,
        progress_callback=None,
        expected_speaker_count=None,
        late_join_offset_seconds=None,
        repair_reason=None,
    ):
        if progress_callback:
            progress_callback(0.15, "Transcribing audio")
            progress_callback(0.45, "Aligning words")
            progress_callback(0.70, "Assigning speakers")
        self.expected_speaker_count = expected_speaker_count
        self.late_join_offset_seconds = late_join_offset_seconds
        self.repair_reason = repair_reason
        return FileTranscriptionResult(
            text="Hello world Goodbye now",
            duration_seconds=3.5,
            language="en",
            confidence=None,
            diarization_backend="pyannote",
            diarization_model="pyannote/speaker-diarization-community-1",
            repair_strategy="full_file_exact_count" if expected_speaker_count is not None else "full_file_unconstrained",
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
        cls.diarize_module = importlib.import_module("src.transcription.diarize")
        cls.manager_module = importlib.import_module("src.transcription.manager")
        cls.diarization_runtime = importlib.import_module("src.transcription.diarization_runtime")
        cls.speaker_attribution = importlib.import_module("src.transcription.speaker_attribution")
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
                transcript, duration, metadata = asyncio.run(
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
        self.assertEqual(metadata["diarization_backend"], "pyannote")
        self.assertEqual(metadata["diarization_model"], "pyannote/speaker-diarization-community-1")
        self.assertEqual(len(repository.added_segments), 2)
        self.assertEqual(repository.added_segments[0].speaker_cluster, "SPEAKER_00")
        self.assertEqual(repository.added_segments[1].speaker_cluster, "SPEAKER_01")
        self.assertEqual(repository.has_transcription_updates, [("session-1", True)])
        self.assertEqual(repository.reindexed_sessions, ["session-1"])
        self.assertTrue(repository.transcript_version_updates)
        self.assertIsNone(manager.repair_reason)

    def test_transcribe_and_persist_session_passes_repair_reason(self):
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
                asyncio.run(
                    self.export._transcribe_and_persist_session(
                        session_id="session-1",
                        session=session,
                        repository=repository,
                        transcription_manager=manager,
                        primary_meeting_id="meeting-1",
                        transcript_version_id="tv-1",
                        expected_speaker_count=3,
                        late_join_offset_seconds=120.0,
                        repair_reason="missing_speaker",
                    )
                )

        self.assertEqual(manager.expected_speaker_count, 3)
        self.assertEqual(manager.late_join_offset_seconds, 120.0)
        self.assertEqual(manager.repair_reason, "missing_speaker")

    def test_whisperx_local_streaming_preview_is_not_supported(self):
        engine = self.whisperx_local.WhisperXLocalEngine()

        with self.assertRaises(RuntimeError):
            asyncio.run(engine.transcribe(audio=[], sample_rate=16000))

    def test_whisperx_local_file_transcription_uses_pyav_decode(self):
        engine = self.whisperx_local.WhisperXLocalEngine()
        engine._initialized = True

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

        fake_whisperx = types.ModuleType("whisperx")
        fake_whisperx.align = fake_align

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
            self.whisperx_local,
            "ensure_diarization_ready",
            return_value={"enabled": True, "ready": True},
        ), patch.object(
            self.diarize_module,
            "diarize",
            return_value=[(0.0, 1.0, "SPEAKER_00")],
        ), patch.dict(sys.modules, {"whisperx": fake_whisperx}):
            result = engine._transcribe_file_sync("sample.wav", None)

        decode_mock.assert_called_once_with("sample.wav", sample_rate=16000)
        self.assertEqual(result.duration_seconds, 1.0)
        self.assertEqual(result.text, "Hello world")
        self.assertEqual(result.segments[0].speaker_cluster, "SPEAKER_00")
        self.assertEqual(result.diarization_backend, "pyannote")
        self.assertEqual(result.repair_strategy, "full_file_unconstrained")

    def test_repair_uses_same_full_file_pyannote_path_as_initial_transcription(self):
        engine = self.whisperx_local.WhisperXLocalEngine()
        engine._initialized = True
        engine._model = SimpleNamespace(
            transcribe=lambda _audio, batch_size, language=None: {
                "language": "en",
                "segments": [
                    {"start": 0.0, "end": 2.0, "text": "Hello Greg"},
                    {"start": 2.0, "end": 4.0, "text": "Hi Oscar"},
                    {"start": 4.0, "end": 6.0, "text": "Greg joined"},
                ],
            }
        )

        fake_whisperx = types.ModuleType("whisperx")
        fake_whisperx.align = lambda *_args, **_kwargs: {
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Hello Greg"},
                {"start": 2.0, "end": 4.0, "text": "Hi Oscar"},
                {"start": 4.0, "end": 6.0, "text": "Greg joined"},
            ],
        }

        with patch.object(
            self.whisperx_local,
            "decode_audio_to_mono_float32",
            return_value=np.ones(32000, dtype=np.float32),
        ), patch.object(
            self.whisperx_local,
            "duration_seconds_from_audio",
            return_value=2.0,
        ), patch.object(
            engine,
            "_get_align_model",
            return_value=("align-model", {"language": "en"}),
        ), patch.object(
            self.whisperx_local,
            "ensure_diarization_ready",
            return_value={"enabled": True, "ready": True},
        ), patch.object(
            self.diarize_module,
            "diarize",
            return_value=[(0.0, 2.0, "SPEAKER_02"), (2.0, 4.0, "SPEAKER_01"), (4.0, 6.0, "SPEAKER_00")],
        ) as diarize_mock, patch.dict(sys.modules, {"whisperx": fake_whisperx}):
            result = engine._transcribe_file_sync(
                "sample.wav",
                None,
                expected_speaker_count=3,
                late_join_offset_seconds=75.0,
                repair_reason="missing_speaker",
            )

        diarize_mock.assert_called_once()
        self.assertIsNone(diarize_mock.call_args.kwargs.get("start_offset"))
        self.assertIsNone(diarize_mock.call_args.kwargs.get("end_offset"))
        self.assertEqual(result.diarization_backend, "pyannote")
        self.assertEqual(result.diarization_model, "pyannote/speaker-diarization-community-1")
        self.assertEqual(result.segments[0].speaker_cluster, "SPEAKER_02")
        self.assertEqual(result.repair_strategy, "full_file_exact_count")
        self.assertEqual(result.diarization_actual_speaker_count, 3)

    def test_repair_quality_gate_rejects_wrong_count_and_high_unassigned_ratio(self):
        self.assertFalse(
            self.speaker_attribution.repair_quality_gate_passed(
                {
                    "actual_speaker_count": 4,
                    "unassigned_segment_ratio": 0.0,
                },
                expected_speaker_count=3,
            )
        )
        self.assertFalse(
            self.speaker_attribution.repair_quality_gate_passed(
                {
                    "actual_speaker_count": 3,
                    "unassigned_segment_ratio": 0.2,
                },
                expected_speaker_count=3,
            )
        )

    def test_required_diarization_runtime_reports_load_failure(self):
        with patch.object(
            self.diarization_runtime,
            "preload_required_pipeline",
            side_effect=RuntimeError("HF_TOKEN missing or invalid"),
        ):
            state = self.diarization_runtime.preload_diarization_runtime(
                enabled=True,
                hf_token="",
            )

        self.assertTrue(state["enabled"])
        self.assertFalse(state["ready"])
        self.assertIn("HF_TOKEN missing or invalid", state["error"])

    def test_whisperx_unload_releases_cached_models_and_cuda_memory(self):
        engine = self.whisperx_local.WhisperXLocalEngine()
        engine._initialized = True
        engine._model = object()
        engine._align_models["en"] = (object(), object())

        fake_cuda = SimpleNamespace(
            is_available=MagicMock(return_value=True),
            empty_cache=MagicMock(),
            ipc_collect=MagicMock(),
        )
        fake_torch = SimpleNamespace(cuda=fake_cuda)

        with patch.object(self.whisperx_local.gc, "collect") as gc_collect, patch.dict(
            sys.modules,
            {"torch": fake_torch},
        ):
            asyncio.run(engine.unload())

        self.assertIsNone(engine._model)
        self.assertEqual(engine._align_models, {})
        self.assertFalse(engine._initialized)
        gc_collect.assert_called_once_with()
        fake_cuda.empty_cache.assert_called_once_with()
        fake_cuda.ipc_collect.assert_called_once_with()
