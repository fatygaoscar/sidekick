#!/usr/bin/env python3
"""Repair Sidekick Obsidian notes to use sidekick_export_status."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import get_settings
from src.core.obsidian_exports import (
    parse_frontmatter_text,
    rewrite_markdown_frontmatter,
    write_obsidian_markdown_atomic,
)


def _expected_status(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/")
    if "/_versions/" in normalized:
        return "archived"
    if normalized.startswith("Meetings/") and normalized.endswith(".md"):
        return "latest"
    return None


def _iter_sidekick_notes(vault_path: Path):
    meetings_root = vault_path / "Meetings"
    if not meetings_root.exists():
        return
    for note_path in meetings_root.rglob("*.md"):
        relative_path = str(note_path.relative_to(vault_path)).replace("\\", "/")
        frontmatter, _ = parse_frontmatter_text(
            note_path.read_text(encoding="utf-8", errors="ignore")
        )
        if frontmatter.get("sidekick_summary_id"):
            yield note_path, relative_path, frontmatter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair Sidekick Obsidian notes to use sidekick_export_status."
    )
    parser.add_argument(
        "--vault-path",
        default=get_settings().obsidian_vault_path,
        help="Absolute Obsidian vault path. Defaults to OBSIDIAN_VAULT_PATH.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite files in place. Without this flag, the script is dry-run only.",
    )
    args = parser.parse_args()

    if not args.vault_path:
        print("No vault path configured.", file=sys.stderr)
        return 1

    vault_path = Path(args.vault_path)
    if not vault_path.exists():
        print(f"Vault path does not exist: {vault_path}", file=sys.stderr)
        return 1

    updates: list[tuple[Path, str, str | None, str]] = []
    for note_path, relative_path, frontmatter in _iter_sidekick_notes(vault_path) or ():
        expected_status = _expected_status(relative_path)
        if not expected_status:
            continue
        current_status = frontmatter.get("sidekick_export_status")
        legacy_status = frontmatter.get("sidekick_is_latest_export")
        needs_update = (
            current_status != expected_status
            or legacy_status is not None
        )
        if needs_update:
            updates.append(
                (
                    note_path,
                    relative_path,
                    None if current_status is None else str(current_status),
                    expected_status,
                )
            )

    print(f"Found {len(updates)} Sidekick notes needing export-status repair.")
    for note_path, relative_path, current_status, expected_status in updates:
        print(
            f"- {relative_path}: "
            f"{current_status or 'missing'} -> {expected_status}"
        )
        if not args.apply:
            continue
        original = note_path.read_text(encoding="utf-8", errors="ignore")
        rewritten = rewrite_markdown_frontmatter(
            original,
            frontmatter_updates={"sidekick_export_status": expected_status},
            remove_frontmatter_keys=("sidekick_is_latest_export",),
        )
        write_obsidian_markdown_atomic(rewritten, relative_path, str(vault_path))

    print("Dry run only." if not args.apply else "Applied repairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
