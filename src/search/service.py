"""Transcript-first AI search across completed recordings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
import re
from typing import Any

from src.core.datetime_utils import localize_datetime
from src.core.speaker_labels import build_user_facing_speaker_map, resolve_user_facing_speaker_name
from src.sessions.repository import Repository
from src.summarization.manager import SummarizationManager


_TOKEN_RE = re.compile(r"[A-Za-z0-9_']+")


@dataclass(slots=True)
class SearchWindow:
    """Candidate evidence window for answer synthesis."""

    session_id: str
    meeting_id: str | None
    transcript_version_id: str | None
    recording_title: str
    recorded_at: datetime
    timezone_name: str | None
    timezone_offset_minutes: int | None
    speaker: str | None
    speaker_cluster: str | None
    start_time: float
    end_time: float
    timestamp: str
    snippet: str
    transcript_segment_ids: list[str]
    query_rank: float
    score: float


class RecordingSearchService:
    """Search recordings with transcript retrieval and grounded answer synthesis."""

    def __init__(
        self,
        repository: Repository,
        summarization_manager: SummarizationManager,
    ) -> None:
        self._repository = repository
        self._summarization_manager = summarization_manager

    async def search(
        self,
        *,
        query: str,
        date_from: date | None = None,
        date_to: date | None = None,
        speaker: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search completed recordings and synthesize an answer from grounded evidence."""
        normalized_query = self._normalize_query(query)
        normalized_speaker = self._normalize_optional_text(speaker)
        fts_query = self._build_fts_query(normalized_query)
        candidates = await self._repository.search_transcript_segments(
            query=fts_query,
            limit=max(limit * 3, 24),
            date_from=date_from,
            date_to=date_to,
            speaker=normalized_speaker,
        )

        if not candidates:
            return {
                "query": normalized_query,
                "answer": "I didn’t find grounded transcript evidence for that in the searched recordings.",
                "confidence": "low",
                "results": [],
                "retrieval_count": 0,
            }

        windows = await self._build_windows(
            candidates=candidates,
            query=normalized_query,
            limit=limit,
        )
        if not windows:
            return {
                "query": normalized_query,
                "answer": "I didn’t find grounded transcript evidence for that in the searched recordings.",
                "confidence": "low",
                "results": [],
                "retrieval_count": 0,
            }

        answer = None
        confidence = "low"
        cited_indexes: list[int] = []
        try:
            answer_payload = await self._summarization_manager.answer_question_with_citations(
                question=normalized_query,
                evidence_windows=[
                    {
                        "recording_title": window.recording_title,
                        "recorded_at": window.recorded_at.isoformat(),
                        "speaker": window.speaker,
                        "timestamp": window.timestamp,
                        "snippet": window.snippet,
                    }
                    for window in windows
                ],
            )
            answer = answer_payload.get("answer") or None
            confidence = str(answer_payload.get("confidence") or "low")
            cited_indexes = [
                int(index)
                for index in answer_payload.get("citations", [])
                if isinstance(index, int) and 0 <= int(index) < len(windows)
            ]
        except Exception:
            answer = None
            confidence = "low"

        results = []
        cited_set = set(cited_indexes)
        for index, window in enumerate(windows):
            date_label, time_label = self._recorded_labels(
                window.recorded_at,
                window.timezone_name,
                window.timezone_offset_minutes,
            )
            results.append(
                {
                    "citation_id": f"c{index + 1}",
                    "session_id": window.session_id,
                    "meeting_id": window.meeting_id,
                    "transcript_version_id": window.transcript_version_id,
                    "recording_title": window.recording_title,
                    "recorded_at": window.recorded_at.isoformat().replace("+00:00", "Z"),
                    "recorded_date_label": date_label,
                    "recorded_time_label": time_label,
                    "speaker": window.speaker,
                    "speaker_cluster": window.speaker_cluster,
                    "start_time": window.start_time,
                    "end_time": window.end_time,
                    "timestamp": window.timestamp,
                    "snippet": window.snippet,
                    "transcript_segment_ids": window.transcript_segment_ids,
                    "score": round(window.score, 4),
                    "is_cited": index in cited_set,
                }
            )

        return {
            "query": normalized_query,
            "answer": answer,
            "confidence": confidence,
            "results": results,
            "retrieval_count": len(results),
        }

    async def _build_windows(
        self,
        *,
        candidates: list[dict[str, Any]],
        query: str,
        limit: int,
    ) -> list[SearchWindow]:
        session_segments: dict[tuple[str, str | None], list[Any]] = {}
        unique_segment_sets = sorted(
            {
                (
                    str(candidate["session_id"]),
                    str(candidate["transcript_version_id"]) if candidate.get("transcript_version_id") else None,
                )
                for candidate in candidates
            }
        )
        for session_id, transcript_version_id in unique_segment_sets:
            session_segments[(session_id, transcript_version_id)] = await self._repository.get_segments(
                session_id=session_id,
                transcript_version_id=transcript_version_id,
            )

        query_tokens = set(self._ordered_query_tokens(query))
        windows: list[SearchWindow] = []
        seen_keys: dict[tuple[str, tuple[str, ...]], int] = {}

        for candidate in candidates:
            segment_key = (
                str(candidate["session_id"]),
                str(candidate["transcript_version_id"]) if candidate.get("transcript_version_id") else None,
            )
            segments = session_segments.get(segment_key, [])
            if not segments:
                continue
            window_segments = self._window_for_candidate(candidate, segments)
            if not window_segments:
                continue
            window = self._serialize_window(candidate, window_segments, query_tokens)
            key = (window.session_id, tuple(window.transcript_segment_ids))
            previous_index = seen_keys.get(key)
            if previous_index is not None:
                if windows[previous_index].score < window.score:
                    windows[previous_index] = window
                continue
            seen_keys[key] = len(windows)
            windows.append(window)

        windows.sort(key=lambda item: item.score, reverse=True)

        per_session_count: defaultdict[str, int] = defaultdict(int)
        filtered: list[SearchWindow] = []
        for window in windows:
            if per_session_count[window.session_id] >= 3 and len(windows) >= 5:
                continue
            filtered.append(window)
            per_session_count[window.session_id] += 1
            if len(filtered) >= limit:
                break

        return filtered

    def _window_for_candidate(
        self,
        candidate: dict[str, Any],
        segments: list[Any],
    ) -> list[Any]:
        segment_ids = [str(getattr(segment, "id")) for segment in segments]
        try:
            center_index = segment_ids.index(str(candidate["segment_id"]))
        except ValueError:
            return []

        selected = [segments[center_index]]

        previous_index = center_index - 1
        while previous_index >= 0 and len(selected) < 4:
            current = segments[previous_index + 1]
            previous = segments[previous_index]
            gap = max(0.0, float(getattr(current, "start_time", 0.0)) - float(getattr(previous, "end_time", 0.0)))
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
            gap = max(0.0, float(getattr(nxt, "start_time", 0.0)) - float(getattr(last, "end_time", 0.0)))
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
        candidate: dict[str, Any],
        window_segments: list[Any],
        query_tokens: set[str],
    ) -> SearchWindow:
        snippet = " ".join(str(getattr(segment, "text", "")).strip() for segment in window_segments).strip()
        transcript_segment_ids = [str(getattr(segment, "id")) for segment in window_segments]
        raw_labels = [
            str(getattr(segment, "speaker_cluster", None) or getattr(segment, "speaker", None) or "").strip()
            for segment in window_segments
        ]
        fallback_map = build_user_facing_speaker_map(raw_labels)
        speaker = resolve_user_facing_speaker_name(
            candidate.get("speaker"),
            candidate.get("speaker_cluster") or candidate.get("speaker"),
            fallback_map,
        )
        rank = abs(float(candidate.get("rank", 0.0) or 0.0))
        base_score = 1.0 / (1.0 + rank)
        title = self._normalize_optional_text(candidate.get("meeting_title")) or "Untitled Recording"
        title_tokens = set(self._ordered_query_tokens(title))
        snippet_tokens = set(self._ordered_query_tokens(snippet))
        speaker_tokens = set(self._ordered_query_tokens(speaker or ""))
        overlap = len(query_tokens & snippet_tokens)
        title_overlap = len(query_tokens & title_tokens)
        recorded_at = self._coerce_datetime(candidate.get("session_started_at"))

        score = base_score
        if speaker_tokens and query_tokens & speaker_tokens:
            score += 0.2
        if title_overlap >= 2:
            score += 0.15
        if overlap >= 3:
            score += 0.1
        if len(snippet) < 80 and not (query_tokens & speaker_tokens):
            score -= 0.1
        if recorded_at >= datetime.now(UTC).replace(tzinfo=None) - timedelta(days=14):
            score += 0.05
        if candidate.get("is_important"):
            score += 0.05

        start_time = float(getattr(window_segments[0], "start_time", 0.0) or 0.0)
        end_time = float(getattr(window_segments[-1], "end_time", start_time) or start_time)

        return SearchWindow(
            session_id=str(candidate["session_id"]),
            meeting_id=str(candidate["meeting_id"]) if candidate.get("meeting_id") else None,
            transcript_version_id=(
                str(candidate["transcript_version_id"])
                if candidate.get("transcript_version_id")
                else None
            ),
            recording_title=title,
            recorded_at=recorded_at,
            timezone_name=candidate.get("timezone_name"),
            timezone_offset_minutes=candidate.get("timezone_offset_minutes"),
            speaker=speaker,
            speaker_cluster=self._normalize_optional_text(candidate.get("speaker_cluster")),
            start_time=start_time,
            end_time=end_time,
            timestamp=self._format_timestamp(start_time),
            snippet=snippet,
            transcript_segment_ids=transcript_segment_ids,
            query_rank=rank,
            score=score,
        )

    def _build_fts_query(self, query: str) -> str:
        tokens = [token for token in self._ordered_query_tokens(query) if len(token) >= 2]
        unique_tokens = []
        seen = set()
        for token in tokens:
            if token in seen:
                continue
            unique_tokens.append(token)
            seen.add(token)
        if not unique_tokens:
            return f'"{query.lower()}"'
        phrases = []
        if len(unique_tokens) > 1:
            phrases.append(f'"{" ".join(unique_tokens)}"')
        phrases.extend(unique_tokens)
        return " OR ".join(phrases)

    def _ordered_query_tokens(self, value: str) -> list[str]:
        return [token.lower() for token in _TOKEN_RE.findall(value or "") if token.strip()]

    def _normalize_query(self, value: str) -> str:
        normalized = re.sub(r"\s+", " ", str(value or "").strip())
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1].strip()
        return normalized

    def _normalize_optional_text(self, value: Any) -> str | None:
        normalized = re.sub(r"\s+", " ", str(value or "").strip())
        return normalized or None

    def _recorded_labels(
        self,
        recorded_at: datetime,
        timezone_name: str | None,
        timezone_offset_minutes: int | None,
    ) -> tuple[str, str]:
        localized = localize_datetime(recorded_at, timezone_name, timezone_offset_minutes)
        return (
            localized.strftime("%b %d, %Y"),
            localized.strftime("%I:%M%p").lstrip("0"),
        )

    def _format_timestamp(self, seconds: float) -> str:
        total_seconds = max(0, int(math.floor(seconds)))
        minutes, remainder = divmod(total_seconds, 60)
        return f"[{minutes:02d}:{remainder:02d}]"

    def _coerce_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        text = str(value or "")
        if not text:
            return datetime.now(UTC).replace(tzinfo=None)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
