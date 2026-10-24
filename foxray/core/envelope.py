"""Request bodies: content encodings, envelopes, and the legacy store JSON.

Envelope format (develop.sentry.dev/sdk/envelopes): one JSON header line,
then items, each a JSON header line and a payload. With ``length`` the
payload is exactly that many bytes; without it the payload runs to the next
newline or the end."""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from dataclasses import dataclass

try:  # Optional: the Python SDK picks brotli when the module is installed.
    import brotli  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    brotli = None


class PayloadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Item:
    headers: dict
    payload: bytes

    @property
    def type(self) -> str:
        t = self.headers.get("type")
        return t if isinstance(t, str) else ""

    def json(self) -> dict:
        try:
            data = json.loads(self.payload)
        except (ValueError, UnicodeDecodeError):
            raise PayloadError(400, "item payload is not JSON") from None
        if not isinstance(data, dict):
            raise PayloadError(400, "item payload is not a JSON object")
        return data


def supported_encodings() -> tuple[str, ...]:
    base = ("identity", "gzip", "deflate")
    return base + ("br",) if brotli is not None else base


def decode_body(body: bytes, content_encoding: str | None, limit: int) -> bytes:
    """Undo ``Content-Encoding``. 415 for encodings we can't read, 413 when
    the decoded body would pass ``limit``."""
    encodings = [e.strip().lower() for e in (content_encoding or "").split(",") if e.strip()]
    data = body
    for enc in reversed(encodings):
        if enc == "identity":
            continue
        if enc in ("gzip", "x-gzip"):
            data = _inflate(data, 16 + zlib.MAX_WBITS, limit)
        elif enc == "deflate":
            try:
                data = _inflate(data, zlib.MAX_WBITS, limit)
            except PayloadError as exc:
                if exc.status != 400:
                    raise
                data = _inflate(data, -zlib.MAX_WBITS, limit)  # raw deflate, no zlib wrapper
        elif enc == "br" and brotli is not None:
            data = _unbrotli(data, limit)
        else:
            raise PayloadError(415, f"unsupported content encoding: {enc}")
    if len(data) > limit:
        raise PayloadError(413, "body too large")
    return data


def _inflate(data: bytes, wbits: int, limit: int) -> bytes:
    d = zlib.decompressobj(wbits)
    try:
        out = d.decompress(data, limit + 1)
        if len(out) <= limit and not d.unconsumed_tail:
            out += d.flush()
    except zlib.error:
        raise PayloadError(400, "body could not be decompressed") from None
    if len(out) > limit or d.unconsumed_tail:
        raise PayloadError(413, "decompressed body too large")
    return out


def _unbrotli(data: bytes, limit: int) -> bytes:  # pragma: no cover - optional
    try:
        dec = brotli.Decompressor()
        out = bytearray()
        step = 64 * 1024
        for i in range(0, len(data), step):
            out += dec.process(data[i:i + step])
            if len(out) > limit:
                raise PayloadError(413, "decompressed body too large")
        return bytes(out)
    except brotli.error:
        raise PayloadError(400, "body could not be decompressed") from None


def parse_envelope(data: bytes, max_items: int = 100) -> tuple[dict, list[Item]]:
    n = len(data)
    end = data.find(b"\n")
    first = data if end < 0 else data[:end]
    pos = n if end < 0 else end + 1
    header = _json_line(first, "envelope header") if first.strip() else {}
    items: list[Item] = []
    while pos < n:
        end = data.find(b"\n", pos)
        line = data[pos:] if end < 0 else data[pos:end]
        pos = n if end < 0 else end + 1
        if not line.strip():
            continue
        item_header = _json_line(line, "item header")
        length = item_header.get("length")
        if length is not None:
            if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                raise PayloadError(400, "bad item length")
            if pos + length > n:
                raise PayloadError(400, "item shorter than its length")
            payload = data[pos:pos + length]
            pos += length
            if pos < n and data[pos:pos + 1] == b"\n":
                pos += 1
        else:
            end = data.find(b"\n", pos)
            payload = data[pos:] if end < 0 else data[pos:end]
            pos = n if end < 0 else end + 1
        items.append(Item(item_header, payload))
        if len(items) > max_items:
            raise PayloadError(413, "too many items in envelope")
    return header, items


def _json_line(line: bytes, what: str) -> dict:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise PayloadError(400, f"{what} is not JSON") from None
    if not isinstance(value, dict):
        raise PayloadError(400, f"{what} is not a JSON object")
    return value


def parse_store_body(data: bytes, limit: int) -> dict:
    """The legacy ``/store/`` endpoint: a JSON event. Very old SDKs sent it
    zlib-compressed and base64-encoded."""
    s = data.strip()
    if s[:1] != b"{":
        try:
            s = _inflate(base64.b64decode(s, validate=False), zlib.MAX_WBITS, limit)
        except (binascii.Error, ValueError):
            raise PayloadError(400, "store body is not JSON") from None
    try:
        value = json.loads(s)
    except (ValueError, UnicodeDecodeError):
        raise PayloadError(400, "store body is not JSON") from None
    if not isinstance(value, dict):
        raise PayloadError(400, "store body is not a JSON object")
    return value
