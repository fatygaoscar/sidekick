"""Audio file storage helpers for recording sessions."""

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
    existing = [path for path in candidates if path.exists()]
    if existing:
        return existing
    return sorted(audio_dir.glob(f"{session_id}.*"))


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
