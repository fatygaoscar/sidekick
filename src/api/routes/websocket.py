"""WebSocket endpoint for audio streaming and live transcription."""

import asyncio
import json
from typing import Any

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.settings import get_settings
from src.audio.buffer import AudioBuffer
from src.audio.vad import VoiceActivityDetector
from src.core.events import Event, EventType, get_event_bus
from src.sessions.manager import SessionManager
from src.transcription.manager import TranscriptionManager


router = APIRouter()


class AudioWebSocketHandler:
    """Handles WebSocket connection for audio streaming."""

    def __init__(
        self,
        websocket: WebSocket,
        session_manager: SessionManager,
        transcription_manager: TranscriptionManager,
    ) -> None:
        self._websocket = websocket
        self._session_manager = session_manager
        self._transcription_manager = transcription_manager
        self._settings = get_settings()
        self._event_bus = get_event_bus()

        # Audio processing components
        self._vad = VoiceActivityDetector(
            sample_rate=self._settings.audio_sample_rate,
            aggressiveness=self._settings.vad_aggressiveness,
        )
        self._buffer = AudioBuffer(sample_rate=self._settings.audio_sample_rate)

        self._is_running = False
        self._process_task: asyncio.Task | None = None

    async def handle(self) -> None:
        """Main handler for WebSocket connection."""
        await self._websocket.accept()
        self._is_running = True

        # Subscribe to events to forward to client
        self._event_bus.subscribe(EventType.TRANSCRIPTION_SEGMENT, self._on_transcription)

        # Emit connected event
        await self._event_bus.emit(
            EventType.WEBSOCKET_CONNECTED,
            {"client": str(self._websocket.client)},
            source="websocket",
        )

        try:
            # Start processing task
            self._process_task = asyncio.create_task(self._process_buffer())

            # Send initial state
            await self._send_state()

            # Receive audio data
            while self._is_running:
                try:
                    message = await self._websocket.receive()

                    if message["type"] == "websocket.disconnect":
                        break

                    if "bytes" in message:
                        await self._handle_audio(message["bytes"])
                    elif "text" in message:
                        await self._handle_command(message["text"])

                except WebSocketDisconnect:
                    break

        finally:
            self._is_running = False
            if self._process_task:
                self._process_task.cancel()
                try:
                    await self._process_task
                except asyncio.CancelledError:
                    pass

            self._event_bus.unsubscribe(EventType.TRANSCRIPTION_SEGMENT, self._on_transcription)

            await self._event_bus.emit(
                EventType.WEBSOCKET_DISCONNECTED,
                {"client": str(self._websocket.client)},
                source="websocket",
            )

    async def _handle_audio(self, data: bytes) -> None:
        """Handle incoming audio data."""
        # Convert bytes to numpy array (expecting 16-bit PCM)
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

        # Get speech ratio for VAD
        speech_ratio = self._vad.get_speech_ratio(audio)
        is_speech = speech_ratio > 0.3

        # Add to buffer
        await self._buffer.add_chunk(audio, is_speech=is_speech)

    async def _handle_command(self, message: str) -> None:
        """Handle text commands from client."""
        try:
            data = json.loads(message)
            command = data.get("command")

            if command == "start_session":
                mode = data.get("mode", "work")
                submode = data.get("submode")
                await self._session_manager.start_session(mode=mode, submode=submode)
                await self._send_state()

            elif command == "end_session":
                await self._session_manager.end_session()
                await self._send_state()

            elif command == "start_meeting":
                title = data.get("title")
                await self._session_manager.start_meeting(title=title)
                await self._send_state()

            elif command == "end_meeting":
                await self._session_manager.end_meeting()
                await self._send_state()

            elif command == "mark_important":
                note = data.get("note")
                await self._session_manager.mark_important(note=note)
                await self._send_json({
                    "type": "important_marked",
                    "timestamp": self._session_manager.session_elapsed_seconds,
                })

            elif command == "change_mode":
                mode = data.get("mode", "work")
                submode = data.get("submode")
                await self._session_manager.change_mode(mode=mode, submode=submode)
                await self._send_state()

            elif command == "ping":
                await self._send_json({"type": "pong"})

        except json.JSONDecodeError:
            await self._send_json({"type": "error", "message": "Invalid JSON"})
        except Exception as e:
            await self._send_json({"type": "error", "message": str(e)})

    async def _process_buffer(self) -> None:
        """Process audio buffer and send for transcription."""
        while self._is_running:
            try:
                if self._buffer.is_ready:
                    result = await self._buffer.get_audio()
                    if result:
                        audio_data, start_offset, end_offset = result

                        # Only transcribe if we have a session
                        if self._session_manager.current_session:
                            try:
                                transcription = await self._transcription_manager.transcribe(
                                    audio=audio_data,
                                    sample_rate=self._settings.audio_sample_rate,
                                    start_offset=start_offset,
                                )

                                if transcription.text.strip():
                                    # Save to session
                                    await self._session_manager.add_transcript_segment(
                                        text=transcription.text,
                                        start_time=transcription.start_time,
                                        end_time=transcription.end_time,
                                        confidence=transcription.confidence,
                                    )
                            except Exception as e:
                                await self._send_json({
                                    "type": "error",
                                    "message": f"Transcription error: {e}",
                                })

                await asyncio.sleep(0.1)  # Check buffer every 100ms

            except asyncio.CancelledError:
                break
            except Exception as e:
                await self._send_json({"type": "error", "message": str(e)})
                await asyncio.sleep(1)  # Back off on error

    async def _on_transcription(self, event: Event) -> None:
        """Handle transcription events and forward to client."""
        await self._send_json({
            "type": "transcription",
            "text": event.data.get("text", ""),
            "start_time": event.data.get("start_time", 0),
            "end_time": event.data.get("end_time", 0),
            "is_important": event.data.get("is_important", False),
        })

    async def _send_state(self) -> None:
        """Send current state to client."""
        session = self._session_manager.current_session
        meeting = self._session_manager.current_meeting

        state = {
            "type": "state",
            "session": None,
            "meeting": None,
        }

        if session:
            state["session"] = {
                "id": session.id,
                "mode": session.mode,
                "submode": session.submode,
                "is_active": session.is_active,
                "elapsed_seconds": self._session_manager.session_elapsed_seconds,
            }

        if meeting:
            state["meeting"] = {
                "id": meeting.id,
                "title": meeting.title,
                "is_active": meeting.is_active,
            }

        await self._send_json(state)

    async def _send_json(self, data: dict[str, Any]) -> None:
        """Send JSON data to client."""
        try:
            await self._websocket.send_text(json.dumps(data))
        except Exception:
            pass  # Client may have disconnected


@router.websocket("/ws/audio")
async def audio_websocket(websocket: WebSocket):
    """WebSocket endpoint for audio streaming."""
    session_manager = websocket.app.state.session_manager
    transcription_manager = websocket.app.state.transcription_manager

    handler = AudioWebSocketHandler(
        websocket=websocket,
        session_manager=session_manager,
        transcription_manager=transcription_manager,
    )
    await handler.handle()
