"""Helpers for Obsidian export paths and file operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class ObsidianExportTarget:
    """Resolved target paths for one meeting export."""

    display_id: str
    meeting_year: int
    meeting_month: str
    meeting_iso_week: str
    resolved_stem: str
    filename: str
    relative_path: str


def sanitize_meeting_stem(title: str | None) -> str:
    """Return a filesystem-safe note stem."""
    raw = (title or "").strip() or "Untitled Recording"
    cleaned = "".join(ch for ch in raw if ch not in '<>:"/\\|?*').strip()
    return cleaned or "Untitled Recording"


def meeting_display_id(meeting_id: str | None) -> str:
    """Return a short human-safe meeting ID for note metadata."""
    compact = (meeting_id or "").strip() or "unknown"
    return f"SK-{compact[:8]}"


def meeting_month_folder(dt: datetime) -> str:
    """Return YYYY-MM folder label for the local meeting start."""
    return f"{dt.year:04d}-{dt.month:02d}"


def meeting_iso_week(dt: datetime) -> str:
    """Return ISO week label for metadata queries."""
    iso_year, week_num, _ = dt.isocalendar()
    return f"{iso_year}-W{week_num:02d}"


def is_new_meetings_layout_path(relative_path: str | None) -> bool:
    """Return True when a relative path already uses the new year/month latest-note layout."""
    if not relative_path:
        return False
    parts = Path(relative_path).parts
    if len(parts) != 4:
        return False
    if parts[0] != "Meetings":
        return False
    if parts[2] != meeting_month_folder(_parse_year_month(parts[2])):
        return False
    return parts[3].endswith(".md")


def _parse_year_month(label: str) -> datetime:
    year_str, month_str = label.split("-", 1)
    return datetime(int(year_str), int(month_str), 1)


def resolve_latest_export_target(
    *,
    vault_path: str | None,
    meeting_id: str | None,
    title: str | None,
    local_started_at: datetime,
    preferred_relative_path: str | None = None,
) -> ObsidianExportTarget:
    """Resolve the canonical latest-note path for a meeting export."""
    candidates = _candidate_latest_stems(title, local_started_at)
    if preferred_relative_path and is_new_meetings_layout_path(preferred_relative_path):
        preferred = Path(preferred_relative_path)
        if preferred.stem in candidates:
            return ObsidianExportTarget(
                display_id=meeting_display_id(meeting_id),
                meeting_year=local_started_at.year,
                meeting_month=meeting_month_folder(local_started_at),
                meeting_iso_week=meeting_iso_week(local_started_at),
                resolved_stem=preferred.stem,
                filename=preferred.name,
                relative_path=str(preferred).replace("\\", "/"),
            )

    root = Path(vault_path) if vault_path else None
    folder = Path("Meetings") / f"{local_started_at.year:04d}" / meeting_month_folder(local_started_at)
    chosen_stem = candidates[-1]
    for stem in candidates:
        rel = folder / f"{stem}.md"
        if root is None:
            chosen_stem = stem
            break
        if not (root / rel).exists():
            chosen_stem = stem
            break

    relative_path = str((folder / f"{chosen_stem}.md")).replace("\\", "/")
    return ObsidianExportTarget(
        display_id=meeting_display_id(meeting_id),
        meeting_year=local_started_at.year,
        meeting_month=meeting_month_folder(local_started_at),
        meeting_iso_week=meeting_iso_week(local_started_at),
        resolved_stem=chosen_stem,
        filename=f"{chosen_stem}.md",
        relative_path=relative_path,
    )


def _candidate_latest_stems(title: str | None, local_started_at: datetime) -> list[str]:
    base_stem = sanitize_meeting_stem(title)
    return [
        base_stem,
        f"{base_stem} - {local_started_at:%Y-%m-%d}",
        f"{base_stem} - {local_started_at:%Y-%m-%d %H%M}",
    ]


def build_archive_relative_path(target: ObsidianExportTarget, version_number: int) -> str:
    """Return the hidden archive path for an older exported summary."""
    folder = (
        Path("Meetings")
        / f"{target.meeting_year:04d}"
        / target.meeting_month
        / "_versions"
        / target.resolved_stem
    )
    return str((folder / f"v{version_number}.md")).replace("\\", "/")


def read_frontmatter(path: Path) -> dict[str, object]:
    """Parse a small YAML-like frontmatter block from the top of a markdown file."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return parse_frontmatter_text(handle.read())[0]
    except OSError:
        return {}


def parse_frontmatter_text(markdown_content: str) -> tuple[dict[str, object], str]:
    """Split markdown into parsed frontmatter and body text."""
    if not markdown_content.startswith("---\n"):
        return {}, markdown_content
    end_marker = markdown_content.find("\n---\n", 4)
    if end_marker == -1:
        return {}, markdown_content
    frontmatter_lines = markdown_content[4:end_marker].splitlines()
    body = markdown_content[end_marker + len("\n---\n") :]
    return _parse_frontmatter_lines(frontmatter_lines), body


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, object]:
    data: dict[str, object] = {}
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].rstrip("\n")
        if ":" not in stripped:
            idx += 1
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value == "":
            items: list[str] = []
            lookahead = idx + 1
            while lookahead < len(lines):
                candidate = lines[lookahead].rstrip("\n")
                if candidate.startswith("  - ") or candidate.startswith("- "):
                    item = candidate.split("- ", 1)[1].strip().strip('"').strip("'")
                    if item:
                        items.append(item)
                    lookahead += 1
                    continue
                if candidate.startswith(" ") or candidate.startswith("\t"):
                    lookahead += 1
                    continue
                break
            data[key] = items if items else ""
            idx = lookahead
            continue
        data[key] = _parse_frontmatter_value(value)
        idx += 1
    return data


