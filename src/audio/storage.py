"""Audio file storage helpers for recording sessions."""

import json
from pathlib import Path

from config.settings import get_settings


_KNOWN_AUDIO_EXTENSIONS = ("webm", "wav", "mp3", "ogg", "m4a")


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


def extension_from_content_type(content_type: str) -> str:
    """Infer extension from upload content-type."""
    lowered = (content_type or "").lower()
    if "audio/wav" in lowered or "audio/x-wav" in lowered:
        return "wav"
    if "audio/mpeg" in lowered or "audio/mp3" in lowered:
        return "mp3"
    if "audio/ogg" in lowered:
        return "ogg"
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
