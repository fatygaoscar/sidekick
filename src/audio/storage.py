"""Audio file storage helpers for recording sessions."""

import json
import shutil
from pathlib import Path

from config.settings import get_settings


_KNOWN_AUDIO_EXTENSIONS = ("webm", "wav", "mp3", "ogg", "m4a", "mp4")


def normalize_audio_extension(value: str | None, default: str = "webm") -> str:
    """Normalize a user- or browser-provided audio extension."""
    normalized = str(value or "").strip().lower().lstrip(".")
    if normalized in _KNOWN_AUDIO_EXTENSIONS:
        return normalized
    return default


# ---------------------------------------------------------------------------
# Chunk storage with client isolation (idempotent, order-independent)
# ---------------------------------------------------------------------------


def get_chunk_storage_dir(session_id: str, client_id: str | None = None) -> Path:
    """Directory for storing individual chunks, namespaced by client.

    Structure: data/audio/chunks/{session_id}/{client_id}/
    If client_id is None, returns the session-level chunk directory.
    """
    base = get_settings().data_dir / "audio" / "chunks" / session_id
    if client_id:
        return base / client_id
    return base


def write_chunk(session_id: str, client_id: str, chunk_index: int, data: bytes) -> Path:
    """Write a single chunk (idempotent - skip if exists with same size).

    Returns the path to the chunk file.
    """
    chunk_dir = get_chunk_storage_dir(session_id, client_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    chunk_path = chunk_dir / f"{chunk_index:06d}.chunk"

    # Idempotent: skip if file exists with same size
    if chunk_path.exists() and chunk_path.stat().st_size == len(data):
        return chunk_path

    # Write atomically via temp file
    temp_path = chunk_path.with_suffix(".tmp")
    temp_path.write_bytes(data)
    temp_path.replace(chunk_path)

    return chunk_path


def get_available_chunks(session_id: str, client_id: str) -> list[tuple[int, Path]]:
    """Return sorted list of (index, path) for available chunks from a client."""
    chunk_dir = get_chunk_storage_dir(session_id, client_id)
    if not chunk_dir.exists():
        return []

    chunks = []
    for path in chunk_dir.glob("*.chunk"):
        try:
            index = int(path.stem)
            chunks.append((index, path))
        except ValueError:
            continue

    return sorted(chunks, key=lambda x: x[0])


def assemble_chunks(
    session_id: str,
    client_id: str,
    expected_count: int,
    extension: str,
) -> Path | None:
    """Assemble chunks from a specific client into final audio file.

    Returns the final audio path if successful, None if chunks are incomplete.
    """
    chunks = get_available_chunks(session_id, client_id)

    if not chunks:
        return None

    # Check for completeness (indices 0 through expected_count-1)
    chunk_indices = {idx for idx, _ in chunks}
    expected_indices = set(range(expected_count))
    if chunk_indices != expected_indices:
        return None

    # Assemble chunks in order
    audio_dir = get_audio_dir()
    final_path = audio_dir / f"{session_id}.{extension}"

    # Write to temp file first for atomicity
    temp_path = final_path.with_suffix(".assembling")
    with temp_path.open("wb") as out:
        for _, chunk_path in chunks:
            out.write(chunk_path.read_bytes())

    temp_path.replace(final_path)

    return final_path


def get_missing_chunk_indices(session_id: str, client_id: str, expected_count: int) -> list[int]:
    """Return list of missing chunk indices for a client."""
    chunks = get_available_chunks(session_id, client_id)
    chunk_indices = {idx for idx, _ in chunks}
    expected_indices = set(range(expected_count))
    return sorted(expected_indices - chunk_indices)


def get_chunk_upload_summary(
    session_id: str,
    client_id: str,
    expected_count: int | None = None,
) -> dict:
    """Summarize the chunk upload state for a specific client."""
    chunks = get_available_chunks(session_id, client_id)
    available_indices = [idx for idx, _ in chunks]
    highest_index = max(available_indices) if available_indices else None
    chunk_indices = set(available_indices)

    expected = int(expected_count) if expected_count and expected_count > 0 else None
    contiguous_expected = set(range((highest_index or -1) + 1))
    is_contiguous = bool(available_indices) and chunk_indices == contiguous_expected
    missing_indices = (
        sorted(set(range(expected)) - chunk_indices)
        if expected is not None
        else []
    )

    return {
        "client_id": client_id,
        "available_indices": available_indices,
        "available_count": len(available_indices),
        "highest_index": highest_index,
        "expected_count": expected,
        "missing_indices": missing_indices,
        "is_contiguous": is_contiguous,
        "is_complete": expected is not None and not missing_indices and len(available_indices) == expected,
    }


def cleanup_chunk_storage(session_id: str, client_id: str | None = None) -> None:
    """Remove chunk storage. If client_id is None, removes all clients."""
    if client_id:
        chunk_dir = get_chunk_storage_dir(session_id, client_id)
        if chunk_dir.exists():
            shutil.rmtree(chunk_dir)
        # Also remove session dir if now empty
        session_dir = get_chunk_storage_dir(session_id)
        if session_dir.exists() and not any(session_dir.iterdir()):
            session_dir.rmdir()
    else:
        session_dir = get_chunk_storage_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)


def get_audio_dir() -> Path:
    """Directory where session audio files are stored."""
    path = get_settings().data_dir / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_audio_candidates(session_id: str) -> list[Path]:
    """Possible audio files for a session (known extensions first)."""
    audio_dir = get_audio_dir()
    candidates = [audio_dir / f"{session_id}.{ext}" for ext in _KNOWN_AUDIO_EXTENSIONS]
    return [path for path in candidates if path.exists()]


