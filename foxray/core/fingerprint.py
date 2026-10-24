"""Grouping events into issues.

An SDK-supplied ``fingerprint`` wins (with ``{{ default }}`` standing for the
built-in grouping). Otherwise: each exception's type plus its in-app frames
as ``module:function`` (no line numbers, so edits elsewhere in a file don't
split an issue). Events with no exception group by their message with
numbers, hex and ids blanked out."""

from __future__ import annotations

import hashlib
import posixpath
import re

_DEFAULT = re.compile(r"^\{\{\s*default\s*\}\}$")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b")
_HEX_PREFIXED = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LONG_HEX = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
_FILE_HASH = re.compile(r"[.-][0-9a-f]{6,}(?=\.)", re.I)


def normalize_message(message: str) -> str:
    s = _UUID.sub("<id>", message)
    s = _HEX_PREFIXED.sub("<hex>", s)
    s = _LONG_HEX.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _SPACE.sub(" ", s).strip()
    return s[:500]


def _frame_key(frame: dict) -> str:
    where = frame.get("module")
    if not where:
        name = posixpath.basename((frame.get("filename") or frame.get("abs_path") or "").split("?")[0])
        where = _FILE_HASH.sub("", name)  # app.3f2a1b9c.js -> app.js
    return f"{where or '?'}:{frame.get('function') or '?'}"


def _frames_components(frames: list[dict]) -> list[str]:
    app = [f for f in frames if f.get("in_app")]
    return [_frame_key(f) for f in (app or frames)]


def default_components(ev: dict) -> list[str]:
    comps: list[str] = []
    excs = (ev.get("exception") or {}).get("values") or []
    for exc in excs:
        comps.append("type:" + (exc.get("type") or "Error"))
        frames = (exc.get("stacktrace") or {}).get("frames") or []
        if frames:
            comps.extend(_frames_components(frames))
        elif exc.get("value"):
            comps.append("value:" + normalize_message(exc["value"]))
    if comps:
        return comps
    message = ev.get("message_template") or ev.get("message")
    if message:
        return ["message:" + normalize_message(message)]
    frames = (ev.get("stacktrace") or {}).get("frames") or []
    if frames:
        return _frames_components(frames)
    return ["empty:" + (ev.get("transaction") or "")]


def fingerprint_components(ev: dict) -> list[str]:
    custom = ev.get("fingerprint")
    if isinstance(custom, list) and custom:
        comps: list[str] = []
        for part in custom:
            if _DEFAULT.match(str(part)):
                comps.extend(default_components(ev))
            else:
                comps.append("custom:" + str(part))
        return comps
    return default_components(ev)


def fingerprint(ev: dict) -> str:
    joined = "\n".join(fingerprint_components(ev))
    return hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()
