"""Inbound filters, applied before an event is stored. A dropped event still
gets a 200; the reason is counted per project per day.

Built-in noise filters (per-project switches): crawlers by user agent,
browser-extension frames and localhost URLs on JavaScript events, and
health-check transactions. Rules (stored per project): exception type,
message, environment or release, as a substring or a regular expression,
optionally limited to one environment and/or release."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from .normalize import all_frames

RULE_KINDS = ("exception", "message", "environment", "release")
MAX_PATTERN = 200          # characters in a rule's pattern
MAX_SUBJECT = 2000         # characters of text a rule looks at

_CRAWLER = re.compile(
    r"(bot\b|bot/|crawler|spider|slurp|mediapartners|facebookexternalhit|bingpreview|"
    r"yandex|baiduspider|ahrefs|semrush|petalbot|bytespider|gptbot|claudebot|"
    r"applebot|duckduckbot|archive\.org_bot|headlesschrome|phantomjs|lighthouse)", re.I)
_EXTENSION = re.compile(r"^(chrome|moz|safari|safari-web|ms-browser|edge)-extension://|^webkit-masked-url://", re.I)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")
# Nested quantifiers such as (a+)+ can take exponential time in Python's re.
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{?]")


class PatternError(ValueError):
    pass


def validate_pattern(pattern: str, is_regex: bool) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise PatternError("pattern is empty")
    if len(pattern) > MAX_PATTERN:
        raise PatternError(f"pattern is longer than {MAX_PATTERN} characters")
    if is_regex:
        if _NESTED_QUANT.search(pattern):
            raise PatternError("nested repeats like (a+)+ are not allowed")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PatternError(f"bad regular expression: {exc}") from None
    return pattern


@lru_cache(maxsize=512)
def _compiled(pattern: str, is_regex: bool):
    if is_regex:
        return re.compile(pattern, re.I)
    return pattern.lower()


def text_matches(pattern: str, is_regex: bool, text: str) -> bool:
    if not text:
        return False
    text = text[:MAX_SUBJECT]
    try:
        validate_pattern(pattern, is_regex)
    except PatternError:
        return False
    m = _compiled(pattern, is_regex)
    return bool(m.search(text)) if is_regex else m in text.lower()


def _exception_types(ev: dict) -> list[str]:
    out = []
    for exc in (ev.get("exception") or {}).get("values") or []:
        t = exc.get("type") or ""
        if t:
            out.append(t)
            if exc.get("module"):
                out.append(f"{exc['module']}.{t}")
    return out


def _messages(ev: dict) -> list[str]:
    out = [ev.get("message") or ""]
    for exc in (ev.get("exception") or {}).get("values") or []:
        if exc.get("value"):
            out.append(exc["value"])
    return [m for m in out if m]


def rule_matches(rule, ev: dict) -> bool:
    if not rule.enabled:
        return False
    if rule.environment and rule.environment != (ev.get("environment") or ""):
        return False
    if rule.release and rule.release != (ev.get("release") or ""):
        return False
    if rule.kind == "exception":
        return any(text_matches(rule.pattern, rule.is_regex, t) for t in _exception_types(ev))
    if rule.kind == "message":
        return any(text_matches(rule.pattern, rule.is_regex, t) for t in _messages(ev))
    if rule.kind in ("environment", "release"):
        value = ev.get(rule.kind) or ""
        if rule.is_regex:
            return text_matches(rule.pattern, True, value)
        return value.lower() == rule.pattern.strip().lower()
    return False


def _health_names(project, settings) -> list[str]:
    names = list(settings.health_check_names)
    extra = getattr(project, "health_check_names", "") or ""
    names += [n.strip() for n in extra.split(",") if n.strip()]
    return [n.lower().rstrip("/") for n in names if n]


def _path_of(value: str) -> str:
    if "://" in value:
        try:
            value = urlsplit(value).path
        except ValueError:
            pass
    return value.split("?")[0].lower().rstrip("/")


def is_health_check(ev: dict, meta: dict, names: list[str]) -> bool:
    candidates = []
    if ev.get("transaction"):
        candidates.append(_path_of(ev["transaction"]))
    url = (ev.get("request") or {}).get("url") or meta.get("request_url") or ""
    if url:
        candidates.append(_path_of(url))
    for c in candidates:
        if not c:
            continue
        last = c.rsplit("/", 1)[-1]
        for n in names:
            bare = n.lstrip("/")
            if c == n or c == bare or (bare and last == bare) or c.endswith("." + bare):
                return True
    return False


def builtin_reason(project, settings, ev: dict, meta: dict) -> str | None:
    if getattr(project, "filter_crawlers", False):
        ua = meta.get("user_agent") or ""
        if ua and _CRAWLER.search(ua):
            return "filter:crawler"
    if getattr(project, "filter_health_checks", False):
        if is_health_check(ev, meta, _health_names(project, settings)):
            return "filter:health-check"
    if getattr(project, "filter_js_noise", False) and (ev.get("platform") or "") in ("javascript", "node"):
        frames = all_frames(ev)
        if any(_EXTENSION.match(f.get("abs_path") or f.get("filename") or "") for f in frames):
            return "filter:browser-extension"
        url = (ev.get("request") or {}).get("url") or meta.get("request_url") or ""
        if url:
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if host in _LOCAL_HOSTS or host.endswith(".localhost"):
                return "filter:localhost"
    return None


def filter_reason(project, settings, rules, ev: dict, meta: dict) -> str | None:
    """The reason to drop this event, or None to keep it."""
    reason = builtin_reason(project, settings, ev, meta)
    if reason:
        return reason
    for rule in rules:
        if rule_matches(rule, ev):
            return f"filter:{rule.kind}#{rule.id}"
    return None
