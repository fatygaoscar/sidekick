#!/usr/bin/env python3
"""Conservatively rewrite untouched Sidekick meeting notes into the current export format."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings
from src.core.markdown_utils import _sanitize_prompt_for_note
from src.core.obsidian_exports import (
    extract_tags_from_frontmatter,
    file_sha256,
    parse_frontmatter_text,
    write_obsidian_markdown_atomic,
)
from src.core.obsidian_rewrite import build_saved_summary_markdown, segments_to_transcript


ALLOWED_FRONTMATTER_KEYS = {
    "type",
    "sidekick_meeting_id",
    "sidekick_summary_id",
    "sidekick_transcript_version_id",
    "sidekick_summary_version",
    "sidekick_transcript_version",
    "sidekick_is_latest_export",
    "sidekick_export_status",
    "sidekick_display_id",
    "meeting_title",
    "meeting_date",
    "meeting_started_at",
    "meeting_year",
    "meeting_month",
    "meeting_iso_week",
    "template_key",
    "recording_duration_minutes",
    "aliases",
    "tags",
    "exported_at",
}


@dataclass
class RewriteEntry:
    summary_id: str
    meeting_id: str
    relative_path: str
    status: str
    skip_reasons: list[str]
    expected_export_status: str
    summary_content_match: bool
    prompt_audit_match: bool
    transcript_match: bool
    frontmatter_match: bool
    path_match: bool
    preserved_tags: list[str]
    file_sha256: str | None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-path",
        default=get_settings().obsidian_vault_path,
        help="Absolute Obsidian vault path. Defaults to OBSIDIAN_VAULT_PATH.",
    )
    parser.add_argument(
        "--db-path",
        default="data/sidekick.db",
        help="Path to Sidekick SQLite database.",
    )
    parser.add_argument(
        "--manifest-path",
        default="data/obsidian_rewrite_manifest.json",
        help="Where to write the JSON manifest/report.",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Optional explicit backup directory. Defaults to a sibling next to Meetings.",
    )
    parser.add_argument("--meeting-id", default="", help="Limit to one meeting ID.")
    parser.add_argument("--summary-id", default="", help="Limit to one summary ID.")
    parser.add_argument("--limit", type=int, default=0, help="Max number of rewrites to apply.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite eligible notes after creating a backup. Default is dry run only.",
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


def _expected_export_status(relative_path: str) -> str:
    return "archived" if "/_versions/" in relative_path.replace("\\", "/") else "latest"


def _load_summary_rows(
    db_path: Path,
    *,
    meeting_id: str | None = None,
    summary_id: str | None = None,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT
                s.id AS summary_id,
                s.meeting_id AS meeting_id,
                s.created_at AS summary_created_at,
                s.saved_to_obsidian_at AS saved_to_obsidian_at,
                s.obsidian_relative_path AS obsidian_relative_path,
                m.title AS meeting_title
            FROM summaries s
            JOIN meetings m ON m.id = s.meeting_id
            WHERE s.status = 'saved'
              AND s.obsidian_relative_path IS NOT NULL
              AND TRIM(s.obsidian_relative_path) != ''
        """
        params: list[Any] = []
        if meeting_id:
            query += " AND s.meeting_id = ?"
            params.append(meeting_id)
        if summary_id:
            query += " AND s.id = ?"
            params.append(summary_id)
        query += " ORDER BY s.meeting_id, s.created_at DESC"
        rows = conn.execute(query, params).fetchall()
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
        raise RuntimeError(f"Summary {summary_id} not found during rewrite detail load")
    return dict(row)


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


def _load_segments_for_transcript(
    conn: sqlite3.Connection,
    transcript_version_id: str | None,
) -> list[Any]:
    if not transcript_version_id:
        return []
    rows = conn.execute(
        """
        SELECT text, start_time, end_time, is_important, speaker, speaker_cluster
        FROM transcript_segments
        WHERE transcript_version_id = ?
        ORDER BY start_time ASC, id ASC
        """,
        (transcript_version_id,),
    ).fetchall()
    return rows


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


def _frontmatter_is_safe(frontmatter: dict[str, object]) -> bool:
    return set(frontmatter).issubset(ALLOWED_FRONTMATTER_KEYS)


