"""The ingest pipeline, shared by the standalone server, the Django views and
the in-process Django transport.

auth -> size -> decode -> parse -> per item:
  event:    normalise -> inbound filters -> rate limit -> scrub -> group ->
            one transaction (issue, daily count, body unless sampled or the
            store is full, users, tags, alert into the outbox)
  check_in: monitor update (and its alert) in one transaction
  anything else: accepted, dropped, counted per project per day

Every answer to an SDK is 200 unless the request itself is wrong (auth,
size, encoding) or the project is over its rate limit.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping

from .alerts import Links, issue_draft, queue_alert, should_alert_issue
from .auth import extract_public_key, key_matches
from .envelope import Item, PayloadError, decode_body, parse_envelope, parse_store_body
from .filters import filter_reason
from .fingerprint import fingerprint
from .models import Project
from .monitors import process_checkin
from .normalize import event_title, event_culprit, normalize_event
from .scrub import scrub_event
from .settings import Settings
from .sql import Duplicate
from .util import day_str, parse_timestamp, utcnow

log = logging.getLogger("foxray.ingest")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Content-Encoding, X-Sentry-Auth, Authorization, "
                                    "sentry-trace, baggage",
    "Access-Control-Expose-Headers": "X-Sentry-Rate-Limits, Retry-After",
    "Access-Control-Max-Age": "86400",
}
RATE_LIMIT_CATEGORIES = "error;default;security"


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    after: Callable | None = None  # run after the response is sent (cleanup, alerts)


def _json(status: int, obj, headers: dict | None = None) -> Response:
    h = {"Content-Type": "application/json", **CORS_HEADERS}
    h.update(headers or {})
    return Response(status, json.dumps(obj).encode(), h)


@dataclass
class _Outcome:
    event_ids: list = field(default_factory=list)
    stored: int = 0
    accepted: int = 0
    rate_limited: int = 0
    alerted: bool = False


class Ingestor:
    def __init__(self, store, settings: Settings | None = None, *, links: Links | None = None,
                 channels_for: Callable[[Project], list[str]] | None = None, housekeeper=None,
                 clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.settings = settings or Settings()
        self.links = links or Links(self.settings.base_url)
        self.channels_for = channels_for or (lambda project: ["log"])
        self.housekeeper = housekeeper
        self.clock = clock

    # -- entry points ------------------------------------------------------

    def preflight(self) -> Response:
        return Response(200, b"", dict(CORS_HEADERS))

    def handle(self, project_id, kind: str, headers: Mapping[str, str], query: Mapping[str, str],
               body: bytes) -> Response:
        """An HTTP request to ``/api/<project_id>/<kind>/``. ``headers`` must
        have lower-case names."""
        project = self.store.get_project(project_id)
        if project is None:
            return _json(404, {"detail": "unknown project"})
        if len(body) > self.settings.max_body_bytes:
            return _json(413, {"detail": "body too large"})
        try:
            data = decode_body(body, headers.get("content-encoding"), self.settings.max_decompressed_bytes)
            if kind == "store":
                envelope_header: dict = {}
                items = [Item({"type": "event"}, data)]
                parse_store_body(data, self.settings.max_decompressed_bytes)  # validate early
            else:
                envelope_header, items = parse_envelope(data, self.settings.max_envelope_items)
        except PayloadError as exc:
            return _json(exc.status, {"detail": exc.message})
        key = extract_public_key(headers, query, envelope_header)
        if not key:
            return _json(401, {"detail": "missing authentication (X-Sentry-Auth or sentry_key)"})
        if not key_matches(key, project.public_key):
            return _json(403, {"detail": "invalid key for this project"})
        return self._process(project, envelope_header, items, legacy=(kind == "store"))

    def ingest_envelope(self, project: Project, data: bytes) -> Response:
        """A serialized envelope from inside the same process (no HTTP, no auth)."""
        try:
            envelope_header, items = parse_envelope(data, self.settings.max_envelope_items)
        except PayloadError as exc:
            return _json(exc.status, {"detail": exc.message})
        return self._process(project, envelope_header, items)

    # -- the work ----------------------------------------------------------

    def _process(self, project: Project, header: dict, items: list[Item], legacy: bool = False) -> Response:
        now = self.clock()
        states = self.store.states()
        mode = states.get("mode")
        counts_only = bool(mode and mode.value == "counts_only")
        out = _Outcome()
        rules = None
        day = day_str(now)
        for item in items:
            kind = item.type
            if kind == "event":
                if len(item.payload) > self.settings.max_event_bytes:
                    self.store.bump_counter(project.id, day, "too_large:event")
                    continue
                try:
                    raw = parse_store_body(item.payload, self.settings.max_decompressed_bytes) if legacy \
                        else item.json()
                except PayloadError:
                    self.store.bump_counter(project.id, day, "invalid:event")
                    continue
                if rules is None:
                    rules = self.store.list_filters(project.id, enabled_only=True)
                self._event(project, raw, header, now, counts_only, rules, out)
            elif kind == "check_in":
                try:
                    payload = item.json()
                except PayloadError:
                    self.store.bump_counter(project.id, day, "invalid:check_in")
                    continue
                checkin, alerted = process_checkin(self.store, project, payload, now, links=self.links,
                                                   channels=self.channels_for(project))
                if checkin is None:
                    self.store.bump_counter(project.id, day, "invalid:check_in")
                out.accepted += 1
                out.alerted |= alerted
            else:
                self.store.bump_counter(project.id, day, "dropped:" + (kind or "unknown")[:40])
                out.accepted += 1

        headers = {}
        if out.rate_limited:
            retry = max(1, 60 - now.second)
            headers = {"Retry-After": str(retry),
                       "X-Sentry-Rate-Limits": f"{retry}:{RATE_LIMIT_CATEGORIES}:project"}
        if out.rate_limited and not out.stored and not out.accepted:
            resp = _json(429, {"detail": "rate limited"}, headers)
        else:
            event_id = header.get("event_id") if isinstance(header.get("event_id"), str) else None
            event_id = event_id or (out.event_ids[0] if out.event_ids else None)
            resp = _json(200, {"id": event_id} if event_id else {}, headers)
        if self.housekeeper is not None:
            resp.after = self.housekeeper.after_ingest(states, now, out.alerted)
        return resp

    def _event(self, project: Project, raw: dict, header: dict, now: datetime, counts_only: bool,
               rules, out: _Outcome) -> None:
        s = self.settings
        day = day_str(now)
        ev, meta = normalize_event(raw, s, header, now)
        out.event_ids.append(ev["event_id"])
        reason = filter_reason(project, s, rules, ev, meta)
        if reason:
            self.store.bump_counter(project.id, day, reason)
            out.accepted += 1
            return
        limit = s.rate_limit_for(project)
        if limit and limit > 0:
            window = math.floor(now.timestamp() / 60)
            if self.store.hit_rate(project.id, window) > limit:
                self.store.bump_counter(project.id, day, "rate_limited")
                out.rate_limited += 1
                return
        ev = scrub_event(ev)
        try:
            alerted, stored = self._save(project, ev, now, counts_only)
        except Duplicate:
            out.accepted += 1
            return
        out.alerted |= alerted
        out.stored += 1 if stored else 0
        out.accepted += 0 if stored else 1

    def _save(self, project: Project, ev: dict, now: datetime, counts_only: bool) -> tuple[bool, bool]:
        s, store = self.settings, self.store
        title, culprit, fp = event_title(ev), event_culprit(ev), fingerprint(ev)
        env, rel = ev.get("environment") or "", ev.get("release") or ""
        level, platform = ev.get("level") or "error", ev.get("platform") or "other"
        user = ev.get("user") or {}
        user_key = next((str(user[k]) for k in ("id", "username", "email", "ip_address") if user.get(k)), "")
        day = day_str(now)
        with store.atomic():
            issue = store.insert_issue_if_new(project.id, fp, title=title, culprit=culprit, level=level,
                                              platform=platform, environment=env, release=rel, now=now)
            kind = "new_issue" if issue is not None else None
            if issue is None:
                current = store.lock_issue(project.id, fp)
                changes: dict = {}
                if current.status == "resolved":
                    changes = dict(status="unresolved", resolved_at=None, reopened_at=now)
                    kind = "regression"
                elif current.status == "ignored" and (
                        (current.ignore_until is not None and current.ignore_until <= now) or
                        (current.ignore_until_count is not None and current.times_seen + 1 >= current.ignore_until_count)):
                    changes = dict(status="unresolved", ignore_until=None, ignore_until_count=None, reopened_at=now)
                    kind = "unignored"
                issue = store.record_repeat(current.id, now=now, title=title, culprit=culprit, level=level,
                                            environment=env, release=rel, **changes)
            today = store.bump_issue_day(issue.id, project.id, day)
            sample_after = s.project_value(project, "sample_after")
            stored = not counts_only and not (sample_after and today > sample_after)
            if stored:
                row = store.insert_event(project_id=project.id, issue_id=issue.id, event_id=ev["event_id"],
                                         received_at=now, occurred_at=parse_timestamp(ev.get("timestamp")),
                                         level=level, title=title, environment=env, release=rel,
                                         user_key=user_key, data=ev)
                if row is None:
                    raise Duplicate(ev["event_id"])  # rolls back the counts above
            else:
                store.bump_counter(project.id, day, "counts_only" if counts_only else "sampled")
            if user_key and store.add_issue_user(issue.id, user_key):
                issue.users_seen += 1
            for key, value in (("environment", env), ("release", rel), ("server_name", ev.get("server_name"))):
                if value:
                    store.bump_issue_tag(issue.id, key, value, now)
            alerted = False
            if kind and should_alert_issue(project, s, issue, kind, env, now):
                queue_alert(store, project, issue_draft(project, issue, kind, ev, self.links),
                            self.channels_for(project), now)
                store.update_issue(issue.id, last_alerted_at=now)
                alerted = True
        return alerted, stored
