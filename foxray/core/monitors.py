"""Cron monitors from ``check_in`` items.

A check-in creates the monitor if needed and updates its schedule from
``monitor_config``. After each check-in the next deadline is the next
scheduled run plus the margin. ``check_monitors`` (run by tick, or by
ingest at most once a minute) marks monitors that passed their deadline as
missed, and in-progress check-ins older than ``max_runtime`` as timed out.
A monitor alerts when it goes from healthy to failing, not on every
failure after that."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from .alerts import Links, monitor_draft, queue_alert
from .cron import ScheduleError, next_expected, parse_schedule, zone
from .models import MONITOR_FAILING, CheckIn, Project
from .util import UTC, clip, new_id, normalize_event_id

STATUSES = ("in_progress", "ok", "error")
_SLUG = re.compile(r"[^a-z0-9_-]+")


def clean_slug(value) -> str:
    if not isinstance(value, str):
        return ""
    return _SLUG.sub("-", value.strip().lower()).strip("-")[:64]


def _int(value, lo: int, hi: int):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _deadline(monitor, after: datetime) -> datetime | None:
    nxt = next_expected(monitor.schedule_type, monitor.schedule_value, monitor.schedule_unit,
                        monitor.timezone, after)
    return None if nxt is None else nxt + timedelta(minutes=max(1, monitor.checkin_margin))


def process_checkin(store, project: Project, payload: dict, now: datetime, *, links: Links,
                    channels: list[str]) -> tuple[CheckIn | None, bool]:
    slug = clean_slug(payload.get("monitor_slug"))
    if not slug:
        return None, False
    status = payload.get("status") if payload.get("status") in STATUSES else "error"
    check_in_id = normalize_event_id(payload.get("check_in_id")) or new_id()
    duration = payload.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
    environment = clip(payload.get("environment") or "", 64) or ""
    release = clip(payload.get("release") or "", 200) or ""
    config = payload.get("monitor_config")
    alerted = False
    with store.atomic():
        monitor = store.get_monitor(project.id, slug, for_update=True)
        if monitor is None:
            store.insert_monitor(project.id, slug, now)
            monitor = store.get_monitor(project.id, slug, for_update=True)
        updates: dict = {}
        if isinstance(config, dict):
            try:
                kind, value, unit = parse_schedule(config.get("schedule"))
                updates.update(schedule_type=kind, schedule_value=value, schedule_unit=unit)
            except ScheduleError:
                pass
            margin = _int(config.get("checkin_margin"), 0, 1440 * 7)
            if margin is not None:
                updates["checkin_margin"] = margin
            runtime = _int(config.get("max_runtime"), 1, 1440 * 7)
            if runtime is not None:
                updates["max_runtime"] = runtime
            tz = config.get("timezone")
            if isinstance(tz, str) and tz and zone(tz) is not UTC:
                updates["timezone"] = tz[:64]
        monitor = replace(monitor, **updates)
        checkin = store.upsert_checkin(project_id=project.id, monitor_id=monitor.id, check_in_id=check_in_id,
                                       status=status, duration=duration, environment=environment,
                                       release=release, now=now)
        updates["last_checkin_at"] = now
        # A little slack so a run that starts a few seconds early still counts for its slot.
        updates["next_due_at"] = _deadline(monitor, now + timedelta(seconds=30))
        previous = monitor.status
        if status in ("ok", "error"):
            updates["status"] = status
        if status == "error" and previous not in MONITOR_FAILING and monitor.active:
            draft = monitor_draft(project, monitor, "monitor_error", links,
                                  f"The job reported an error (check-in {check_in_id}).")
            queue_alert(store, project, draft, channels, now)
            updates["last_alerted_at"] = now
            alerted = True
        store.update_monitor(monitor.id, **updates)
    return checkin, alerted


def check_monitors(store, now: datetime, *, links: Links, channels_for) -> int:
    """Mark missed and timed-out runs. Returns how many alerts were queued."""
    alerts = 0
    projects: dict = {}

    def project_of(pid):
        if pid not in projects:
            projects[pid] = store.get_project(pid)
        return projects[pid]

    for monitor in store.due_monitors(now):
        project = project_of(monitor.project_id)
        if project is None:
            continue
        with store.atomic():
            if not store.claim_missed(monitor.id, now, _deadline(monitor, now)):
                continue  # another tick got there first
            store.upsert_checkin(project_id=project.id, monitor_id=monitor.id, check_in_id=new_id(),
                                 status="missed", duration=None, environment="", release="", now=now)
            fields: dict = {"status": "missed"}
            if monitor.status not in MONITOR_FAILING:
                due = monitor.next_due_at
                detail = f"No check-in by {due:%Y-%m-%d %H:%M} UTC." if due else "No check-in on time."
                queue_alert(store, project, monitor_draft(project, monitor, "monitor_missed", links, detail),
                            channels_for(project), now)
                fields["last_alerted_at"] = now
                alerts += 1
            store.update_monitor(monitor.id, **fields)

    for checkin in store.in_progress_checkins():
        monitor = store.get_monitor_by_id(checkin.monitor_id)
        if monitor is None or checkin.received_at + timedelta(minutes=max(1, monitor.max_runtime)) > now:
            continue
        project = project_of(monitor.project_id)
        with store.atomic():
            if not store.set_checkin_status_if(checkin.id, "in_progress", "timeout", now):
                continue
            current = store.get_monitor_by_id(monitor.id)
            # Only the latest run decides the monitor's state.
            if current.last_checkin_at and current.last_checkin_at > checkin.updated_at:
                continue
            fields = {"status": "timeout"}
            if current.status not in MONITOR_FAILING and current.active and project is not None:
                detail = f"A run started {checkin.received_at:%Y-%m-%d %H:%M} UTC and did not finish " \
                         f"within {current.max_runtime} minutes."
                queue_alert(store, project, monitor_draft(project, current, "monitor_timeout", links, detail),
                            channels_for(project), now)
                fields["last_alerted_at"] = now
                alerts += 1
            store.update_monitor(current.id, **fields)
    return alerts
