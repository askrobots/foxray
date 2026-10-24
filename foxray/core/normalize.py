"""Turn whatever an SDK sent into the small, predictable event Foxray keeps.

Anything not listed here is dropped. User and request data are cut down to
the configured fields (``id`` and ``url``/``method`` by default)."""

from __future__ import annotations

import posixpath
import re
from urllib.parse import urlsplit, urlunsplit

from .models import LEVELS
from .settings import Settings
from .util import clip, new_id, normalize_event_id, parse_timestamp, utcnow

LEVEL_ALIASES = {"warn": "warning", "critical": "fatal", "log": "info", "err": "error", "exception": "error"}
MAX_FRAMES = 250
MAX_EXCEPTIONS = 10
MAX_TAGS = 50


def _level(value) -> str:
    if not isinstance(value, str):
        return "error"
    v = value.strip().lower()
    v = LEVEL_ALIASES.get(v, v)
    return v if v in LEVELS else "error"


def trim(value, depth: int = 0, *, max_str: int = 1000, max_items: int = 50, max_depth: int = 6):
    """Bound the size of free-form data (contexts, extra, breadcrumb data)."""
    if depth > max_depth:
        return "[too deep]"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= max_items:
                out["…"] = f"{len(value) - max_items} more"
                break
            out[clip(k, 100)] = trim(v, depth + 1, max_str=max_str, max_items=max_items, max_depth=max_depth)
        return out
    if isinstance(value, (list, tuple)):
        items = [trim(v, depth + 1, max_str=max_str, max_items=max_items, max_depth=max_depth)
                 for v in list(value)[:max_items]]
        if len(value) > max_items:
            items.append(f"… {len(value) - max_items} more")
        return items
    if isinstance(value, str):
        return clip(value, max_str)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clip(repr(value), max_str)


_LIB_PATH = re.compile(r"(site-packages|dist-packages|node_modules|/usr/lib/python|/lib/python\d|<frozen|"
                       r"^/usr/local/lib/|\\lib\\python)")


def _guess_in_app(frame: dict) -> bool:
    path = frame.get("abs_path") or frame.get("filename") or ""
    module = frame.get("module") or ""
    if _LIB_PATH.search(path):
        return False
    if module.split(".")[0] in ("django", "celery", "kombu", "asyncio", "concurrent", "threading",
                                "sentry_sdk", "gunicorn", "uvicorn", "starlette", "importlib"):
        return False
    return True


def _frame(raw: dict, settings: Settings) -> dict:
    f = {}
    for key, limit in (("filename", 300), ("abs_path", 500), ("module", 200), ("function", 200),
                       ("package", 300), ("platform", 32)):
        v = raw.get(key)
        if isinstance(v, str) and v:
            f[key] = clip(v, limit)
    for key in ("lineno", "colno"):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            f[key] = v
    in_app = raw.get("in_app")
    f["in_app"] = in_app if isinstance(in_app, bool) else _guess_in_app(f)
    if isinstance(raw.get("context_line"), str):
        f["context_line"] = clip(raw["context_line"], 300)
    for key in ("pre_context", "post_context"):
        v = raw.get(key)
        if isinstance(v, list):
            f[key] = [clip(x, 300) if isinstance(x, str) else "" for x in v[-10:]]
    if settings.keep_frame_vars and isinstance(raw.get("vars"), dict):
        f["vars"] = trim(raw["vars"], max_str=300, max_items=30, max_depth=3)
    return f


def _stacktrace(raw, settings: Settings) -> dict | None:
    if not isinstance(raw, dict):
        return None
    frames = raw.get("frames")
    if not isinstance(frames, list):
        return None
    frames = [x for x in frames if isinstance(x, dict)]
    if len(frames) > MAX_FRAMES:  # keep the end: the crashing frame is last
        frames = frames[-MAX_FRAMES:]
    return {"frames": [_frame(x, settings) for x in frames]}


def _exceptions(raw, settings: Settings) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("values")
    if not isinstance(raw, list):
        return []
    out = []
    for value in [x for x in raw if isinstance(x, dict)][-MAX_EXCEPTIONS:]:
        e = {"type": clip(value.get("type"), 200) or "", "value": clip(value.get("value"), 2000) or ""}
        if not isinstance(value.get("type"), str):
            e["type"] = "Error" if value.get("value") else ""
        if isinstance(value.get("value"), (int, float)) and not isinstance(value.get("value"), bool):
            e["value"] = str(value["value"])
        if isinstance(value.get("module"), str):
            e["module"] = clip(value["module"], 200)
        mech = value.get("mechanism")
        if isinstance(mech, dict):
            m = {}
            if isinstance(mech.get("type"), str):
                m["type"] = clip(mech["type"], 64)
            if isinstance(mech.get("handled"), bool):
                m["handled"] = mech["handled"]
            if m:
                e["mechanism"] = m
        st = _stacktrace(value.get("stacktrace"), settings)
        if st:
            e["stacktrace"] = st
        out.append(e)
    return out