def _evaluate_note(
    *,
    row: dict[str, Any],
    relative_path: str,
    note_text: str,
    frontmatter: dict[str, object],
    transcript: str,
    expected_markdown: str,
) -> RewriteEntry:
    skip_reasons: list[str] = []
    normalized_note = _normalized(note_text)
    payload_note = _payload_normalized_note(note_text)
    summary_content_match = _normalized(str(row.get("summary_content") or "")) in _normalized(payload_note)
    prompt_audit_match = _matches_prompt_payload(payload_note, row)
    transcript_match = _matches_transcript_payload(payload_note, transcript)
    path_match = relative_path == str(row.get("obsidian_relative_path") or "").replace("\\", "/")
    frontmatter_match = True

    if not relative_path.startswith("Meetings/"):
        skip_reasons.append("outside_meetings_folder")
    if not path_match:
        skip_reasons.append("path_mismatch_or_relocated")
    if frontmatter:
        if frontmatter.get("sidekick_summary_id") not in (None, "", str(row["summary_id"])):
            frontmatter_match = False
            skip_reasons.append("frontmatter_summary_id_mismatch")
        if not _frontmatter_is_safe(frontmatter):
            frontmatter_match = False
            skip_reasons.append("unexpected_frontmatter_keys")
    if not summary_content_match:
        skip_reasons.append("summary_content_mismatch")
    if not prompt_audit_match:
        skip_reasons.append("prompt_audit_mismatch")
    if not transcript_match:
        skip_reasons.append("transcript_mismatch")
    if normalized_note == _normalized(expected_markdown):
        skip_reasons.append("already_modern")

    status = "rewrite" if not skip_reasons else "skip"
    return RewriteEntry(
        summary_id=str(row["summary_id"]),
        meeting_id=str(row["meeting_id"]),
        relative_path=relative_path,
        status=status,
        skip_reasons=skip_reasons,
        expected_export_status=_expected_export_status(relative_path),
        summary_content_match=summary_content_match,
        prompt_audit_match=prompt_audit_match,
        transcript_match=transcript_match,
        frontmatter_match=frontmatter_match,
        path_match=path_match,
        preserved_tags=[],
        file_sha256=None,
    )


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

    rows = _load_summary_rows(
        db_path,
        meeting_id=args.meeting_id or None,
        summary_id=args.summary_id or None,
    )
    version_map = _compute_version_map(rows)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    entries: list[RewriteEntry] = []
    rewrites: dict[str, str] = {}
    try:
        for row in rows:
            detail_row = _load_summary_detail(conn, str(row["summary_id"]))
            relative_path = str(row["obsidian_relative_path"]).replace("\\", "/")
            note_path = vault_path / relative_path
            if not note_path.exists():
                entries.append(
                    RewriteEntry(
                        summary_id=str(row["summary_id"]),
                        meeting_id=str(row["meeting_id"]),
                        relative_path=relative_path,
                        status="skip",
                        skip_reasons=["file_missing"],
                        expected_export_status=_expected_export_status(relative_path),
                        summary_content_match=False,
                        prompt_audit_match=False,
                        transcript_match=False,
                        frontmatter_match=False,
                        path_match=False,
                        preserved_tags=[],
                        file_sha256=None,
                    )
                )
                continue

            note_text = note_path.read_text(encoding="utf-8", errors="ignore")
            frontmatter, _body = parse_frontmatter_text(note_text)
            preserved_tags = list(extract_tags_from_frontmatter(note_path) or [])
            segments = _load_segments_for_transcript(conn, detail_row.get("transcript_version_id"))
            transcript, _duration = ("", 0.0)
            if segments:
                transcript, _duration = segments_to_transcript(segments)

            exported_at_source = (
                _parse_db_datetime(detail_row.get("saved_to_obsidian_at"))
                or _parse_db_datetime(detail_row.get("summary_created_at"))
            )
            session_started_at = _parse_db_datetime(detail_row.get("session_started_at"))
            if not session_started_at:
                entries.append(
                    RewriteEntry(
                        summary_id=str(detail_row["summary_id"]),
                        meeting_id=str(detail_row["meeting_id"]),
                        relative_path=relative_path,
                        status="skip",
                        skip_reasons=["missing_session_started_at"],
                        expected_export_status=_expected_export_status(relative_path),
                        summary_content_match=False,
                        prompt_audit_match=False,
                        transcript_match=False,
                        frontmatter_match=False,
                        path_match=False,
                        preserved_tags=preserved_tags,
                        file_sha256=file_sha256(note_path),
                    )
                )
                continue

            try:
                expected_markdown = build_saved_summary_markdown(
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
                    tags=preserved_tags,
                    export_status=_expected_export_status(relative_path),
                    exported_at_source=exported_at_source,
                )
            except ValueError as exc:
                entries.append(
                    RewriteEntry(
                        summary_id=str(detail_row["summary_id"]),
                        meeting_id=str(detail_row["meeting_id"]),
                        relative_path=relative_path,
                        status="skip",
                        skip_reasons=[str(exc).replace(" ", "_").lower()],
                        expected_export_status=_expected_export_status(relative_path),
                        summary_content_match=False,
                        prompt_audit_match=False,
                        transcript_match=False,
                        frontmatter_match=False,
                        path_match=True,
                        preserved_tags=preserved_tags,
                        file_sha256=file_sha256(note_path),
                    )
                )
                continue

            entry = _evaluate_note(
                row=detail_row,
                relative_path=relative_path,
                note_text=note_text,
                frontmatter=frontmatter,
                transcript=transcript,
                expected_markdown=expected_markdown,
            )
            entry.preserved_tags = preserved_tags
            entry.file_sha256 = file_sha256(note_path)
            entries.append(entry)
            if entry.status == "rewrite":
                rewrites[relative_path] = expected_markdown
    finally:
        conn.close()

    if args.limit > 0:
        allowed = set()
        for entry in entries:
            if entry.status == "rewrite" and len(allowed) < args.limit:
                allowed.add(entry.relative_path)
        for entry in entries:
            if entry.status == "rewrite" and entry.relative_path not in allowed:
                entry.status = "skip"
                entry.skip_reasons = ["limit_filtered"]
        rewrites = {path: content for path, content in rewrites.items() if path in allowed}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault_path": str(vault_path),
        "db_path": str(db_path),
        "apply_requested": bool(args.apply),
        "meeting_id": args.meeting_id or None,
        "summary_id": args.summary_id or None,
        "limit": args.limit or None,
        "counts": {
            "total": len(entries),
            "rewrite": sum(1 for entry in entries if entry.status == "rewrite"),
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
    for relative_path, markdown in rewrites.items():
        write_obsidian_markdown_atomic(markdown, relative_path, str(vault_path))
    print(f"Backup created at {backup_root}")
    print(json.dumps(report["counts"], indent=2))
    print(f"Manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
