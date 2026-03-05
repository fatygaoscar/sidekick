"""Datetime helpers for consistent UTC storage + local timezone presentation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def to_utc_iso(dt: datetime | None) -> str | None:
    """Return an ISO8601 UTC timestamp with trailing Z."""
    if dt is None:
        return None
    utc_dt = _coerce_utc(dt)
    return utc_dt.isoformat().replace("+00:00", "Z")


def get_tzinfo(timezone_name: str | None, timezone_offset_minutes: int | None) -> tzinfo:
    """Resolve timezone from IANA name first, then numeric offset, then UTC."""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass

    if timezone_offset_minutes is not None:
        # JS getTimezoneOffset() returns UTC-local in minutes.
        return timezone(-timedelta(minutes=int(timezone_offset_minutes)))

    return timezone.utc


def localize_datetime(
    dt: datetime,
    timezone_name: str | None,
    timezone_offset_minutes: int | None,
) -> datetime:
    """Convert UTC/naive UTC datetime to the provided local timezone."""
    return _coerce_utc(dt).astimezone(get_tzinfo(timezone_name, timezone_offset_minutes))


def timezone_label(timezone_name: str | None, timezone_offset_minutes: int | None) -> str:
    """Human label for timezone metadata."""
    if timezone_name:
        return timezone_name

    if timezone_offset_minutes is None:
        return "UTC"

    total_minutes = -int(timezone_offset_minutes)
    sign = "+" if total_minutes >= 0 else "-"
    abs_minutes = abs(total_minutes)
    hours, minutes = divmod(abs_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _coerce_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
