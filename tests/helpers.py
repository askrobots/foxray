"""Builders shared by the tests."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from foxray.core.alerts import Links, Outbox
from foxray.core.ingest import Ingestor
from foxray.core.maintenance import Housekeeper, Maintenance
from foxray.core.settings import Settings


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def envelope(*items, header: dict | None = None, with_length: bool = True) -> bytes:
    """items: (item_header_dict, payload) with payload a dict or bytes."""
    out = [json.dumps(header or {}).encode()]
    for item_header, payload in items:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h = dict(item_header)
        if with_length:
            h["length"] = len(data)
        out.append(json.dumps(h).encode())
        out.append(data)
    return b"\n".join(out) + b"\n"


def make_event(exc_type="ValueError", value="boom", *, module="app.views", function="index",
               in_app=True, lineno=10, event_id=None, **extra) -> dict:
    ev = {
        "event_id": event_id or uuid.uuid4().hex,
        "platform": "python",
        "level": "error",
        "exception": {"values": [{
            "type": exc_type, "value": value,
            "stacktrace": {"frames": [
                {"module": "django.core.handlers", "function": "inner", "lineno": 55, "in_app": False},
                {"module": module, "function": function, "lineno": lineno, "in_app": in_app,
                 "context_line": "    raise ValueError('boom')", "pre_context": ["def index():"],
                 "post_context": [""]},
            ]},
        }]},
    }
    ev.update(extra)
    return ev


def auth_headers(project, **extra) -> dict:
    h = {"x-sentry-auth": f"Sentry sentry_key={project.public_key}, sentry_version=7, sentry_client=test/1"}
    h.update({k.lower(): v for k, v in extra.items()})
    return h


class Recorder:
    """A notifier channel that remembers what it sent."""

    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    def __call__(self, alert, project):
        if self.fail:
            raise RuntimeError("mail server down")
        self.sent.append(alert)
        return "recorded"


def make_stack(store, settings: Settings | None = None, clock: Clock | None = None, channel: Recorder | None = None):
    settings = settings or Settings()
    clock = clock or Clock()
    channel = channel or Recorder()
    links = Links("http://foxray.test")
    outbox = Outbox(store, settings, {"test": channel}, clock=clock)
    maint = Maintenance(store, settings, links=links, global_channels=["test"], clock=clock)
    hk = Housekeeper(store, settings, maintenance=maint, outbox=outbox, links=links,
                     channels_for=lambda p: ["test"], clock=clock)
    ing = Ingestor(store, settings, links=links, channels_for=lambda p: ["test"], housekeeper=hk, clock=clock)
    return ing, hk, outbox, channel, clock
