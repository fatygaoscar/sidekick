#!/usr/bin/env python3
"""Safely migrate existing Sidekick meeting notes into the new Obsidian layout.

Default mode is dry-run. `--apply` performs a copy-first migration and updates
`summaries.obsidian_relative_path` only after verifying copied hashes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.datetime_utils import localize_datetime
from src.core.obsidian_exports import (
    build_archive_relative_path,
    file_sha256,
    resolve_latest_export_target,
)


@dataclass
class MigrationEntry:
    summary_id: str | None
    meeting_id: str | None
    version_number: int | None
    kind: str
    old_relative_path: str
    new_relative_path: str | None
    status: str
    sha256: str | None
    note: str | None = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault-path",
        default="/mnt/c/Users/ozzfa/Documents/Obsidian Sync Vault",
        help="Absolute path to the Obsidian vault.",
    )
    parser.add_argument(
        "--db-path",
        default="data/sidekick.db",
        help="Path to Sidekick SQLite database.",
    )
    parser.add_argument(
        "--manifest-path",
        default="data/obsidian_migration_manifest.json",
        help="Where to write the JSON manifest/report.",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Optional explicit backup directory. Defaults to a sibling next to Meetings.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy files into the new layout and update DB paths after verification.",
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


def _load_summary_rows(db_path: Path) -> list[dict[str, Any]]:
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
                s.obsidian_relative_path AS obsidian_relative_path,
                m.title AS meeting_title,
                m.template_key AS template_key,
                sess.started_at AS session_started_at,
                sess.timezone_name AS timezone_name,
                sess.timezone_offset_minutes AS timezone_offset_minutes
            FROM summaries s
            JOIN meetings m ON m.id = s.meeting_id
            JOIN sessions sess ON sess.id = m.session_id
            WHERE s.status = 'saved'
              AND s.obsidian_relative_path IS NOT NULL
              AND TRIM(s.obsidian_relative_path) != ''
            ORDER BY s.meeting_id, s.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _compute_version_map(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_meeting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_meeting[str(row["meeting_id"])].append(row)
    version_map: dict[str, int] = {}
    for _meeting_id, meeting_rows in by_meeting.items():
        total = len(meeting_rows)
        for index, row in enumerate(meeting_rows):
            version_map[str(row["summary_id"])] = total - index
    return version_map


def _latest_exported_by_meeting(rows: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for row in rows:
        meeting_id = str(row["meeting_id"])
        latest.setdefault(meeting_id, str(row["summary_id"]))
    return latest


def _build_manifest(rows: list[dict[str, Any]], vault_path: Path) -> list[MigrationEntry]:
    version_map = _compute_version_map(rows)
    latest_exported = _latest_exported_by_meeting(rows)
    entries: list[MigrationEntry] = []
    referenced_paths: set[str] = set()

    for row in rows:
        old_relative = str(row["obsidian_relative_path"]).replace("\\", "/")
        referenced_paths.add(old_relative)
        source = vault_path / old_relative
        sha = file_sha256(source) if source.exists() else None
        started_at = _parse_db_datetime(row.get("session_started_at"))
        if started_at is None:
            entries.append(
                MigrationEntry(
                    summary_id=str(row["summary_id"]),
                    meeting_id=str(row["meeting_id"]),
                    version_number=version_map.get(str(row["summary_id"])),
                    kind="db_summary",
                    old_relative_path=old_relative,
                    new_relative_path=None,
                    status="unresolved",
                    sha256=sha,
                    note="Missing or invalid session.started_at in DB",
                )
            )
            continue
        local_started_at = localize_datetime(
            started_at,
            row.get("timezone_name"),
            row.get("timezone_offset_minutes"),
        )
        target = resolve_latest_export_target(
            vault_path=str(vault_path),
            meeting_id=str(row["meeting_id"]),
            title=row.get("meeting_title"),
            local_started_at=local_started_at,
            preferred_relative_path=(
                old_relative
                if str(row["summary_id"]) == latest_exported.get(str(row["meeting_id"]))
                else None
            ),
        )
        version_number = version_map.get(str(row["summary_id"]))
        if str(row["summary_id"]) == latest_exported.get(str(row["meeting_id"])):
            new_relative = target.relative_path
            status = "unchanged" if old_relative == new_relative else "migrate"
        elif version_number is not None:
            new_relative = build_archive_relative_path(target, version_number)
            status = "unchanged" if old_relative == new_relative else "migrate"
        else:
            new_relative = None
            status = "unresolved"
        entries.append(
            MigrationEntry(
                summary_id=str(row["summary_id"]),
                meeting_id=str(row["meeting_id"]),
                version_number=version_number,
                kind="db_summary",
                old_relative_path=old_relative,
                new_relative_path=new_relative,
                status=status,
                sha256=sha,
            )
        )

    for source in sorted((vault_path / "Meetings").rglob("*.md")):
        relative = str(source.relative_to(vault_path)).replace("\\", "/")
        if relative in referenced_paths:
            continue
        entries.append(
            MigrationEntry(
                summary_id=None,
                meeting_id=None,
                version_number=None,
                kind="untracked_file",
                old_relative_path=relative,
                new_relative_path=None,
                status="unresolved",
                sha256=file_sha256(source),
                note="No matching exported summary row in sidekick.db",
            )
        )
    return entries


def _copy_backup(meetings_root: Path, backup_root: Path) -> None:
    if backup_root.exists():
        raise RuntimeError(f"Backup path already exists: {backup_root}")
    shutil.copytree(meetings_root, backup_root)


def _apply_migration(
    *,
    manifest: list[MigrationEntry],
    vault_path: Path,
    db_path: Path,
) -> dict[str, Any]:
    copied: list[tuple[str, str]] = []
    db_updates: list[tuple[str, str]] = []

    for entry in manifest:
        if entry.status != "migrate" or not entry.new_relative_path:
            continue
        source = vault_path / entry.old_relative_path
        destination = vault_path / entry.new_relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if file_sha256(source) != file_sha256(destination):
            raise RuntimeError(
                f"Hash mismatch after copy: {entry.old_relative_path} -> {entry.new_relative_path}"
            )
        copied.append((entry.old_relative_path, entry.new_relative_path))
        if entry.summary_id:
            db_updates.append((entry.new_relative_path, entry.summary_id))

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "UPDATE summaries SET obsidian_relative_path = ? WHERE id = ?",
            db_updates,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "copied_count": len(copied),
        "db_updates": len(db_updates),
    }


def main() -> int:
    args = _parse_args()
    vault_path = Path(args.vault_path).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()
    meetings_root = vault_path / "Meetings"
    manifest_path = Path(args.manifest_path).expanduser().resolve()

    if not vault_path.exists():
        raise SystemExit(f"Vault path not found: {vault_path}")
    if not db_path.exists():
        raise SystemExit(f"Database path not found: {db_path}")
    if not meetings_root.exists():
        raise SystemExit(f"Meetings folder not found: {meetings_root}")

    rows = _load_summary_rows(db_path)
    manifest = _build_manifest(rows, vault_path)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault_path": str(vault_path),
        "db_path": str(db_path),
        "apply_requested": bool(args.apply),
        "entries": [asdict(entry) for entry in manifest],
        "counts": {
            "total": len(manifest),
            "migrate": sum(1 for entry in manifest if entry.status == "migrate"),
            "unchanged": sum(1 for entry in manifest if entry.status == "unchanged"),
            "unresolved": sum(1 for entry in manifest if entry.status == "unresolved"),
        },
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
    result = _apply_migration(
        manifest=manifest,
        vault_path=vault_path,
        db_path=db_path,
    )
    print(f"Backup created at {backup_root}")
    print(json.dumps(result, indent=2))
    print(f"Manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
