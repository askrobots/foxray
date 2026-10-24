"""Errors from the Django app itself, stored in-process: no DSN, no HTTP.

``foxray.django.sdk.init(**kwargs)`` calls ``sentry_sdk.init`` with a
transport that hands each envelope to Foxray's ingest on a background
thread. If storing fails it logs the error with the stdlib ``logging``
module (logger ``foxray.sdk``, which the SDK is told to ignore) and never
raises into the app.

With ``FOXRAY = {"AUTO_INIT": True}`` (the default when sentry-sdk is
installed) AppConfig.ready() does this for you, with DjangoIntegration,
CeleryIntegration if Celery is installed, traces_sample_rate=0 and no PII, unless the SDK is already
set up (your own ``sentry_sdk.init``) or ``SENTRY_DSN`` is set (unless
AUTO_INIT is set explicitly)."""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading

from .conf import conf

log = logging.getLogger("foxray.sdk")

try:
    from sentry_sdk.transport import Transport as _Transport
except ImportError:  # pragma: no cover - sentry-sdk not installed
    _Transport = object


def _summary(data: bytes) -> str:
    """A line to log when an envelope can't be stored."""
    try:
        for line in data.split(b"\n")[2:3]:
            ev = json.loads(line)
            exc = (ev.get("exception") or {}).get("values") or []
            if exc:
                return f"{exc[-1].get('type')}: {exc[-1].get('value')}"
            msg = ev.get("message") or (ev.get("logentry") or {}).get("formatted")
            if msg:
                return str(msg)[:300]
    except Exception:  # noqa: BLE001
        pass
    return f"{len(data)} bytes"


def deliver(data: bytes) -> None:
    from django.db import close_old_connections

    from .runtime import runtime
    close_old_connections()
    try:
        rt = runtime()
        response = rt.ingestor.ingest_envelope(rt.default_project(), data)
        if response.status >= 300:
            log.error("Foxray refused an event (%s): %s", response.status, _summary(data))
        if response.after is not None:
            response.after()
    except Exception as exc:  # noqa: BLE001 - never raise into the app
        log.error("Foxray could not store an event (%s: %s): %s", type(exc).__name__, exc, _summary(data))


class FoxrayTransport(_Transport):
    """A Sentry SDK transport that stores envelopes in this process."""

    def __init__(self, options=None, deliver_fn=None):
        if _Transport is not object:
            super().__init__(options)
        self._deliver = deliver_fn or deliver
        self._lock = threading.Lock()
        self._pid = None
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None

    def _ensure_worker(self) -> queue.Queue:
        with self._lock:
            # A forked worker (gunicorn, uvicorn --workers) has no thread: start one.
            if self._pid != os.getpid() or self._thread is None or not self._thread.is_alive():
                self._pid = os.getpid()
                self._queue = queue.Queue(maxsize=500)
                self._thread = threading.Thread(target=self._run, args=(self._queue,), name="foxray-sdk",
                                                daemon=True)
                self._thread.start()
            return self._queue

    def _run(self, q: queue.Queue) -> None:
        try:
            while True:
                data = q.get()
                try:
                    if data is None:
                        return
                    self._deliver(data)
                except Exception:  # noqa: BLE001
                    log.exception("Foxray transport failed")
                finally:
                    q.task_done()
        finally:
            try:
                from django.db import connections
                connections.close_all()  # this thread's connections only
            except Exception:  # noqa: BLE001
                pass

    def capture_envelope(self, envelope) -> None:
        try:
            data = envelope.serialize()
        except Exception:  # noqa: BLE001
            log.exception("could not serialize an envelope")
            return
        try:
            self._ensure_worker().put_nowait(data)
        except queue.Full:
            log.error("Foxray queue full; dropped: %s", _summary(data))

    def capture_event(self, event) -> None:
        """sentry-sdk 1.x hands plain errors here instead of to capture_envelope."""
        try:
            from sentry_sdk.envelope import Envelope
            envelope = Envelope(headers={"event_id": event.get("event_id")})
            envelope.add_event(event)
        except Exception:  # noqa: BLE001
            log.exception("could not wrap an event in an envelope")
            return
        self.capture_envelope(envelope)

    def flush(self, timeout, callback=None) -> None:
        q = self._queue
        if q is None:
            return
        with q.all_tasks_done:
            if q.unfinished_tasks:
                q.all_tasks_done.wait_for(lambda: q.unfinished_tasks == 0, timeout=timeout)

    def kill(self) -> None:
        q, thread = self._queue, self._thread
        if q is None:
            return
        try:
            q.put_nowait(None)
        except queue.Full:
            return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def is_healthy(self) -> bool:
        return True


def default_integrations(existing=()) -> list:
    names = {type(i).__name__ for i in existing}
    out = list(existing)
    from sentry_sdk.integrations.django import DjangoIntegration
    if "DjangoIntegration" not in names:
        out.append(DjangoIntegration())
    if "CeleryIntegration" not in names:
        try:
            import celery  # noqa: F401
            from sentry_sdk.integrations.celery import CeleryIntegration
            out.append(CeleryIntegration())
        except Exception:  # noqa: BLE001 - Celery not installed (or too old)
            pass
    return out


def init(**kwargs):
    """``sentry_sdk.init`` with Foxray's in-process transport. Keyword
    arguments are passed through (they win over Foxray's defaults)."""
    import sentry_sdk
    # No send_default_pii: it already defaults to off, and sentry-sdk 2.7x
    # deprecates it in favour of data_collection (whose default is also off).
    options = {"traces_sample_rate": 0, "transport": FoxrayTransport()}
    options.update(kwargs)
    options["integrations"] = default_integrations(kwargs.get("integrations") or [])
    return sentry_sdk.init(**options)


def _running_tests() -> bool:
    return (len(sys.argv) > 1 and sys.argv[1] == "test") or "pytest" in sys.modules


def auto_init() -> bool:
    c = conf()
    setting = c.get("AUTO_INIT", True)
    if not setting:
        return False
    if _running_tests() and setting != "always":
        return False  # don't write the host app's test errors into its test database
    try:
        import sentry_sdk  # noqa: F401
    except ImportError:
        return False
    # SENTRY_DSN alone usually means a real Sentry; a left-over one (the SDK
    # never started) doesn't stop an AUTO_INIT the settings asked for.
    if os.environ.get("SENTRY_DSN") and "AUTO_INIT" not in c:
        return False
    if _sdk_active():
        return False
    init(**(c.get("SDK_OPTIONS") or {}))
    return True


def _sdk_active() -> bool:
    import sentry_sdk
    try:
        return sentry_sdk.get_client().is_active()
    except AttributeError:  # pragma: no cover - sentry-sdk 1.x
        return sentry_sdk.Hub.current.client is not None
