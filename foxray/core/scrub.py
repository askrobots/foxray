"""Remove secrets before anything is stored.

Two passes over the whole event: values under sensitive-looking keys are
replaced, and every string is checked for things that look like card numbers,
keys, tokens or passwords in URLs."""

from __future__ import annotations

import re

FILTERED = "[Filtered]"

# Matched against the key with case, '-', '_' and spaces removed.
_KEY_PARTS = (
    "password", "passwd", "secret", "token", "apikey", "authorization", "cookie",
    "csrf", "xsrf", "sessionid", "credential", "privatekey", "accesskey",
    "creditcard", "cardnumber", "signature",
)
_KEY_EXACT = {"auth", "pass", "pwd", "pin", "cvv", "cvc", "ssn", "session", "dsn", "sentrykey"}

_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), FILTERED),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9\-._~+/]{8,}=*"), r"\1 " + FILTERED),
    (re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{10,}"), FILTERED),          # Stripe
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), FILTERED),                     # AWS key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), FILTERED),                    # GitHub
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), FILTERED),                   # Slack
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), FILTERED),           # OpenAI / Anthropic style
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), FILTERED),                        # Google API key
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), FILTERED),  # JWT
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+):([^@/\s]+)@"), r"\1:" + FILTERED + "@"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api_key|apikey|access_token|"
                r"refresh_token|auth|sentry_key|client_secret)=([^&\s#]+)"), r"\1=" + FILTERED),
]


def is_sensitive_key(key) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lower().replace("-", "").replace("_", "").replace(" ", "")
    return k in _KEY_EXACT or any(p in k for p in _KEY_PARTS)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_sub(m: re.Match) -> str:
    digits = re.sub(r"[ -]", "", m.group(0))
    return FILTERED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def scrub_string(s: str) -> str:
    if not s:
        return s
    if sum(1 for c in s[:4096] if c.isdigit()) >= 13:
        s = _CARD.sub(_card_sub, s)
    for pattern, repl in _PATTERNS:
        s = pattern.sub(repl, s)
    return s


def scrub(value, extra_keys: tuple = ()):
    """Return a scrubbed copy of ``value`` (dicts, lists, strings)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if (is_sensitive_key(k) or (extra_keys and str(k).lower() in extra_keys)) \
                    and v not in (None, "", [], {}):
                out[k] = FILTERED
            else:
                out[k] = scrub(v, extra_keys)
        return out
    if isinstance(value, list):
        return [scrub(v, extra_keys) for v in value]
    if isinstance(value, tuple):
        return [scrub(v, extra_keys) for v in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def scrub_event(event: dict) -> dict:
    """Scrub an event. Frame variables and context are scrubbed like the rest;
    the grouping keys (types, modules, functions) contain no data."""
    return scrub(event)
