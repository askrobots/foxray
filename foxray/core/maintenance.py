"""Cleanup, the size cap, and the periodic jobs.

Nothing here needs cron. After an ingest request is answered, the request
that notices a job is due runs it on a short thread: cleanup at most every
10 minutes, the monitor check at most every minute, and pending alerts when
there are any. ``foxray tick`` (or ``manage.py foxray_tick``) runs the same
jobs from cron if you want them on a clock. Each job is claimed with a lease
in ``foxray_state``, so two never overlap, across processes too.

Cleanup removes, in small batches:
- event bodies older than the retention (30 days by default, per project);
- event bodies past the newest N per issue (100; the issue keeps its counts);
- resolved/ignored issues not seen for 90 days, with their daily counts;
- daily counts older than 400 days;
- check-ins past 30 days or the newest 500 per monitor;
- sent alerts after 30 days, unsent ones after 7 days of retries;
- rate-limit windows and filter counters after 2 days.
Then the size cap: over 90% the oldest bodies go until it is under 80%; if
it is still over 100% Foxray counts only (no new bodies) until there is
room, and says so once. SQLite space goes back to the disk with an
incremental vacuum and a WAL checkpoint; Postgres reuses it after autovacuum.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta

from .alerts import Links, queue_alert, store_full_draft
from .monitors import check_monitors
from .settings import Settings
from .util import day_str, utcnow

log = logging.getLogger("foxray.maintenance")
MB = 1024 * 1024


class Maintenance:
    def __init__(self, store, settings: Settings, *, links: Links | None = None,
                 global_channels: list[str] | None = None, clock=utcnow, before_batch=None):
        self.store = store
        self.settings = settings
        self.links = links or Links(settings.base_url)
        self.global_channels = global_channels or ["log"]
        self.clock = clock
        self.before_batch = before_batch  # test hook

    def run(self, now: datetime | None = None, *, force: bool = False, budget: float | None = None) -> dict | None:
        """One cleanup pass, unless another is running (or, without
        ``force``, one ran within the interval). Returns what it did."""
        now = now or self.clock()
        budget = self.settings.cleanup_budget_seconds if budget is None else budget
        lease = now + timedelta(seconds=max(300.0, budget * 4))
        ran_before = None if force else now - timedelta(seconds=self.settings.cleanup_interval_seconds)
        if not self.store.try_lease("cleanup", now, lease, ran_before=ran_before):
            return None
        result: dict = {"started": now.isoformat()}
        try:
            if self.before_batch:
                self.before_batch()
            result.update(self._run(now, budget))
        except Exception as exc:  # noqa: BLE001 - cleanup must never break ingest
            log.exception("cleanup failed")
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.store.finish_lease("cleanup", now, json.dumps(result, default=str))
        return result

    # ------------------------------------------------------------------

    def _run(self, now: datetime, budget: float) -> dict:
        s, store = self.settings, self.store
        t0 = time.monotonic()
        deadline = t0 + budget
        batch = max(1, s.cleanup_batch)
        removed: dict[str, int] = {}

        def more() -> bool:
            return time.monotonic() < deadline

        def drain(name: str, fn) -> None:
            while more():
                n = fn()
                if n:
                    removed[name] = removed.get(name, 0) + n
                if n < batch:
                    return

        projects = store.list_projects()
        by_id = {p.id: p for p in projects}

        for p in projects:
            days = s.project_value(p, "event_retention_days")
            if days and days > 0:
                cutoff = now - timedelta(days=days)
                drain("events_expired", lambda: store.delete_events_before(p.id, cutoff, batch))

        # Newest N bodies per issue: only issues seen since the last pass can have grown.
        cursor_text = store.get_state_value("trim_cursor", "")
        cursor = datetime.fromisoformat(cursor_text) if cursor_text else None
        page = 500
        while more():
            start = cursor
            rows = store.issues_seen_since(cursor, page)
            if len(rows) == page and rows[-1][1] == start:
                cursor = now - timedelta(seconds=1)  # a page of identical times; move on
                break
            for issue_id, last_seen in rows:
                if not more():
                    break
                issue = store.get_issue(issue_id)
                keep = s.project_value(by_id.get(issue.project_id) if issue else None, "events_per_issue")
                if keep and keep > 0:
                    n = store.trim_issue_events(issue_id, keep, batch * 5)
                    if n:
                        removed["events_over_issue_cap"] = removed.get("events_over_issue_cap", 0) + n
                cursor = last_seen
            if len(rows) < page:
                cursor = now - timedelta(seconds=1)
                break
        if cursor is not None:
            store.set_state_value("trim_cursor", cursor.isoformat())

        for p in projects:
            days = s.project_value(p, "issue_retention_days")
            if days and days > 0:
                cutoff = now - timedelta(days=days)
                while more():
                    ids = store.stale_closed_issues(p.id, cutoff, 200)
                    if not ids:
                        break
                    for key, n in store.delete_issues(ids).items():
                        removed[f"closed_{key}"] = removed.get(f"closed_{key}", 0) + n

        if s.day_count_retention_days > 0:
            old_day = day_str(now - timedelta(days=s.day_count_retention_days))
            drain("issue_days_expired", lambda: store.delete_days_before(old_day, batch))

        checkin_cutoff = now - timedelta(days=s.checkin_retention_days)
        for m in store.list_monitors():
            if not more():
                break
            n = store.trim_checkins(m.id, checkin_cutoff, s.checkins_per_monitor, batch)
            if n:
                removed["checkins"] = removed.get("checkins", 0) + n

        sent_before = now - timedelta(days=s.sent_alert_retention_days)
        unsent_before = now - timedelta(days=s.failed_alert_retention_days)
        drain("alerts", lambda: store.delete_alerts_before(sent_before, unsent_before, batch))

        window = math.floor((now - timedelta(days=s.counter_retention_days)).timestamp() / 60)
        drain("rate_windows", lambda: store.delete_rate_before(window, batch))
        counter_day = day_str(now - timedelta(days=s.counter_retention_days))
        drain("counters", lambda: store.delete_counters_before(counter_day, batch))

        if removed:
            store.reclaim()
        cap = self._enforce_cap(now, more, batch, removed)
        cap["removed"] = removed
        cap["seconds"] = round(time.monotonic() - t0, 3)
        cap["complete"] = more()
        return cap

    # ------------------------------------------------------------------

    def used_bytes(self) -> int:
        """What counts against the cap. SQLite: the file's pages minus free
        pages. Postgres: the tables' size, less space freed by our last prune
        that new rows have not used yet (Postgres keeps freed space in the
        table for reuse instead of giving it back)."""
        store = self.store
        size = store.db_size()
        if store.dialect == "sqlite":
            return max(0, size - store.free_bytes())
        try:
            pg = json.loads(store.get_state_value("pg_space", "{}"))
        except ValueError:
            pg = {}
        freed = float(pg.get("freed", 0))
        grown = max(0, size - float(pg.get("size_at_prune", size)))
        return int(max(0, size - max(0.0, freed - grown)))

    def _enforce_cap(self, now: datetime, more, batch: int, removed: dict) -> dict:
        s, store = self.settings, self.store
        cap = int(s.max_db_mb * MB)
        before = self.used_bytes()
        used = before
        if cap > 0 and used > 0.9 * cap:
            if store.dialect == "sqlite":
                while used > 0.8 * cap and more():
                    n = store.delete_oldest_events(batch)
                    removed["events_for_space"] = removed.get("events_for_space", 0) + n
                    store.reclaim()
                    used = self.used_bytes()
                    if n == 0:
                        break
            else:
                avg = store.avg_event_bytes() + 300  # row and index overhead, roughly
                want = math.ceil((used - 0.8 * cap) / avg)
                deleted = 0
                while deleted < want and more():
                    n = store.delete_oldest_events(min(batch, want - deleted))
                    deleted += n
                    if n == 0:
                        break
                if deleted:
                    removed["events_for_space"] = removed.get("events_for_space", 0) + deleted
                    size = store.db_size()
                    store.set_state_value("pg_space", json.dumps({"size_at_prune": size, "freed": deleted * avg}))
                    used = self.used_bytes()
        mode = store.get_state_value("mode", "normal")
        new_mode = mode
        if cap > 0 and used > cap:
            new_mode = "counts_only"
        elif mode == "counts_only" and (cap <= 0 or used < 0.9 * cap):
            new_mode = "normal"
        if new_mode != mode:
            store.set_state_value("mode", new_mode)
            if new_mode == "counts_only":
                log.warning("store is full (%d of %d bytes): counting only", used, cap)
                queue_alert(store, None, store_full_draft(used, cap, self.links), self.global_channels, now)
            else:
                log.info("store has room again (%d of %d bytes)", used, cap)
        return {"used_before": before, "used_after": used, "cap": cap, "mode": new_mode,
                "file_bytes": store.db_size()}


class Housekeeper:
    """Decides, after an ingest request, whether a job is due, and runs the
    due ones. Also ``tick``, for cron and the standalone server's clock."""

    def __init__(self, store, settings: Settings, *, maintenance: Maintenance, outbox=None,
                 links: Links | None = None, channels_for=None, clock=utcnow):
        self.store = store
        self.settings = settings
        self.maintenance = maintenance
        self.outbox = outbox
        self.links = links or Links(settings.base_url)
        self.channels_for = channels_for or (lambda project: ["log"])
        self.clock = clock

    @staticmethod
    def _due(state, now: datetime, seconds: float) -> bool:
        if state is None or state.last_run_at is None:
            return True
        if state.lease_until is not None and state.lease_until > now:
            return False
        return state.last_run_at <= now - timedelta(seconds=seconds)

    def after_ingest(self, states: dict, now: datetime, alerted: bool):
        s = self.settings
        cleanup = self._due(states.get("cleanup"), now, s.cleanup_interval_seconds)
        monitors = self._due(states.get("monitors"), now, s.monitor_check_seconds)
        if not (cleanup or monitors or alerted):
            return None

        def run():
            self.run_due(cleanup=cleanup, monitors=monitors)
        return run

    def check_monitors(self, now: datetime | None = None, *, force: bool = False) -> int | None:
        now = now or self.clock()
        ran_before = None if force else now - timedelta(seconds=self.settings.monitor_check_seconds)
        if not self.store.try_lease("monitors", now, now + timedelta(seconds=300), ran_before=ran_before):
            return None
        try:
            return check_monitors(self.store, now, links=self.links, channels_for=self.channels_for)
        finally:
            self.store.finish_lease("monitors", now)

    def run_due(self, *, cleanup: bool = True, monitors: bool = True, force: bool = False,
                budget: float | None = None) -> dict:
        now = self.clock()
        out: dict = {}
        try:
            if monitors or force:
                out["monitor_alerts"] = self.check_monitors(now, force=force)
            if cleanup or force:
                out["cleanup"] = self.maintenance.run(now, force=force, budget=budget)
            if self.outbox is not None:
                out["alerts"] = self.outbox.flush()
        except Exception:  # noqa: BLE001 - never raise into ingest
            log.exception("housekeeping failed")
        return out

    def tick(self, *, force_cleanup: bool = False) -> dict:
        """Monitors always; cleanup when due (or forced); then send alerts."""
        now = self.clock()
        out: dict = {"monitor_alerts": self.check_monitors(now, force=True)}
        out["cleanup"] = self.maintenance.run(now, force=force_cleanup, budget=self.settings.tick_budget_seconds)
        if self.outbox is not None:
            out["alerts"] = self.outbox.flush()
        return out


def storage_report(store, settings: Settings, maintenance: Maintenance | None = None) -> dict:
    maintenance = maintenance or Maintenance(store, settings)
    states = store.states()
    cleanup = states.get("cleanup")
    try:
        last = json.loads(cleanup.value) if cleanup and cleanup.value else {}
    except ValueError:
        last = {}
    cap = int(settings.max_db_mb * MB)
    used = maintenance.used_bytes()
    return {
        "dialect": store.dialect,
        "file_bytes": store.db_size(),
        "used_bytes": used,
        "free_bytes": store.free_bytes(),
        "cap_bytes": cap,
        "percent": round(100.0 * used / cap, 1) if cap else None,
        "mode": store.get_state_value("mode", "normal"),
        "tables": store.table_counts(),
        "last_cleanup_at": cleanup.last_run_at if cleanup else None,
        "last_cleanup": last,
        "pending_alerts": store.pending_alert_count(),
    }