def _message(raw: dict) -> tuple[str, str]:
    """(formatted message, template for grouping)."""
    candidates = []
    for key in ("logentry", "sentry.interfaces.Message"):
        if isinstance(raw.get(key), dict):
            candidates.append(raw[key])
    msg = raw.get("message")
    if isinstance(msg, dict):
        candidates.append(msg)
    for entry in candidates:
        template = entry.get("message") if isinstance(entry.get("message"), str) else ""
        formatted = entry.get("formatted") if isinstance(entry.get("formatted"), str) else ""
        params = entry.get("params")
        if not formatted and template:
            formatted = template
            if params:
                try:
                    formatted = template % (tuple(params) if isinstance(params, list) else params)
                except (TypeError, ValueError, KeyError):
                    pass
        if formatted or template:
            return clip(formatted, 8192) or "", clip(template or formatted, 8192) or ""
    if isinstance(msg, str) and msg:
        return clip(msg, 8192), clip(msg, 8192)
    return "", ""


def _tags(raw) -> dict:
    out = {}
    if isinstance(raw, dict):
        pairs = raw.items()
    elif isinstance(raw, list):
        pairs = [p for p in raw if isinstance(p, (list, tuple)) and len(p) == 2]
    else:
        return out
    for k, v in pairs:
        if len(out) >= MAX_TAGS:
            break
        if k is None or v is None:
            continue
        out[clip(str(k), 32)] = clip(str(v), 200)
    return out


