"""Alerts: decided inside the ingest transaction, written to the
``foxray_alert`` outbox, and sent after commit by a short background
thread. A send that fails is retried with backoff; nothing is lost if the
mail server is down for a while.

Issue alerts fire on a new issue and on a regression (a resolved issue seen
again, or an ignored one past its "until"). Repeats never alert, and a
regression within the cooldown of the issue's last alert stays quiet.
Per project: new and/or regression, a minimum level, and environments."""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .models import Alert, Issue, Monitor, Project, level_rank
from .settings import Settings
from .util import utcnow

log = logging.getLogger("foxray.alerts")

KIND_LABELS = {
    "new_issue": "New issue",
    "regression": "Regression",
    "unignored": "Back after ignore",
    "monitor_error": "Monitor failed",
    "monitor_missed": "Monitor missed a check-in",
    "monitor_timeout": "Monitor timed out",
    "store_full": "Storage full",
}


class Links:
    """Where things live in the UI, for the links in alerts."""

    def __init__(self, base_url: str = ""):
        self.base_url = (base_url or "").rstrip("/")

    def issue(self, issue_id: int) -> str:
        return f"{self.base_url}/issues/{issue_id}"

    def monitor(self, monitor_id: int) -> str:
        return f"{self.base_url}/monitors/{monitor_id}"

    def storage(self) -> str:
        return f"{self.base_url}/storage"


@dataclass
class AlertDraft:
    kind: str
    subject: str
    body: str
    payload: dict = field(default_factory=dict)
    issue_id: int | None = None
    monitor_id: int | None = None


