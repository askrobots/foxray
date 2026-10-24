"""Shaping stored data for people: used by the standalone UI and the Django
admin pages, so both show the same thing."""

from __future__ import annotations

import posixpath
from datetime import datetime, timedelta

from .util import UTC, utcnow


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    now = now or utcnow()
    secs = (now - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            n = int(secs // size)
            return f"in {n}{unit}" if future else f"{n}{unit} ago"
    return "just now" if not future else "in a moment"


def when(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if dt else "-"


def frame_lines(frame: dict) -> list[tuple[int | None, str, bool]]:
    """(line number, text, is the line that ran) around a frame."""
    lineno = frame.get("lineno")
    pre = frame.get("pre_context") or []
    post = frame.get("post_context") or []
    if "context_line" not in frame:
        return []
    out = []
    start = (lineno - len(pre)) if isinstance(lineno, int) else None
    for i, text in enumerate(pre):
        out.append((None if start is None else start + i, text, False))
    out.append((lineno, frame.get("context_line") or "", True))
    for i, text in enumerate(post):
        out.append((None if lineno is None else lineno + 1 + i, text, False))
    return out


def frame_label(frame: dict) -> tuple[str, str]:
    where = frame.get("module") or frame.get("filename") or posixpath.basename(frame.get("abs_path") or "") or "?"
    path = frame.get("abs_path") or frame.get("filename") or ""
    line = f":{frame['lineno']}" if frame.get("lineno") else ""
    return f"{where} in {frame.get('function') or '?'}", f"{path}{line}"


def exceptions_for_display(ev: dict) -> list[dict]:
    """The chain with the exception that was raised last first, and each
    stack with the most recent call first."""
    out = []
    for exc in reversed((ev.get("exception") or {}).get("values") or []):
        frames = list(reversed((exc.get("stacktrace") or {}).get("frames") or []))
        out.append({
            "type": exc.get("type") or "Error",
            "value": exc.get("value") or "",
            "module": exc.get("module") or "",
            "mechanism": exc.get("mechanism") or {},
            "frames": [{"label": frame_label(f)[0], "path": frame_label(f)[1], "in_app": bool(f.get("in_app")),
                        "lines": frame_lines(f), "vars": f.get("vars") or {}} for f in frames],
            "app_frames": sum(1 for f in frames if f.get("in_app")),
        })
    st = ev.get("stacktrace")
    if not out and st:
        frames = list(reversed(st.get("frames") or []))
        out.append({"type": "Stack", "value": "", "module": "", "mechanism": {},
                    "frames": [{"label": frame_label(f)[0], "path": frame_label(f)[1], "in_app": bool(f.get("in_app")),
                                "lines": frame_lines(f), "vars": f.get("vars") or {}} for f in frames],
                    "app_frames": sum(1 for f in frames if f.get("in_app"))})
    return out


def day_bars(days: list[tuple[str, int]], now: datetime | None = None, span: int = 30) -> list[dict]:
    """The last ``span`` days, oldest first, with a height in percent."""
    now = now or utcnow()
    counts = dict(days)
    out = []
    for i in range(span - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"day": d, "n": counts.get(d, 0)})
    top = max([b["n"] for b in out] + [1])
    for b in out:
        b["pct"] = 0 if not b["n"] else max(4, round(100 * b["n"] / top))
    return out


def schedule_text(monitor) -> str:
    if monitor.schedule_type == "crontab":
        tz = f" ({monitor.timezone})" if monitor.timezone else ""
        return f"cron {monitor.schedule_value}{tz}"
    if monitor.schedule_type == "interval":
        n = int(monitor.schedule_value or 0)
        return f"every {n} {monitor.schedule_unit}{'s' if n != 1 else ''}"
    return "no schedule"


def kv_rows(value, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a small dict for a key/value table."""
    rows: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict) and v and len(rows) < 200:
                rows.extend(kv_rows(v, key + "."))
            else:
                rows.append((key, v if isinstance(v, str) else repr(v) if not isinstance(v, (int, float)) else str(v)))
    return rows


def size_text(n: int | float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


DROP_REASON_LABELS = {
    "rate_limited": "over the rate limit",
    "sampled": "sampled (counted, body not kept)",
    "counts_only": "store full (counted, body not kept)",
    "filter:crawler": "crawler or bot",
    "filter:health-check": "health check",
    "filter:browser-extension": "browser extension",
    "filter:localhost": "localhost",
}


def reason_label(reason: str, rules: dict | None = None) -> str:
    if reason in DROP_REASON_LABELS:
        return DROP_REASON_LABELS[reason]
    if reason.startswith("dropped:"):
        return f"{reason[8:]} (not stored)"
    if reason.startswith("filter:") and "#" in reason:
        kind, _, rid = reason[7:].partition("#")
        rule = (rules or {}).get(int(rid)) if rid.isdigit() else None
        return f"filter: {kind} {rule.pattern!r}" if rule else f"filter: {kind} (rule {rid})"
    return reason
