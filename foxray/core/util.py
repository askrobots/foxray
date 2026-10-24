"""Small helpers shared by the core."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def day_str(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


def days_ago_str(now: datetime, days: int) -> str:
    return day_str(now - timedelta(days=days))


def parse_timestamp(value) -> datetime | None:
    """Sentry timestamps are either seconds since the epoch (int/float, or a
    numeric string) or RFC 3339 strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return datetime.fromtimestamp(float(s), UTC)
        except ValueError:
            pass
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        # Python 3.10's fromisoformat wants 0, 3 or 6 fraction digits.
        m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", s)
        if m:
            s = m.group(1) + "." + (m.group(2) + "000000")[:6] + m.group(3)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def to_utc_text(dt: datetime) -> str:
    """The text form used for datetimes in SQLite: naive UTC, the same shape
    Django writes, so both writers sort and compare alike."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def from_db_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return parse_timestamp(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def normalize_event_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace("-", "")
    return s if _HEX32.match(s) else None


def new_id() -> str:
    return uuid.uuid4().hex


def clip(value, limit: int) -> str | None:
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[: limit - 1] + "\u2026"