def _split(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def should_alert_issue(project: Project, settings: Settings, issue: Issue, kind: str,
                       environment: str, now: datetime) -> bool:
    if kind == "new_issue" and not project.alert_new:
        return False
    if kind in ("regression", "unignored") and not project.alert_regression:
        return False
    if project.alert_min_level and level_rank(issue.level) < level_rank(project.alert_min_level):
        return False
    envs = _split(project.alert_environments)
    if envs and environment not in envs:
        return False
    if kind != "new_issue" and issue.last_alerted_at is not None:
        if now - issue.last_alerted_at < timedelta(seconds=settings.alert_cooldown_seconds):
            return False
    return True


def issue_draft(project: Project, issue: Issue, kind: str, ev: dict, links: Links) -> AlertDraft:
    label = KIND_LABELS.get(kind, kind)
    url = links.issue(issue.id)
    env = ev.get("environment") or ""
    lines = [
        f"{label} in {project.name}: {issue.title}",
        "",
        f"Where:       {issue.culprit or '-'}",
        f"Level:       {issue.level}",
        f"Environment: {env or '-'}",
        f"Release:     {ev.get('release') or '-'}",
        f"Seen:        {issue.times_seen} time(s), first {issue.first_seen:%Y-%m-%d %H:%M} UTC",
        "",
        url,
    ]
    payload = {
        "kind": kind, "project": project.name, "project_id": project.id, "issue_id": issue.id,
        "title": issue.title, "culprit": issue.culprit, "level": issue.level, "environment": env,
        "release": ev.get("release") or "", "times_seen": issue.times_seen,
        "first_seen": issue.first_seen.isoformat(), "last_seen": issue.last_seen.isoformat(),
        "event_id": ev.get("event_id"), "url": url,
    }
    return AlertDraft(kind, f"[Foxray] {label}: {issue.title}"[:300], "\n".join(lines), payload, issue_id=issue.id)


def monitor_draft(project: Project, monitor: Monitor, kind: str, links: Links, detail: str = "") -> AlertDraft:
    label = KIND_LABELS.get(kind, kind)
    url = links.monitor(monitor.id)
    schedule = f"{monitor.schedule_type} {monitor.schedule_value} {monitor.schedule_unit}".strip()
    body = "\n".join([f"{label} in {project.name}: {monitor.slug}", "", f"Schedule: {schedule or '-'}",
                      detail, "", url])
    payload = {"kind": kind, "project": project.name, "project_id": project.id, "monitor_id": monitor.id,
               "monitor": monitor.slug, "schedule": schedule, "detail": detail, "url": url}
    return AlertDraft(kind, f"[Foxray] {label}: {monitor.slug}", body, payload, monitor_id=monitor.id)


def store_full_draft(size: int, cap: int, links: Links) -> AlertDraft:
    mb = lambda n: f"{n / 1048576:.1f} MB"  # noqa: E731
    body = (f"Foxray's store is at {mb(size)}, over its cap of {mb(cap)}, even after removing old event "
            f"bodies.\n\nIt is now counting only: issues are still grouped, counted and alerted, but new event "
            f"bodies are not stored until there is room again.\n\n{links.storage()}")
    return AlertDraft("store_full", "[Foxray] Storage full: counting only", body,
                      {"kind": "store_full", "size": size, "cap": cap, "url": links.storage()})


def queue_alert(store, project: Project | None, draft: AlertDraft, channels: list[str], now: datetime) -> None:
    for channel in channels or ["log"]:
        store.add_alert(project_id=project.id if project else None, kind=draft.kind, channel=channel,
                        subject=draft.subject, body=draft.body, payload=draft.payload, now=now,
                        issue_id=draft.issue_id, monitor_id=draft.monitor_id)


def post_webhook(url: str, alert: Alert, timeout: float = 10) -> str:
    try:
        payload = json.loads(alert.payload)
    except ValueError:
        payload = {}
    payload.setdefault("subject", alert.subject)
    payload.setdefault("text", alert.body)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "foxray"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - admin-configured URL
        if resp.status >= 300:
            raise RuntimeError(f"webhook answered {resp.status}")
        return f"webhook {resp.status}"


def webhook_sender(default_url: str):
    def send(alert: Alert, project: Project | None) -> str:
        url = (project.alert_webhook if project else "") or default_url
        if not url:
            return "no webhook URL"
        return post_webhook(url, alert)
    return send


Sender = Callable[[Alert, "Project | None"], "str | None"]


class Outbox:
    """Sends pending alerts. Safe to run in several processes at once: each
    alert is claimed with a conditional UPDATE before it is sent."""

    def __init__(self, store, settings: Settings, senders: dict[str, Sender], clock=utcnow):
        self.store = store
        self.settings = settings
        self.senders = senders
        self.clock = clock
        self._running = threading.Lock()

    @staticmethod
    def backoff(attempts: int) -> timedelta:
        return timedelta(seconds=min(3600, 60 * (2 ** min(attempts, 6))))

    def flush(self, now: datetime | None = None, limit: int = 50) -> dict:
        now = now or self.clock()
        sent = failed = 0
        give_up = timedelta(days=self.settings.failed_alert_retention_days)
        projects: dict = {}
        for alert in self.store.due_alerts(now, limit):
            if now - alert.created_at > give_up:
                self.store.mark_alert_error(alert.id, alert.last_error or "gave up", failed=True)
                failed += 1
                continue
            if not self.store.claim_alert(alert.id, now, now + self.backoff(alert.attempts)):
                continue
            if alert.project_id is not None and alert.project_id not in projects:
                projects[alert.project_id] = self.store.get_project(alert.project_id)
            project = projects.get(alert.project_id)
            sender = self.senders.get(alert.channel)
            try:
                if sender is None:
                    log.warning("alert (no channel configured): %s", alert.subject)
                    note = "logged only: no email or webhook configured"
                else:
                    note = sender(alert, project) or ""
                self.store.mark_alert_sent(alert.id, self.clock(), note)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - any failure means retry later
                log.warning("alert %s via %s failed: %s", alert.id, alert.channel, exc)
                self.store.mark_alert_error(alert.id, f"{type(exc).__name__}: {exc}")
                failed += 1
        return {"sent": sent, "failed": failed}

    def kick(self, on_thread_exit: Callable | None = None) -> None:
        """Send due alerts on a short background thread (one per process)."""
        if not self._running.acquire(blocking=False):
            return

        def run():
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("sending alerts failed")
            finally:
                self._running.release()
                if on_thread_exit:
                    on_thread_exit()

        threading.Thread(target=run, name="foxray-alerts", daemon=True).start()
