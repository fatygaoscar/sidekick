"""Workspace-scoped grounded chat service."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from src.core.datetime_utils import to_utc_iso
from src.core.speaker_labels import build_user_facing_speaker_map, resolve_user_facing_speaker_name
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager


_TOKEN_RE = re.compile(r"[A-Za-z0-9_']+")


class WorkspaceChatService:
    """Manage grounded chat turns and draft-apply actions for one recording."""

    def __init__(
        self,
        repository: Repository,
        summarization_manager: SummarizationManager | None = None,
    ) -> None:
        self._repository = repository
        self._summarization_manager = summarization_manager

    async def get_or_create_thread(self, session_id: str, meeting_id: str):
        """Return the persistent chat thread for a recording."""
        return await self._repository.get_or_create_workspace_chat_thread(session_id, meeting_id)

    async def list_messages(self, session_id: str, meeting_id: str) -> tuple[object, list[object]]:
        """Load the active thread and all messages for one recording."""
        thread = await self.get_or_create_thread(session_id, meeting_id)
        messages = await self._repository.list_workspace_chat_messages(thread.id)
        return thread, messages

    async def append_system_event(
        self,
        *,
        session_id: str,
        meeting_id: str,
        transcript_version_id: str | None,
        summary_id: str | None,
        template_key: str | None,
        content: str,
        message_type: str = "info",
    ):
        """Append a user-visible system event if a thread already exists."""
        thread = await self._repository.get_workspace_chat_thread(session_id)
        if thread is None:
            return None
        return await self._repository.add_workspace_chat_message(
            thread_id=thread.id,
            session_id=session_id,
            meeting_id=meeting_id,
            transcript_version_id=transcript_version_id,
            summary_id=summary_id,
            role="system",
            message_type=message_type,
            content=content,
            template_key=template_key,
        )

    async def maybe_append_context_event(
        self,
        *,
        thread_id: str,
        session_id: str,
        meeting_id: str,
        transcript_version_id: str | None,
        summary_id: str | None,
        template_key: str | None,
        content: str,
    ) -> None:
        """Insert a system marker when the active chat context changes."""
        latest = await self._repository.get_latest_workspace_chat_message(thread_id)
        if latest is None:
            return
        if (
            str(getattr(latest, "transcript_version_id", None) or "") == str(transcript_version_id or "")
            and str(getattr(latest, "summary_id", None) or "") == str(summary_id or "")
        ):
            return
        await self._repository.add_workspace_chat_message(
            thread_id=thread_id,
            session_id=session_id,
            meeting_id=meeting_id,
            transcript_version_id=transcript_version_id,
            summary_id=summary_id,
            role="system",
            message_type="context_switch",
            content=content,
            template_key=template_key,
        )

    async def send_message(
        self,
        *,
        session: Any,
        meeting: Any,
        transcript_version: Any,
        current_summary: Any | None,
        content: str,
        context_event_text: str,
    ) -> tuple[object, object]:
        """Create one user turn, grounded assistant answer, and audit trail."""
        if self._summarization_manager is None:
            raise RuntimeError("Summarization manager is required for chat turns")

        thread = await self.get_or_create_thread(session.id, meeting.id)
        template_key = getattr(transcript_version, "template_key", None) or getattr(meeting, "template_key", None)
        await self.maybe_append_context_event(
            thread_id=thread.id,
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=getattr(transcript_version, "id", None),
            summary_id=getattr(current_summary, "id", None),
            template_key=template_key,
            content=context_event_text,
        )

        user_message = await self._repository.add_workspace_chat_message(
            thread_id=thread.id,
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=getattr(transcript_version, "id", None),
            summary_id=getattr(current_summary, "id", None),
            role="user",
            message_type="user_question",
            content=content,
            template_key=template_key,
        )

        thread_messages = await self._repository.list_workspace_chat_messages(thread.id)
        recent_turns = self._serialize_recent_turns(thread_messages)
        evidence_windows = await self._retrieve_evidence_windows(
            session=session,
            meeting=meeting,
            transcript_version=transcript_version,
            question=content,
        )
        answer = await self._summarization_manager.chat_about_recording(
            question=content,
            current_summary=(current_summary.content if current_summary else None),
            evidence_windows=[
                {
                    "recording_title": window["recording_title"],
                    "recorded_at": window["recorded_at"],
                    "speaker": window["speaker"],
                    "timestamp": window["timestamp"],
                    "snippet": window["snippet"],
                }
                for window in evidence_windows
            ],
            recent_turns=recent_turns,
        )

        assistant_message = await self._repository.add_workspace_chat_message(
            thread_id=thread.id,
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=getattr(transcript_version, "id", None),
            summary_id=getattr(current_summary, "id", None),
            role="assistant",
            message_type="assistant_answer",
            content=str(answer.get("answer") or "").strip() or "I don’t have a grounded answer for that yet.",
            citations_json=json.dumps(answer.get("citations") or []),
            retrieval_windows_json=json.dumps(evidence_windows),
            intent_label=str(answer.get("intent_label") or "general_qa"),
            intent_confidence=float(answer.get("intent_confidence") or 0.0),
            suggests_summary_change=bool(answer.get("suggests_summary_change")),
            suggested_change_kind=answer.get("suggested_change_kind"),
            apply_ready=bool(answer.get("apply_ready")) and current_summary is not None,
            template_key=template_key,
            metadata_json=json.dumps(
                {
                    "confidence": answer.get("confidence") or "low",
                    "user_message_id": user_message.id,
                }
            ),
        )
        return user_message, assistant_message

    async def apply_message_to_summary(
        self,
        *,
        session: Any,
        meeting: Any,
        transcript_version: Any,
        assistant_message: Any,
        base_summary: Any,
        context_event_text: str,
    ) -> dict[str, Any]:
        """Apply one assistant turn to the current summary context and update the draft."""
        if self._summarization_manager is None:
            raise RuntimeError("Summarization manager is required for apply actions")

        thread = await self.get_or_create_thread(session.id, meeting.id)
        template_key = getattr(transcript_version, "template_key", None) or getattr(meeting, "template_key", None)
        await self.maybe_append_context_event(
            thread_id=thread.id,
            session_id=session.id,
            meeting_id=meeting.id,
            transcript_version_id=getattr(transcript_version, "id", None),
            summary_id=getattr(base_summary, "id", None),
            template_key=template_key,
            content=context_event_text,
        )

        retrieval_windows = self._loads_json(getattr(assistant_message, "retrieval_windows_json", None), [])
        thread_messages = await self._repository.list_workspace_chat_messages(thread.id)
        recent_turns = self._serialize_recent_turns_until(thread_messages, assistant_message.id)
        assistant_metadata = self._loads_json(getattr(assistant_message, "metadata_json", None), {})
        user_message = None
        user_message_id = assistant_metadata.get("user_message_id")
        if user_message_id:
            user_message = await self._repository.get_workspace_chat_message(str(user_message_id))

        apply_result = await self._summarization_manager.apply_chat_turn_to_summary(
            instruction=(user_message.content if user_message else assistant_message.content),
            current_summary=base_summary.content,
            evidence_windows=[
                {
                    "recording_title": window.get("recording_title"),
                    "recorded_at": window.get("recorded_at"),
                    "speaker": window.get("speaker"),
                    "timestamp": window.get("timestamp"),
                    "snippet": window.get("snippet"),
                }
                for window in retrieval_windows
            ],
            assistant_answer=assistant_message.content,
            recent_turns=recent_turns,
        )
        changed = bool(apply_result.get("changed"))
        reason = str(apply_result.get("reason") or "").strip() or None
        revised_summary = str(apply_result.get("revised_summary") or base_summary.content).strip()

        draft = None
        if changed:
            if getattr(base_summary, "status", None) == "draft":
                draft = await self._repository.update_summary(
                    base_summary.id,
                    content=revised_summary,
                    source_type="chat_applied",
                )
            else:
                draft = await self._repository.create_draft_from_summary(
                    base_summary.id,
                    source_type="chat_applied",
                )
                draft = await self._repository.update_summary(
                    draft.id,
                    content=revised_summary,
                    source_type="chat_applied",
                )
            await self._repository.update_workspace_chat_message(
                assistant_message.id,
                applied_summary_id=base_summary.id,
                applied_draft_summary_id=(draft.id if draft else None),
                applied_at=datetime.utcnow(),
            )
            system_message = await self._repository.add_workspace_chat_message(
                thread_id=thread.id,
                session_id=session.id,
                meeting_id=meeting.id,
                transcript_version_id=getattr(transcript_version, "id", None),
                summary_id=(draft.id if draft else getattr(base_summary, "id", None)),
                role="system",
                message_type="apply_event",
                content="Applied the assistant suggestion to the current draft.",
                applied_summary_id=base_summary.id,
                applied_draft_summary_id=(draft.id if draft else None),
                applied_at=datetime.utcnow(),
                template_key=template_key,
            )
        else:
            system_message = await self._repository.add_workspace_chat_message(
                thread_id=thread.id,
                session_id=session.id,
                meeting_id=meeting.id,
                transcript_version_id=getattr(transcript_version, "id", None),
                summary_id=getattr(base_summary, "id", None),
                role="system",
                message_type="apply_event",
                content=reason or "No draft change was applied.",
                template_key=template_key,
            )

        return {
            "draft_summary": draft,
            "changed": changed,
            "reason": reason,
            "system_message": system_message,
        }

    def _serialize_recent_turns(self, messages: list[Any]) -> list[dict[str, str]]:
        serialized: list[dict[str, str]] = []
        for message in messages[-8:]:
            if getattr(message, "role", "") not in {"user", "assistant"}:
                continue
            serialized.append(
                {
                    "role": str(getattr(message, "role", "")),
                    "content": str(getattr(message, "content", "")).strip(),
                }
            )
        return serialized

    def _serialize_recent_turns_until(self, messages: list[Any], message_id: str) -> list[dict[str, str]]:
        scoped: list[Any] = []
        for message in messages:
            scoped.append(message)
            if str(getattr(message, "id", "")) == str(message_id):
                break
        return self._serialize_recent_turns(scoped)

    async def _retrieve_evidence_windows(
        self,
        *,
        session: Any,
        meeting: Any,
        transcript_version: Any,
        question: str,
    ) -> list[dict[str, Any]]:
        segments = await self._repository.get_segments(
            session_id=session.id,
            transcript_version_id=getattr(transcript_version, "id", None),
        )
        if not segments:
            return []

        query_tokens = self._ordered_query_tokens(question)
        if not query_tokens:
            return []

        scored_candidates: list[tuple[float, int]] = []
        for index, segment in enumerate(segments):
            score = self._segment_score(segment, query_tokens)
            if score <= 0:
                continue
            scored_candidates.append((score, index))
        scored_candidates.sort(key=lambda item: item[0], reverse=True)

        windows: list[dict[str, Any]] = []
        seen_windows: set[tuple[str, ...]] = set()
        for score, center_index in scored_candidates:
            window_segments = self._window_for_center(segments, center_index)
            segment_ids = tuple(str(getattr(segment, "id")) for segment in window_segments)
            if not segment_ids or segment_ids in seen_windows:
                continue
            seen_windows.add(segment_ids)
            window = self._serialize_window(
                session=session,
                meeting=meeting,
                transcript_version=transcript_version,
                window_segments=window_segments,
                score=score,
            )
            windows.append(window)
            if len(windows) >= 6:
                break
        return windows

    def _segment_score(self, segment: Any, query_tokens: list[str]) -> float:
        snippet = str(getattr(segment, "text", "")).strip().lower()
        speaker = str(getattr(segment, "speaker", "") or "").strip().lower()
        score = 0.0
        for token in query_tokens:
            if token in snippet:
                score += 1.5
            if speaker and token in speaker:
                score += 2.0
        if len(query_tokens) >= 2:
            phrase = " ".join(query_tokens[: min(len(query_tokens), 5)])
            if phrase and phrase in snippet:
                score += 2.5
        return score

    def _window_for_center(self, segments: list[Any], center_index: int) -> list[Any]:
        selected = [segments[center_index]]
        previous_index = center_index - 1
        while previous_index >= 0 and len(selected) < 4:
            current = segments[previous_index + 1]
            previous = segments[previous_index]
            gap = max(
                0.0,
                float(getattr(current, "start_time", 0.0)) - float(getattr(previous, "end_time", 0.0)),
            )
            if gap > 45.0:
                break
            selected.insert(0, previous)
            if len(selected) >= 2:
                break
            previous_index -= 1

        next_index = center_index + 1
        while next_index < len(segments) and len(selected) < 4:
            last = selected[-1]
            nxt = segments[next_index]
            gap = max(
                0.0,
                float(getattr(nxt, "start_time", 0.0)) - float(getattr(last, "end_time", 0.0)),
            )
            if gap > 45.0:
                break
            selected.append(nxt)
            next_index += 1

        text = " ".join(str(getattr(segment, "text", "")).strip() for segment in selected).strip()
        while len(text) > 900 and len(selected) > 1:
            if len(selected) > 3:
                selected.pop()
            else:
                selected.pop(0)
            text = " ".join(str(getattr(segment, "text", "")).strip() for segment in selected).strip()

        return selected

    def _serialize_window(
        self,
        *,
        session: Any,
        meeting: Any,
        transcript_version: Any,
        window_segments: list[Any],
        score: float,
    ) -> dict[str, Any]:
        raw_labels = [
            str(getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None) or "").strip()
            for segment in window_segments
        ]
        fallback_map = build_user_facing_speaker_map(raw_labels)
        lead = window_segments[0]
        lead_speaker = resolve_user_facing_speaker_name(
            getattr(lead, "speaker", None),
            getattr(lead, "speaker_cluster", None) or getattr(lead, "speaker", None),
            fallback_map,
        )
        start_time = float(getattr(window_segments[0], "start_time", 0.0) or 0.0)
        end_time = float(getattr(window_segments[-1], "end_time", start_time) or start_time)
        return {
            "session_id": str(session.id),
            "meeting_id": str(meeting.id),
            "transcript_version_id": str(getattr(transcript_version, "id", "") or ""),
            "recording_title": str(getattr(meeting, "title", None) or "Untitled Recording"),
            "recorded_at": to_utc_iso(getattr(session, "started_at", None)),
            "speaker": lead_speaker,
            "timestamp": self._format_timestamp(start_time),
            "snippet": " ".join(str(getattr(segment, "text", "")).strip() for segment in window_segments).strip(),
            "transcript_segment_ids": [str(getattr(segment, "id")) for segment in window_segments],
            "start_time": start_time,
            "end_time": end_time,
            "score": round(float(score), 4),
        }

    def _format_timestamp(self, seconds: float) -> str:
        total_seconds = max(0, int(math.floor(seconds)))
        minutes, remainder = divmod(total_seconds, 60)
        return f"[{minutes:02d}:{remainder:02d}]"

    def _ordered_query_tokens(self, value: str) -> list[str]:
        seen: set[str] = set()
        tokens: list[str] = []
        for token in _TOKEN_RE.findall(value or ""):
            normalized = token.lower().strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            tokens.append(normalized)
        return tokens

    def _loads_json(self, value: str | None, default: Any) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
