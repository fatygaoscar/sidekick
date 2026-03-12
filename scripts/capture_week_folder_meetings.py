#!/usr/bin/env python3
"""Capture legacy `2026 Week ##` meeting notes into the modern year/month layout."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings
from src.core.datetime_utils import localize_datetime
from src.core.markdown_utils import _sanitize_prompt_for_note
from src.core.obsidian_exports import (
    build_frontmatter_text,
    extract_tags_from_frontmatter,
    file_sha256,
    parse_frontmatter_text,
    sanitize_meeting_stem,
    write_obsidian_markdown_atomic,
)
from src.core.obsidian_rewrite import (
    build_saved_summary_frontmatter,
    build_saved_summary_markdown,
    segments_to_transcript,
)


WEEK_PATH_PREFIX = "Meetings/"
WEEK_FOLDER_TOKEN = " Week "
WEEKDAY_TO_ISO = {
    "Mon": 1,
    "Tue": 2,
    "Wed": 3,
    "Thu": 4,
    "Fri": 5,
    "Sat": 6,
    "Sun": 7,
}

ALLOWED_LEGACY_FRONTMATTER_KEYS = {
    "type",
    "sidekick_meeting_id",
    "sidekick_summary_id",
    "sidekick_transcript_version_id",
    "sidekick_summary_version",
    "sidekick_transcript_version",
    "sidekick_export_status",
    "meeting_date",
    "meeting_month",
    "template_key",
    "recording_duration_minutes",
    "tags",
    "exported_at",
}


@dataclass(frozen=True)
class LegacyWeekNote:
    relative_path: str
    year: int
    week: int
    in_archive: bool
    day_token: int
    weekday_token: str
    time_token: str
    clean_title: str
    version_number: int | None
    derived_date: date


@dataclass
class CaptureEntry:
    source_relative_path: str
    destination_relative_path: str | None
    classification: str
    body_mode: str
    property_mode: str
    status: str
    skip_reasons: list[str]
    derived_date: str | None
    clean_title: str | None
    promoted_to_latest: bool
    inferred_version_number: int | None
    db_summary_id: str | None
    db_meeting_id: str | None
    source_sha256: str | None
    destination_sha256: str | None = None
    note: str | None = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-path",
        default=get_settings().obsidian_vault_path,
        help="Absolute Obsidian vault path. Defaults to OBSIDIAN_VAULT_PATH.",
    )
    parser.add_argument("--db-path", default="data/sidekick.db", help="Path to Sidekick SQLite DB.")
    parser.add_argument(
        "--manifest-path",
        default="data/week_capture_manifest.json",
        help="Where to write the JSON manifest/report.",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Optional explicit backup directory. Defaults to a sibling next to Meetings.",
    )
    parser.add_argument("--year", type=int, default=2026, help="Legacy week-folder year to scan.")
    parser.add_argument(
        "--week",
        type=int,
        action="append",
        default=[],
        help="Optional week number filter. Repeatable.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on files written.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write captured notes after creating a full Meetings backup.",
    )
    return parser.parse_args()


def _parse_db_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _payload_normalized_note(note_text: str) -> str:
    lines = []
    for line in _normalized(note_text).splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line == ">":
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines)


def _load_all_saved_summary_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                s.id AS summary_id,
                s.meeting_id AS meeting_id,
                s.created_at AS summary_created_at,
                s.saved_to_obsidian_at AS saved_to_obsidian_at,
                s.obsidian_relative_path AS obsidian_relative_path
            FROM summaries s
            WHERE s.status = 'saved'
              AND s.obsidian_relative_path IS NOT NULL
              AND TRIM(s.obsidian_relative_path) != ''
            ORDER BY s.meeting_id, s.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _load_summary_detail(conn: sqlite3.Connection, summary_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            s.id AS summary_id,
            s.meeting_id AS meeting_id,
            s.transcript_version_id AS transcript_version_id,
            s.content AS summary_content,
            s.created_at AS summary_created_at,
            s.processing_duration_seconds AS processing_duration_seconds,
            s.template AS template_label,
            s.template_key AS template_key,
            s.pass1_system_prompt AS pass1_system_prompt,
            s.pass1_user_prompt AS pass1_user_prompt,
            s.pass2_system_prompt AS pass2_system_prompt,
            s.pass2_user_prompt AS pass2_user_prompt,
            s.saved_to_obsidian_at AS saved_to_obsidian_at,
            s.obsidian_relative_path AS obsidian_relative_path,
            s.workflow_data_json AS workflow_data_json,
            m.title AS meeting_title,
            sess.started_at AS session_started_at,
            sess.timezone_name AS timezone_name,
            sess.timezone_offset_minutes AS timezone_offset_minutes,
            tv.version_number AS transcript_version_number
        FROM summaries s
        JOIN meetings m ON m.id = s.meeting_id
        JOIN sessions sess ON sess.id = m.session_id
        LEFT JOIN transcript_versions tv ON tv.id = s.transcript_version_id
        WHERE s.id = ?
        """,
        (summary_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Summary {summary_id} not found")
    return dict(row)


def _load_segments_for_transcript(
    conn: sqlite3.Connection,
    transcript_version_id: str | None,
) -> list[Any]:
    if not transcript_version_id:
        return []
    return conn.execute(
        """
        SELECT text, start_time, end_time, is_important, speaker, speaker_cluster
        FROM transcript_segments
        WHERE transcript_version_id = ?
        ORDER BY start_time ASC, id ASC
        """,
        (transcript_version_id,),
    ).fetchall()


def _compute_version_map(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_meeting: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_meeting.setdefault(str(row["meeting_id"]), []).append(row)
    version_map: dict[str, int] = {}
    for meeting_rows in by_meeting.values():
        total = len(meeting_rows)
        for index, row in enumerate(meeting_rows):
            version_map[str(row["summary_id"])] = total - index
    return version_map


def _parse_legacy_week_note(relative_path: str, *, expected_year: int) -> LegacyWeekNote | None:
    normalized = relative_path.replace("\\", "/")
    if not normalized.startswith(WEEK_PATH_PREFIX):
        return None
    parts = normalized.split("/")
    if len(parts) not in (3, 4):
        return None
    if parts[0] != "Meetings":
        return None
    folder = parts[1]
    if WEEK_FOLDER_TOKEN not in folder:
        return None
    year_part, week_part = folder.split(WEEK_FOLDER_TOKEN, 1)
    if not (year_part.isdigit() and week_part.isdigit()):
        return None
    year = int(year_part)
    week = int(week_part)
    if year != expected_year:
        return None
    in_archive = len(parts) == 4 and parts[2] == "archive"
    filename = parts[-1]
    if not filename.endswith(".md"):
        return None
    stem = filename[:-3]
    prefix, sep, remainder = stem.partition(" - ")
    if sep == "":
        return None
    prefix_parts = prefix.split(" ")
    if len(prefix_parts) != 3:
        return None
    day_token_raw, weekday_token, time_token = prefix_parts
    if not (day_token_raw.isdigit() and len(day_token_raw) == 2 and time_token.isdigit() and len(time_token) == 4):
        return None
    day_token = int(day_token_raw)
    weekday_key = weekday_token.title()
    weekday_index = WEEKDAY_TO_ISO.get(weekday_key)
    if weekday_index is None:
        return None

    version_number: int | None = None
    clean_title = remainder
    if remainder.endswith(")") and " (v" in remainder:
        title_part, version_part = remainder.rsplit(" (v", 1)
        if version_part.endswith(")") and version_part[:-1].isdigit():
            version_number = int(version_part[:-1])
            clean_title = title_part
    clean_title = clean_title.strip()
    if not clean_title:
        return None

    try:
        derived_date = date.fromisocalendar(year, week, weekday_index)
    except ValueError:
        return None
    if derived_date.day != day_token:
        return None

    return LegacyWeekNote(
        relative_path=normalized,
        year=year,
        week=week,
        in_archive=in_archive,
        day_token=day_token,
        weekday_token=weekday_key,
        time_token=time_token,
        clean_title=clean_title,
        version_number=version_number,
        derived_date=derived_date,
    )


def _group_key(note: LegacyWeekNote) -> tuple[str, str]:
    return (note.derived_date.isoformat(), note.clean_title.lower())


def _select_latest_note(group: list[LegacyWeekNote]) -> tuple[LegacyWeekNote | None, list[str]]:
    reasons: list[str] = []
    root_unversioned = [note for note in group if not note.in_archive and note.version_number is None]
    if len(root_unversioned) > 1:
        return None, ["multiple_unversioned_candidates"]
    if len(root_unversioned) == 1:
        return root_unversioned[0], reasons
    any_unversioned = [note for note in group if note.version_number is None]
    if len(any_unversioned) > 1:
        return None, ["multiple_unversioned_candidates"]
    if len(any_unversioned) == 1:
        return any_unversioned[0], reasons
    versioned = [note for note in group if note.version_number is not None]
    if not versioned:
        return None, ["no_versioned_or_unversioned_candidate"]
    versioned.sort(
        key=lambda note: (
            note.version_number or 0,
            0 if not note.in_archive else 1,
            note.time_token,
        ),
        reverse=True,
    )
    return versioned[0], reasons


def _month_folder_for_date(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _latest_relative_path_for(note: LegacyWeekNote) -> str:
    stem = sanitize_meeting_stem(note.clean_title)
    return f"Meetings/{note.year:04d}/{_month_folder_for_date(note.derived_date)}/{stem}.md"


def _archive_relative_path_for(note: LegacyWeekNote, *, version_number: int | None, suffix: str | None = None) -> str:
    stem = sanitize_meeting_stem(note.clean_title)
    base = Path("Meetings") / f"{note.year:04d}" / _month_folder_for_date(note.derived_date) / "_versions" / stem
    if version_number is not None:
        filename = f"v{version_number}.md"
    else:
        filename = f"legacy-{note.derived_date:%Y%m%d}-{note.time_token}.md"
    if suffix:
        filename = filename[:-3] + f"-{suffix}.md"
    return str(base / filename).replace("\\", "/")


def _matches_prompt_payload(note_payload: str, row: dict[str, Any]) -> bool:
    note_has_prompts = "Pass 1" in note_payload or "Pass 2" in note_payload
    if not note_has_prompts:
        return True
    prompts = [
        _sanitize_prompt_for_note(row.get("pass1_system_prompt"), "Pass 1", "system"),
        _sanitize_prompt_for_note(row.get("pass1_user_prompt"), "Pass 1", "user"),
        _sanitize_prompt_for_note(row.get("pass2_system_prompt"), "Pass 2", "system"),
        _sanitize_prompt_for_note(row.get("pass2_user_prompt"), "Pass 2", "user"),
    ]
    available = [prompt for prompt in prompts if prompt and prompt.strip()]
    if not available:
        return False
    normalized_note = _normalized(note_payload)
    return all(_normalized(prompt) in normalized_note for prompt in available)


def _matches_transcript_payload(note_payload: str, transcript: str) -> bool:
    note_has_transcript = "Transcript" in note_payload
    if not note_has_transcript:
        return True
    if not transcript.strip():
        return False
    return _normalized(transcript) in _normalized(note_payload)


def _is_strict_rewrite_candidate(
    *,
    detail_row: dict[str, Any],
    note_text: str,
    transcript: str,
    frontmatter: dict[str, object],
    source_relative_path: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    payload_note = _payload_normalized_note(note_text)
    if _normalized(str(detail_row.get("summary_content") or "")) not in _normalized(payload_note):
        reasons.append("summary_content_mismatch")
    if not _matches_prompt_payload(payload_note, detail_row):
        reasons.append("prompt_audit_mismatch")
    if not _matches_transcript_payload(payload_note, transcript):
        reasons.append("transcript_mismatch")
    if frontmatter and frontmatter.get("sidekick_summary_id") not in (
        None,
        "",
        str(detail_row["summary_id"]),
    ):
        reasons.append("frontmatter_summary_id_mismatch")
    if source_relative_path != str(detail_row.get("obsidian_relative_path") or "").replace("\\", "/"):
        reasons.append("db_path_mismatch")
    return not reasons, reasons


def _segments_duration_minutes(segments: list[Any]) -> int | None:
    if not segments:
        return None
    end_time = max(float(segment["end_time"]) for segment in segments if segment["end_time"] is not None)
    return max(1, int(end_time // 60))


def _build_db_frontmatter_updates(
    *,
    detail_row: dict[str, Any],
    version_number: int,
    export_status: str,
    tags: list[str],
    duration_minutes: int | None,
) -> dict[str, object]:
    session_started_at = _parse_db_datetime(detail_row.get("session_started_at"))
    if session_started_at is None:
        raise ValueError("missing_session_started_at")
    local_started_at = localize_datetime(
        session_started_at,
        detail_row.get("timezone_name"),
        detail_row.get("timezone_offset_minutes"),
    )
    exported_at_local = localize_datetime(
        _parse_db_datetime(detail_row.get("saved_to_obsidian_at"))
        or _parse_db_datetime(detail_row.get("summary_created_at"))
        or session_started_at,
        detail_row.get("timezone_name"),
        detail_row.get("timezone_offset_minutes"),
    )
    base = build_saved_summary_frontmatter(
        meeting_id=str(detail_row["meeting_id"]),
        summary_id=str(detail_row["summary_id"]),
        transcript_version_id=detail_row.get("transcript_version_id"),
        summary_version_number=version_number,
        transcript_version_number=(
            int(detail_row["transcript_version_number"])
            if detail_row.get("transcript_version_number") is not None
            else None
        ),
        local_started_at=local_started_at,
        template_key=detail_row.get("template_key"),
        recording_duration_minutes=duration_minutes or 1,
        tags=tags,
        exported_at_local=exported_at_local,
        export_status=export_status,
    )
    return base


def _build_legacy_frontmatter_updates(
    *,
    note: LegacyWeekNote,
    export_status: str,
    tags: list[str],
) -> dict[str, object]:
    return {
        "type": "meeting-note",
        "meeting_date": note.derived_date.isoformat(),
        "meeting_month": _month_folder_for_date(note.derived_date),
        "sidekick_export_status": export_status,
        "tags": list(tags),
    }


def _merge_or_prepend_frontmatter(
    note_text: str,
    *,
    frontmatter_updates: dict[str, object],
) -> str:
    frontmatter, body = parse_frontmatter_text(note_text)
    updated = dict(frontmatter)
    updated.update(frontmatter_updates)
    updated.pop("sidekick_is_latest_export", None)
    frontmatter_text = build_frontmatter_text(updated)
    content_body = body if frontmatter else note_text
    content_body = content_body.lstrip("\n")
    return f"{frontmatter_text}\n\n{content_body}" if content_body else f"{frontmatter_text}\n"


def _existing_hash(vault_path: Path, relative_path: str) -> str | None:
    target = vault_path / relative_path
    if not target.exists():
        return None
    return file_sha256(target)


def _next_available_archive_path(
    *,
    vault_path: Path,
    note: LegacyWeekNote,
    version_number: int | None,
    preferred_suffix: str | None = None,
    planned_paths: set[str],
) -> str:
    base = _archive_relative_path_for(note, version_number=version_number, suffix=preferred_suffix)
    if base not in planned_paths and not (vault_path / base).exists():
        return base
    counter = 2
    while True:
        candidate = _archive_relative_path_for(
            note,
            version_number=version_number,
            suffix=f"{preferred_suffix or 'copy'}-{counter}",
        )
        if candidate not in planned_paths and not (vault_path / candidate).exists():
            return candidate
        counter += 1


def _copy_backup(meetings_root: Path, backup_root: Path) -> None:
    if backup_root.exists():
        raise RuntimeError(f"Backup path already exists: {backup_root}")
    shutil.copytree(meetings_root, backup_root)


def main() -> int:
    args = _parse_args()
    if not args.vault_path:
        raise SystemExit("No vault path configured.")
    vault_path = Path(args.vault_path).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()
    manifest_path = Path(args.manifest_path).expanduser().resolve()
    meetings_root = vault_path / "Meetings"

    if not vault_path.exists():
        raise SystemExit(f"Vault path not found: {vault_path}")
    if not db_path.exists():
        raise SystemExit(f"Database path not found: {db_path}")
    if not meetings_root.exists():
        raise SystemExit(f"Meetings folder not found: {meetings_root}")

    all_saved_rows = _load_all_saved_summary_rows(db_path)
    version_map = _compute_version_map(all_saved_rows)
    db_rows_by_path = {
        str(row["obsidian_relative_path"]).replace("\\", "/"): row for row in all_saved_rows
    }
    path_occupied_by_summary = {
        str(row["obsidian_relative_path"]).replace("\\", "/"): str(row["summary_id"]) for row in all_saved_rows
    }

    week_paths: list[LegacyWeekNote] = []
    skipped_parse: list[CaptureEntry] = []
    week_filter = {int(week) for week in args.week}
    for source in sorted(meetings_root.rglob("*.md")):
        relative = str(source.relative_to(vault_path)).replace("\\", "/")
        note = _parse_legacy_week_note(relative, expected_year=args.year)
        if note is None:
            continue
        if week_filter and note.week not in week_filter:
            continue
        week_paths.append(note)

    groups: dict[tuple[str, str], list[LegacyWeekNote]] = {}
    for note in week_paths:
        groups.setdefault(_group_key(note), []).append(note)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    entries: list[CaptureEntry] = []
    writes: dict[str, str] = {}
    db_updates: dict[str, str] = {}
    planned_paths: set[str] = set()
    try:
        for group_notes in groups.values():
            latest_note, group_reasons = _select_latest_note(group_notes)
            if latest_note is None:
                for note in group_notes:
                    entries.append(
                        CaptureEntry(
                            source_relative_path=note.relative_path,
                            destination_relative_path=None,
                            classification="skip",
                            body_mode="preserve_body",
                            property_mode="none",
                            status="skip",
                            skip_reasons=group_reasons,
                            derived_date=note.derived_date.isoformat(),
                            clean_title=note.clean_title,
                            promoted_to_latest=False,
                            inferred_version_number=note.version_number,
                            db_summary_id=None,
                            db_meeting_id=None,
                            source_sha256=file_sha256(vault_path / note.relative_path),
                        )
                    )
                continue

            for note in group_notes:
                source_path = vault_path / note.relative_path
                source_text = source_path.read_text(encoding="utf-8", errors="ignore")
                frontmatter, _body = parse_frontmatter_text(source_text)
                tags = list(extract_tags_from_frontmatter(source_path) or [])

                db_stub = db_rows_by_path.get(note.relative_path)
                detail_row: dict[str, Any] | None = None
                transcript = ""
                segments: list[Any] = []
                duration_minutes: int | None = None
                classification = "bucket_c"
                body_mode = "preserve_body"
                property_mode = "minimal_legacy"
                skip_reasons: list[str] = []

                if db_stub is not None:
                    detail_row = _load_summary_detail(conn, str(db_stub["summary_id"]))
                    segments = _load_segments_for_transcript(conn, detail_row.get("transcript_version_id"))
                    if segments:
                        transcript, _ = segments_to_transcript(segments)
                        duration_minutes = _segments_duration_minutes(segments)
                    can_modernize, reasons = _is_strict_rewrite_candidate(
                        detail_row=detail_row,
                        note_text=source_text,
                        transcript=transcript,
                        frontmatter=frontmatter,
                        source_relative_path=note.relative_path,
                    )
                    classification = "bucket_a" if can_modernize else "bucket_b"
                    body_mode = "modern_rebuild" if can_modernize else "preserve_body"
                    property_mode = "full_db" if can_modernize else "minimal_db"
                    skip_reasons.extend(reasons)

                promoted_to_latest = note.relative_path == latest_note.relative_path
                target_status = "latest" if promoted_to_latest else "archived"
                destination_relative = (
                    _latest_relative_path_for(note)
                    if promoted_to_latest
                    else _archive_relative_path_for(note, version_number=note.version_number)
                )

                destination_hash = _existing_hash(vault_path, destination_relative)
                source_hash = file_sha256(source_path)
                occupied_by = path_occupied_by_summary.get(destination_relative)
                same_summary = detail_row is not None and occupied_by == str(detail_row["summary_id"])

                if destination_hash and destination_hash == source_hash:
                    status = "already_exists_identical"
                    planned_paths.add(destination_relative)
                else:
                    status = "planned"
                    if destination_hash and promoted_to_latest and not same_summary:
                        destination_relative = _next_available_archive_path(
                            vault_path=vault_path,
                            note=note,
                            version_number=note.version_number,
                            preferred_suffix="promoted-latest",
                            planned_paths=planned_paths,
                        )
                        target_status = "archived"
                        promoted_to_latest = False
                    elif destination_hash and not same_summary:
                        destination_relative = _next_available_archive_path(
                            vault_path=vault_path,
                            note=note,
                            version_number=note.version_number,
                            planned_paths=planned_paths,
                        )
                    planned_paths.add(destination_relative)

                if detail_row is not None:
                    try:
                        frontmatter_updates = _build_db_frontmatter_updates(
                            detail_row=detail_row,
                            version_number=version_map[str(detail_row["summary_id"])],
                            export_status=target_status,
                            tags=tags,
                            duration_minutes=duration_minutes,
                        )
                    except ValueError as exc:
                        entries.append(
                            CaptureEntry(
                                source_relative_path=note.relative_path,
                                destination_relative_path=None,
                                classification="skip",
                                body_mode=body_mode,
                                property_mode=property_mode,
                                status="skip",
                                skip_reasons=[str(exc)],
                                derived_date=note.derived_date.isoformat(),
                                clean_title=note.clean_title,
                                promoted_to_latest=False,
                                inferred_version_number=note.version_number,
                                db_summary_id=str(detail_row["summary_id"]),
                                db_meeting_id=str(detail_row["meeting_id"]),
                                source_sha256=source_hash,
                            )
                        )
                        continue
                else:
                    frontmatter_updates = _build_legacy_frontmatter_updates(
                        note=note,
                        export_status=target_status,
                        tags=tags,
                    )

                if detail_row is not None and body_mode == "modern_rebuild":
                    exported_at_source = (
                        _parse_db_datetime(detail_row.get("saved_to_obsidian_at"))
                        or _parse_db_datetime(detail_row.get("summary_created_at"))
                    )
                    session_started_at = _parse_db_datetime(detail_row.get("session_started_at"))
                    if session_started_at is None or not segments:
                        classification = "bucket_b"
                        body_mode = "preserve_body"
                        property_mode = "minimal_db"
                        content = _merge_or_prepend_frontmatter(
                            source_text,
                            frontmatter_updates=frontmatter_updates,
                        )
                    else:
                        content = build_saved_summary_markdown(
                            meeting_id=str(detail_row["meeting_id"]),
                            summary_id=str(detail_row["summary_id"]),
                            transcript_version_id=detail_row.get("transcript_version_id"),
                            summary_version_number=version_map[str(detail_row["summary_id"])],
                            transcript_version_number=(
                                int(detail_row["transcript_version_number"])
                                if detail_row.get("transcript_version_number") is not None
                                else None
                            ),
                            meeting_title=detail_row.get("meeting_title"),
                            template_key=detail_row.get("template_key"),
                            template_label=detail_row.get("template_label"),
                            summary_content=detail_row.get("summary_content") or "",
                            processing_duration_seconds=detail_row.get("processing_duration_seconds"),
                            workflow_data_json=detail_row.get("workflow_data_json"),
                            pass1_system_prompt=detail_row.get("pass1_system_prompt"),
                            pass1_user_prompt=detail_row.get("pass1_user_prompt"),
                            pass2_system_prompt=detail_row.get("pass2_system_prompt"),
                            pass2_user_prompt=detail_row.get("pass2_user_prompt"),
                            session_started_at=session_started_at,
                            session_timezone_name=detail_row.get("timezone_name"),
                            session_timezone_offset_minutes=detail_row.get("timezone_offset_minutes"),
                            transcript_segments=segments,
                            tags=tags,
                            export_status=target_status,
                            exported_at_source=exported_at_source,
                        )
                else:
                    content = _merge_or_prepend_frontmatter(
                        source_text,
                        frontmatter_updates=frontmatter_updates,
                    )

                destination_content_hash = None
                if destination_hash is not None and destination_hash == file_sha256(source_path):
                    destination_content_hash = destination_hash
                elif status == "already_exists_identical":
                    destination_content_hash = destination_hash
                else:
                    writes[destination_relative] = content

                db_summary_id = str(detail_row["summary_id"]) if detail_row is not None else None
                db_meeting_id = str(detail_row["meeting_id"]) if detail_row is not None else None
                if db_summary_id and (
                    destination_relative not in path_occupied_by_summary or same_summary
                ):
                    db_updates[db_summary_id] = destination_relative

                entries.append(
                    CaptureEntry(
                        source_relative_path=note.relative_path,
                        destination_relative_path=destination_relative,
                        classification=classification,
                        body_mode=body_mode,
                        property_mode=property_mode,
                        status=status,
                        skip_reasons=skip_reasons,
                        derived_date=note.derived_date.isoformat(),
                        clean_title=note.clean_title,
                        promoted_to_latest=promoted_to_latest,
                        inferred_version_number=note.version_number,
                        db_summary_id=db_summary_id,
                        db_meeting_id=db_meeting_id,
                        source_sha256=source_hash,
                        destination_sha256=destination_content_hash,
                    )
                )
    finally:
        conn.close()

    entries.extend(skipped_parse)

    if args.limit > 0:
        allowed_paths = set()
        for entry in entries:
            if entry.status == "planned" and entry.destination_relative_path and len(allowed_paths) < args.limit:
                allowed_paths.add(entry.destination_relative_path)
        for entry in entries:
            if entry.status == "planned" and entry.destination_relative_path not in allowed_paths:
                entry.status = "skip"
                entry.skip_reasons = ["limit_filtered"]
                if entry.destination_relative_path in writes:
                    writes.pop(entry.destination_relative_path, None)
                if entry.db_summary_id:
                    db_updates.pop(entry.db_summary_id, None)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault_path": str(vault_path),
        "db_path": str(db_path),
        "year": args.year,
        "weeks": args.week,
        "apply_requested": bool(args.apply),
        "limit": args.limit or None,
        "counts": {
            "total_files": len(entries),
            "bucket_a": sum(1 for entry in entries if entry.classification == "bucket_a"),
            "bucket_b": sum(1 for entry in entries if entry.classification == "bucket_b"),
            "bucket_c": sum(1 for entry in entries if entry.classification == "bucket_c"),
            "planned": sum(1 for entry in entries if entry.status == "planned"),
            "already_exists_identical": sum(1 for entry in entries if entry.status == "already_exists_identical"),
            "skip": sum(1 for entry in entries if entry.status == "skip"),
        },
        "entries": [asdict(entry) for entry in entries],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.apply:
        print(f"Dry run complete. Manifest written to {manifest_path}")
        print(json.dumps(report["counts"], indent=2))
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_root = (
        Path(args.backup_dir).expanduser().resolve()
        if args.backup_dir
        else meetings_root.parent / f"Meetings__backup_{timestamp}"
    )
    _copy_backup(meetings_root, backup_root)

    for relative_path, content in writes.items():
        write_obsidian_markdown_atomic(content, relative_path, str(vault_path))

    if db_updates:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executemany(
                "UPDATE summaries SET obsidian_relative_path = ? WHERE id = ?",
                [(path, summary_id) for summary_id, path in db_updates.items()],
            )
            conn.commit()
        finally:
            conn.close()

    print(f"Backup created at {backup_root}")
    print(json.dumps(report["counts"], indent=2))
    print(f"Manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
