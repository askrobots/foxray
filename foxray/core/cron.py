"""Monitor schedules, as the SDKs send them in ``monitor_config``:
``{"type": "crontab", "value": "0 * * * *"}`` or
``{"type": "interval", "value": 2, "unit": "hour"}``, with an optional
IANA ``timezone``."""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from functools import lru_cache

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

from .util import UTC


class ScheduleError(ValueError):
    pass


MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
UNITS = ("minute", "hour", "day", "week", "month", "year")


def _value(token: str, names: dict, lo: int, hi: int) -> int:
    t = token.strip().lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise ScheduleError(f"bad crontab value: {token!r}")
    v = int(t)
    if not lo <= v <= hi:
        raise ScheduleError(f"crontab value {v} out of range {lo}-{hi}")
    return v


def _field(spec: str, lo: int, hi: int, names: dict | None = None) -> tuple[frozenset, bool]:
    names = names or {}
    out: set[int] = set()
    star = spec.strip() == "*"
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError("empty crontab field")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise ScheduleError(f"bad crontab step: {part!r}")
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _value(a, names, lo, hi), _value(b, names, lo, hi)
            if end < start:
                raise ScheduleError(f"bad crontab range: {part!r}")
        else:
            start = _value(rng, names, lo, hi)
            end = hi if step_s else start
        out.update(range(start, end + 1, step))
    return frozenset(out), star


class Crontab:
    def __init__(self, expr: str):
        expr = (expr or "").strip()
        expr = MACROS.get(expr.lower(), expr)
        parts = expr.split()
        if len(parts) != 5:
            raise ScheduleError("a crontab needs 5 fields")
        self.minutes, _ = _field(parts[0], 0, 59)
        self.hours, _ = _field(parts[1], 0, 23)
        self.doms, self.dom_star = _field(parts[2], 1, 31)
        self.months, _ = _field(parts[3], 1, 12, _MONTHS)
        dows, self.dow_star = _field(parts[4], 0, 7, _DAYS)
        self.dows = frozenset(0 if d == 7 else d for d in dows)

    def _day_ok(self, d: datetime) -> bool:
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # cron: either restricted day field may match

    def next_after(self, after: datetime, tz=UTC) -> datetime:
        """The first scheduled minute strictly after ``after``."""
        local = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        limit = local + timedelta(days=366 * 5)
        while local < limit:
            if local.month not in self.months:
                y, m = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
                local = local.replace(year=y, month=m, day=1, hour=0, minute=0)
                continue
            if not self._day_ok(local):
                local = (local + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if local.hour not in self.hours:
                local = (local + timedelta(hours=1)).replace(minute=0)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            return local.replace(tzinfo=tz).astimezone(UTC)
        raise ScheduleError("crontab never fires")


@lru_cache(maxsize=256)
def crontab(expr: str) -> Crontab:
    return Crontab(expr)


def zone(name: str | None):
    if not name or ZoneInfo is None:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def add_interval(dt: datetime, value: int, unit: str) -> datetime:
    if unit == "minute":
        return dt + timedelta(minutes=value)
    if unit == "hour":
        return dt + timedelta(hours=value)
    if unit == "day":
        return dt + timedelta(days=value)
    if unit == "week":
        return dt + timedelta(weeks=value)
    if unit in ("month", "year"):
        months = value * (12 if unit == "year" else 1)
        y, m = divmod(dt.month - 1 + months, 12)
        year, month = dt.year + y, m + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)
    raise ScheduleError(f"bad interval unit: {unit!r}")


def parse_schedule(config: dict) -> tuple[str, str, str]:
    """Validate an SDK ``monitor_config['schedule']``; returns (type, value, unit)."""
    if not isinstance(config, dict):
        raise ScheduleError("schedule must be an object")
    kind = config.get("type")
    if kind == "crontab":
        value = str(config.get("value") or "").strip()
        crontab(value)
        return "crontab", value, ""
    if kind == "interval":
        try:
            value = int(config.get("value"))
        except (TypeError, ValueError):
            raise ScheduleError("interval value must be a number") from None
        unit = str(config.get("unit") or "").lower().rstrip("s")
        if value <= 0 or unit not in UNITS:
            raise ScheduleError("interval needs a positive value and a unit")
        return "interval", str(value), unit
    raise ScheduleError("schedule type must be crontab or interval")


def next_expected(schedule_type: str, value: str, unit: str, tz_name: str, after: datetime) -> datetime | None:
    """When the next run should start, given the last check-in at ``after``."""
    if schedule_type == "crontab" and value:
        return crontab(value).next_after(after, zone(tz_name))
    if schedule_type == "interval" and value:
        return add_interval(after, int(value), unit)
    return None
