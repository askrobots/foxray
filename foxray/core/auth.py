"""Finding the public key an SDK sent: the ``X-Sentry-Auth`` header, an
``Authorization: Sentry ...`` header, ``sentry_key`` in the query string, or
the ``dsn`` in an envelope header (used by browser tunnels)."""

from __future__ import annotations

import hmac
from typing import Mapping

from .dsn import DsnError, parse_dsn


def parse_auth_header(value: str) -> dict[str, str]:
    """``Sentry sentry_key=abc, sentry_version=7, sentry_client=x/1.0``."""
    if not value:
        return {}
    s = value.strip()
    if s[:7].lower() == "sentry ":
        s = s[7:]
    out: dict[str, str] = {}
    for part in s.split(","):
        key, sep, val = part.strip().partition("=")
        if sep and key.strip():
            out[key.strip()] = val.strip().strip('"')
    return out


def extract_public_key(headers: Mapping[str, str], query: Mapping[str, str],
                       envelope_header: dict | None = None) -> str | None:
    """``headers`` must have lower-case names."""
    raw = headers.get("x-sentry-auth")
    if not raw:
        authz = headers.get("authorization", "")
        if authz[:7].lower() == "sentry ":
            raw = authz
    if raw:
        key = parse_auth_header(raw).get("sentry_key")
        if key:
            return key
    key = query.get("sentry_key") if query else None
    if key:
        return key
    if envelope_header and isinstance(envelope_header.get("dsn"), str):
        try:
            return parse_dsn(envelope_header["dsn"]).public_key
        except DsnError:
            return None
    return None


def key_matches(given: str | None, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())