def _parse_frontmatter_value(value: str) -> object:
    value = value.strip()
    if value == "":
        return ""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item.strip().strip('"').strip("'") for item in inner.split(",")]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return value.strip('"').strip("'")


def extract_tags_from_frontmatter(path: Path) -> list[str] | None:
    """Return normalized tags from top-level frontmatter, if present."""
    frontmatter = read_frontmatter(path)
    raw_tags = frontmatter.get("tags")
    if raw_tags is None:
        return None
    if isinstance(raw_tags, list):
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    if isinstance(raw_tags, str):
        stripped = raw_tags.strip()
        if not stripped:
            return []
        return [stripped]
    return None


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def build_frontmatter_text(frontmatter: dict[str, object]) -> str:
    """Render a frontmatter dictionary back to YAML-like text."""
    if not frontmatter:
        return ""
    lines = ["---"]
    for key, value in frontmatter.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def rewrite_markdown_frontmatter(
    markdown_content: str,
    *,
    frontmatter_updates: dict[str, object] | None = None,
    remove_frontmatter_keys: tuple[str, ...] = (),
) -> str:
    """Rewrite top-level frontmatter while preserving the markdown body."""
    frontmatter, body = parse_frontmatter_text(markdown_content)
    if not frontmatter:
        return markdown_content
    updated = dict(frontmatter)
    for key in remove_frontmatter_keys:
        updated.pop(key, None)
    if frontmatter_updates:
        updated.update(frontmatter_updates)
    frontmatter_text = build_frontmatter_text(updated)
    return f"{frontmatter_text}\n{body}" if body else f"{frontmatter_text}\n"


def find_note_by_summary_id(
    *,
    obsidian_vault_path: str,
    summary_id: str,
) -> str | None:
    """Return the relative path of a note with matching sidekick_summary_id, if unambiguous."""
    if not summary_id:
        return None
    vault_path = Path(obsidian_vault_path)
    matches: list[Path] = []
    for candidate in vault_path.rglob("*.md"):
        frontmatter = read_frontmatter(candidate)
        if frontmatter.get("sidekick_summary_id") == summary_id:
            matches.append(candidate)
            if len(matches) > 1:
                return None
    if len(matches) != 1:
        return None
    return str(matches[0].relative_to(vault_path)).replace("\\", "/")


def write_obsidian_markdown_atomic(
    markdown_content: str,
    relative_path: str,
    obsidian_vault_path: str,
) -> tuple[str, str]:
    """Write markdown to the vault atomically and return absolute path plus obsidian URI."""
    vault_path = Path(obsidian_vault_path)
    filepath = vault_path / Path(relative_path)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = filepath.with_name(f".{filepath.name}.{uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(markdown_content)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(filepath)

    vault_name = vault_path.name
    uri_path = relative_path.replace("\\", "/")
    obsidian_uri = (
        f"obsidian://open?"
        f"vault={urllib.parse.quote(vault_name)}&"
        f"file={urllib.parse.quote(uri_path)}"
    )
    return str(filepath), obsidian_uri


def copy_obsidian_file(
    *,
    obsidian_vault_path: str,
    source_relative_path: str,
    destination_relative_path: str,
) -> bool:
    """Copy one vault file to another relative path and verify the copied bytes."""
    vault_path = Path(obsidian_vault_path)
    source = vault_path / Path(source_relative_path)
    destination = vault_path / Path(destination_relative_path)
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return file_sha256(source) == file_sha256(destination)


def copy_obsidian_markdown_with_frontmatter_updates(
    *,
    obsidian_vault_path: str,
    source_relative_path: str,
    destination_relative_path: str,
    frontmatter_updates: dict[str, object] | None = None,
    remove_frontmatter_keys: tuple[str, ...] = (),
) -> bool:
    """Copy a markdown note while rewriting selected frontmatter fields."""
    vault_path = Path(obsidian_vault_path)
    source = vault_path / Path(source_relative_path)
    if not source.exists():
        return False
    source_text = source.read_text(encoding="utf-8", errors="ignore")
    rewritten = rewrite_markdown_frontmatter(
        source_text,
        frontmatter_updates=frontmatter_updates,
        remove_frontmatter_keys=remove_frontmatter_keys,
    )
    destination_path = vault_path / Path(destination_relative_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_relative_path = str(Path(destination_relative_path)).replace("\\", "/")
    write_obsidian_markdown_atomic(rewritten, tmp_relative_path, obsidian_vault_path)
    return destination_path.exists()


def file_sha256(path: Path) -> str:
    """Return SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
