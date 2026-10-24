"""Plain data classes for what the store holds.

Field names are the column names, in table order, so the SQL store can map
rows to objects generically. Datetimes are timezone-aware UTC."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime

LEVELS = ("debug", "info", "warning", "error", "fatal")
ISSUE_STATUSES = ("unresolved", "resolved", "ignored")
MONITOR_FAILING = ("error", "missed", "timeout")


def level_rank(level: str | None) -> int:
    try:
        return LEVELS.index(level or "error")
    except ValueError:
        return LEVELS.index("error")


@dataclass
class Project:
    id: int
    name: str
    public_key: str
    created_at: datetime
    rate_limit: int | None = None
    sample_after: int | None = None
    event_retention_days: int | None = None
    events_per_issue: int | None = None
    issue_retention_days: int | None = None
    alert_new: bool = True
    alert_regression: bool = True
    alert_min_level: str = ""
    alert_environments: str = ""
    alert_emails: str = ""
    alert_webhook: str = ""
    filter_crawlers: bool = True
    filter_js_noise: bool = False
    filter_health_checks: bool = True
    health_check_names: str = ""


@dataclass
class Issue:
    id: int
    project_id: int
    fingerprint: str
    title: str
    culprit: str
    level: str
    platform: str
    status: str
    first_seen: datetime
    last_seen: datetime
    times_seen: int
    users_seen: int
    last_environment: str
    last_release: str
    last_alerted_at: datetime | None = None
    resolved_at: datetime | None = None
    reopened_at: datetime | None = None
    ignore_until: datetime | None = None
    ignore_until_count: int | None = None


@dataclass
class Event:
    id: int
    project_id: int
    issue_id: int
    event_id: str
    received_at: datetime
    occurred_at: datetime | None
    level: str
    title: str
    environment: str
    release: str
    user_key: str
    data: str

    @property
    def payload(self) -> dict:
        try:
            return json.loads(self.data)
        except (TypeError, ValueError):
            return {}


@dataclass
class Monitor:
    id: int
    project_id: int
    slug: str
    name: str
    schedule_type: str
    schedule_value: str
    schedule_unit: str
    timezone: str
    checkin_margin: int
    max_runtime: int
    status: str
    active: bool
    last_checkin_at: datetime | None = None
    next_due_at: datetime | None = None
    last_alerted_at: datetime | None = None
    created_at: datetime | None = None


@dataclass
class CheckIn:
    id: int
    project_id: int
    monitor_id: int
    check_in_id: str
    status: str
    duration: float | None
    environment: str
    release: str
    received_at: datetime
    updated_at: datetime


@dataclass
class FilterRule:
    id: int
    project_id: int
    kind: str
    pattern: str
    is_regex: bool
    environment: str
    release: str
    enabled: bool
    note: str
    created_at: datetime


@dataclass
class Alert:
    id: int
    project_id: int | None
    kind: str
    channel: str
    issue_id: int | None
    monitor_id: int | None
    subject: str
    body: str
    payload: str
    status: str
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    sent_at: datetime | None = None
    last_error: str = ""


@dataclass
class State:
    name: str
    last_run_at: datetime | None
    lease_until: datetime | None
    value: str


def column_names(cls) -> list[str]:
    return [f.name for f in fields(cls)]


def column_kinds(cls) -> dict[str, str]:
    """'dt' for datetime columns, 'bool' for booleans, '' otherwise."""
    out = {}
    for f in fields(cls):
        t = str(f.type)
        out[f.name] = "dt" if "datetime" in t else ("bool" if t.startswith("bool") else "")
    return out