def get_session_audio_path(session_id: str) -> Path | None:
    """Get stored audio path for a session, if present."""
    candidates = get_session_audio_candidates(session_id)
    return candidates[0] if candidates else None


def ensure_session_audio_path(
    session_id: str,
    preferred_extension: str | None = None,
) -> Path | None:
    """Return finalized audio path, recovering from chunked partial uploads when possible.

    Tries recovery in order:
    1. Check for existing finalized audio file
    2. Try to recover from old .part file (legacy sequential append)
    3. Check chunk storage for any client's chunks (new idempotent storage)
    """
    existing = get_session_audio_path(session_id)
    if existing:
        return existing

    # Try legacy .part file recovery
    part_path = get_session_partial_audio_path(session_id)
    if part_path.exists() and part_path.stat().st_size > 0:
        meta = read_session_chunk_meta(session_id) or {}
        extension = str(meta.get("extension") or preferred_extension or "webm").strip(". ").lower()
        if extension not in _KNOWN_AUDIO_EXTENSIONS:
            extension = "webm"

        final_path = get_audio_dir() / f"{session_id}.{extension}"
        try:
            part_path.replace(final_path)
            chunk_meta_path = get_session_chunk_meta_path(session_id)
            if chunk_meta_path.exists():
                chunk_meta_path.unlink()
            return final_path if final_path.exists() else None
        except Exception:
            pass

    return recover_session_audio_from_chunks(
        session_id,
        preferred_extension=preferred_extension,
    )


def recover_session_audio_from_chunks(
    session_id: str,
    client_id: str | None = None,
    expected_count: int | None = None,
    preferred_extension: str | None = None,
) -> Path | None:
    """Recover finalized audio from retained chunks if a complete set exists.

    If `expected_count` is not provided, recovery falls back to the legacy
    best-effort behavior of treating a contiguous chunk set from 0..max_index as
    complete. That path exists for backward compatibility with older recordings
    that predate explicit expected chunk counts.
    """
    existing = get_session_audio_path(session_id)
    if existing:
        return existing

    meta = read_session_chunk_meta(session_id) or {}
    session_chunk_dir = get_chunk_storage_dir(session_id)
    if not session_chunk_dir.exists():
        return None

    preferred_clients: list[str] = []
    requested_client = str(client_id or "").strip()
    meta_client = str(meta.get("client_id") or "").strip()
    if requested_client:
        preferred_clients.append(requested_client)
    if meta_client and meta_client not in preferred_clients:
        preferred_clients.append(meta_client)

    for client_dir in sorted(session_chunk_dir.iterdir()):
        if client_dir.is_dir() and client_dir.name not in preferred_clients:
            preferred_clients.append(client_dir.name)

    extension = str(
        meta.get("extension")
        or preferred_extension
        or "webm"
    ).strip(". ").lower()
    if extension not in _KNOWN_AUDIO_EXTENSIONS:
        extension = "webm"

    meta_expected = meta.get("expected_chunks")
    try:
        parsed_expected = int(expected_count) if expected_count is not None else None
    except (TypeError, ValueError):
        parsed_expected = None
    try:
        parsed_meta_expected = int(meta_expected) if meta_expected is not None else None
    except (TypeError, ValueError):
        parsed_meta_expected = None

    resolved_expected = (
        parsed_expected
        if parsed_expected and parsed_expected > 0
        else parsed_meta_expected
        if parsed_meta_expected and parsed_meta_expected > 0
        else None
    )

    for candidate_client_id in preferred_clients:
        summary = get_chunk_upload_summary(
            session_id,
            candidate_client_id,
            resolved_expected,
        )
        if summary["available_count"] == 0:
            continue

        assembly_count = summary["expected_count"]
        if assembly_count is None:
            if not summary["is_contiguous"] or summary["highest_index"] is None:
                continue
            assembly_count = int(summary["highest_index"]) + 1
        elif not summary["is_complete"]:
            continue

        result = assemble_chunks(
            session_id,
            candidate_client_id,
            assembly_count,
            extension,
        )
        if result:
            return result

    return None


def extension_from_content_type(content_type: str) -> str:
    """Infer extension from upload content-type."""
    lowered = (content_type or "").lower()
    if "audio/wav" in lowered or "audio/x-wav" in lowered:
        return "wav"
    if "audio/mpeg" in lowered or "audio/mp3" in lowered:
        return "mp3"
    if "audio/ogg" in lowered:
        return "ogg"
    if "video/mp4" in lowered:
        return "mp4"
    if "audio/mp4" in lowered or "audio/m4a" in lowered:
        return "m4a"
    return "webm"


def media_type_for_path(path: Path) -> str:
    """Return HTTP media type for an audio file path."""
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".mp4":
        return "audio/mp4"
    return "audio/webm"


def get_session_partial_audio_path(session_id: str) -> Path:
    """Get temporary append-only partial file path for session audio."""
    return get_audio_dir() / f"{session_id}.part"


def get_session_chunk_meta_path(session_id: str) -> Path:
    """Get metadata file path for chunked uploads."""
    return get_audio_dir() / f"{session_id}.chunks.json"


def read_session_chunk_meta(session_id: str) -> dict | None:
    """Read chunk upload metadata for a session, if any."""
    path = get_session_chunk_meta_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_session_chunk_meta(session_id: str, payload: dict) -> None:
    """Write chunk upload metadata atomically."""
    path = get_session_chunk_meta_path(session_id)
    path.write_text(json.dumps(payload), encoding="utf-8")


def clear_session_chunk_upload_state(session_id: str) -> None:
    """Delete temporary chunk upload files for a session."""
    for path in (get_session_partial_audio_path(session_id), get_session_chunk_meta_path(session_id)):
        if path.exists():
            path.unlink()