def _strip_query(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return clip(url.split("?")[0], 500)
    return clip(urlunsplit((p.scheme, p.netloc.rpartition("@")[2], p.path, "", "")), 500)


def _breadcrumbs(raw, cap: int) -> list:
    if isinstance(raw, dict):
        raw = raw.get("values")
    if not isinstance(raw, list) or cap <= 0:
        return []
    out = []
    for b in [x for x in raw if isinstance(x, dict)][-cap:]:
        crumb = {}
        ts = parse_timestamp(b.get("timestamp"))
        if ts:
            crumb["timestamp"] = ts.isoformat()
        for key, limit in (("type", 32), ("category", 64), ("message", 1000)):
            if isinstance(b.get(key), str):
                crumb[key] = clip(b[key], limit)
        if isinstance(b.get("level"), str):
            crumb["level"] = _level(b["level"])
        if isinstance(b.get("data"), dict):
            crumb["data"] = trim(b["data"], max_str=300, max_items=20, max_depth=3)
        out.append(crumb)
    return out


def normalize_event(raw: dict, settings: Settings, envelope_header: dict | None = None,
                    received=None) -> tuple[dict, dict]:
    """Returns ``(event, meta)``. ``meta`` carries facts used by the inbound
    filters that are not kept (the user agent, the full request URL)."""
    received = received or utcnow()
    meta: dict = {}
    event_id = normalize_event_id(raw.get("event_id")) or \
        normalize_event_id((envelope_header or {}).get("event_id")) or new_id()
    ts = parse_timestamp(raw.get("timestamp")) or received
    ev: dict = {
        "event_id": event_id,
        "timestamp": ts.isoformat(),
        "received": received.isoformat(),
        "level": _level(raw.get("level")),
        "platform": clip(raw.get("platform"), 32) if isinstance(raw.get("platform"), str) else "other",
    }
    for key, limit in (("logger", 64), ("release", 200), ("dist", 64), ("environment", 64),
                       ("server_name", 128), ("transaction", 200)):
        v = raw.get(key)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v) != "":
            ev[key] = clip(str(v), limit)
    if not ev.get("release") and isinstance((envelope_header or {}).get("trace"), dict):
        rel = envelope_header["trace"].get("release")
        if isinstance(rel, str) and rel:
            ev["release"] = clip(rel, 200)

    message, template = _message(raw)
    if message:
        ev["message"] = message
        if template and template != message:
            ev["message_template"] = template

    exceptions = _exceptions(raw.get("exception"), settings)
    if exceptions:
        ev["exception"] = {"values": exceptions}
    st = _stacktrace(raw.get("stacktrace"), settings)
    if not st and not exceptions:
        threads = raw.get("threads")
        if isinstance(threads, dict):
            threads = threads.get("values")
        if isinstance(threads, list):
            for t in threads:
                if isinstance(t, dict) and t.get("stacktrace") and (t.get("crashed") or t.get("current")
                                                                    or len(threads) == 1):
                    st = _stacktrace(t.get("stacktrace"), settings)
                    if st:
                        break
    if st:
        ev["stacktrace"] = st

    tags = _tags(raw.get("tags"))
    if tags:
        ev["tags"] = tags

    user = raw.get("user")
    if isinstance(user, dict):
        kept = {k: clip(str(user[k]), 200) for k in settings.user_fields
                if user.get(k) not in (None, "", "{{auto}}")}
        if kept:
            ev["user"] = kept

    request = raw.get("request")
    if isinstance(request, dict):
        url = request.get("url") if isinstance(request.get("url"), str) else ""
        meta["request_url"] = url
        headers = request.get("headers")
        if isinstance(headers, dict):
            for k, v in headers.items():
                if isinstance(k, str) and k.lower() == "user-agent" and isinstance(v, str):
                    meta["user_agent"] = v
        elif isinstance(headers, list):
            for pair in headers:
                if isinstance(pair, (list, tuple)) and len(pair) == 2 and str(pair[0]).lower() == "user-agent":
                    meta["user_agent"] = str(pair[1])
        kept = {}
        for k in settings.request_fields:
            v = request.get(k)
            if v in (None, ""):
                continue
            if k == "url" and isinstance(v, str):
                kept["url"] = _strip_query(v)
            elif k == "method" and isinstance(v, str):
                kept["method"] = clip(v.upper(), 16)
            else:
                kept[k] = trim(v, max_str=500, max_items=30, max_depth=2)
        if kept:
            ev["request"] = kept

    contexts = raw.get("contexts")
    if isinstance(contexts, dict):
        ctx = {k: trim(v, max_str=500, max_items=30, max_depth=3)
               for k, v in list(contexts.items())[:30] if isinstance(v, dict)}
        if ctx:
            ev["contexts"] = ctx
        browser = contexts.get("browser")
        if isinstance(browser, dict) and isinstance(browser.get("name"), str):
            meta.setdefault("browser", browser["name"])

    crumbs = _breadcrumbs(raw.get("breadcrumbs"), settings.breadcrumbs_cap)
    if crumbs:
        ev["breadcrumbs"] = crumbs

    extra = raw.get("extra")
    if isinstance(extra, dict) and extra:
        ev["extra"] = trim(extra, max_str=1000, max_items=30, max_depth=4)

    sdk = raw.get("sdk") or (envelope_header or {}).get("sdk")
    if isinstance(sdk, dict):
        ev["sdk"] = {k: clip(str(sdk[k]), 64) for k in ("name", "version") if sdk.get(k)}

    fp = raw.get("fingerprint")
    if isinstance(fp, list) and fp:
        ev["fingerprint"] = [clip(str(x), 200) for x in fp[:20] if x is not None]
    return ev, meta


def all_frames(ev: dict) -> list[dict]:
    frames = []
    for exc in (ev.get("exception") or {}).get("values", []):
        frames.extend((exc.get("stacktrace") or {}).get("frames", []))
    frames.extend((ev.get("stacktrace") or {}).get("frames", []))
    return frames


def event_title(ev: dict) -> str:
    excs = (ev.get("exception") or {}).get("values") or []
    if excs:
        top = excs[-1]
        t, v = top.get("type") or "Error", (top.get("value") or "").strip()
        first = v.splitlines()[0] if v else ""
        return clip(f"{t}: {first}" if first else t, 200)
    msg = (ev.get("message") or "").strip()
    if msg:
        return clip(msg.splitlines()[0], 200)
    if ev.get("transaction"):
        return clip(ev["transaction"], 200)
    return "<unlabeled event>"


def event_culprit(ev: dict) -> str:
    if ev.get("transaction"):
        return clip(ev["transaction"], 200)
    frames = all_frames(ev)
    app = [f for f in frames if f.get("in_app")] or frames
    if app:
        f = app[-1]
        where = f.get("module") or posixpath.basename(f.get("filename") or f.get("abs_path") or "") or "?"
        return clip(f"{where} in {f.get('function') or '?'}", 200)
    return ""
