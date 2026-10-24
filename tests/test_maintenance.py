"""Cleanup, the size cap, leases and giving space back."""

import json
import os
import threading
import time
from datetime import timedelta

from foxray.core.maintenance import Maintenance, storage_report
from foxray.core.settings import Settings
from tests.helpers import auth_headers, envelope, make_event, make_stack


def send(ing, p, ev):
    r = ing.handle(p.id, "envelope", auth_headers(p), {}, envelope(({"type": "event"}, ev)))
    assert r.status == 200
    return r


def big_event(i, size=3000, **kw):
    return make_event(value=f"v{i}", extra={"blob": "x" * 900, "more": ["y" * 900] * 3}, **kw)


def test_retention_limits(sqlite_store):
    store = sqlite_store
    s = Settings(events_per_issue=3, rate_limit_per_minute=0, sample_after=0)
    ing, hk, _, _, clock = make_stack(store, s)
    p = store.create_project("app")
    for i in range(10):
        send(ing, p, make_event())
    other = make_event("TypeError")
    send(ing, p, other)
    m = Maintenance(store, s, clock=clock)
    res = m.run(force=True)
    issues = {i.title: i for i in store.list_issues()[0]}
    keep = issues["ValueError: boom"]
    assert store.count_events(keep.id) == 3 and keep.times_seen == 10
    assert res["removed"]["events_over_issue_cap"] == 7

    # bodies past the retention go; the issue and its counts stay
    clock.advance(days=31)
    send(ing, p, make_event("KeyError"))
    m.run(force=True)
    assert store.count_events(keep.id) == 0
    assert store.get_issue(keep.id).times_seen == 10
    assert sum(n for _, n in store.issue_days(keep.id)) == 10

    # resolved and quiet for 90 days: the issue and its daily counts go
    store.set_issue_status(keep.id, "resolved", now=clock())
    clock.advance(days=91)
    m.run(force=True)
    assert store.get_issue(keep.id) is None and store.issue_days(keep.id) == []
    assert store.get_issue(issues["TypeError: boom"].id) is not None  # unresolved issues stay


def test_counters_rate_windows_alerts_and_checkins_are_trimmed(sqlite_store):
    store = sqlite_store
    s = Settings(checkins_per_monitor=5)
    ing, hk, outbox, _, clock = make_stack(store, s)
    p = store.create_project("app")
    send(ing, p, make_event())
    outbox.flush()
    ing.handle(p.id, "envelope", auth_headers(p), {}, envelope(({"type": "session"}, {})))
    for i in range(8):
        ing.handle(p.id, "envelope", auth_headers(p), {},
                   envelope(({"type": "check_in"}, {"monitor_slug": "job", "status": "ok"})))
    m = Maintenance(store, s, clock=clock)
    m.run(force=True)
    mon = store.get_monitor(p.id, "job")
    assert len(store.list_checkins(mon.id, 100)) == 5
    clock.advance(days=3)
    m.run(force=True)
    counts = store.table_counts()
    assert counts["foxray_counter"] == 0 and counts["foxray_rate"] == 0
    assert counts["foxray_alert"] == 1  # sent, kept 30 days
    clock.advance(days=28)
    m.run(force=True)
    assert store.table_counts()["foxray_alert"] == 0


def _fill(ing, p, n):
    for i in range(n):
        send(ing, p, make_event(f"E{i % 40}", f"v{i}", extra={"blob": "x" * 1000, "b2": "z" * 1000}))


def test_size_cap_prunes_shrinks_the_file_and_counts_survive(sqlite_store):
    store = sqlite_store
    s = Settings(max_db_mb=0.75, rate_limit_per_minute=0, sample_after=0, events_per_issue=10_000,
                 cleanup_budget_seconds=30)
    ing, hk, _, _, clock = make_stack(store, s)
    p = store.create_project("app")
    _fill(ing, p, 600)
    path = store.db.path
    store.reclaim()
    size_before = os.path.getsize(path)
    total_before = sum(i.times_seen for i in store.list_issues(_all())[0])
    assert size_before > 0.9 * s.max_db_mb * 1048576
    res = Maintenance(store, s, clock=clock).run(force=True, budget=30)
    assert res["removed"]["events_for_space"] > 0
    size_after = os.path.getsize(path)
    assert size_after < size_before
    assert res["used_after"] <= 0.8 * s.max_db_mb * 1048576
    assert res["mode"] == "normal"
    assert sum(i.times_seen for i in store.list_issues(_all())[0]) == total_before == 600
    report = storage_report(store, s)
    assert report["mode"] == "normal" and report["last_cleanup"]["removed"]["events_for_space"] > 0


def _all():
    from foxray.core.sql import IssueQuery
    return IssueQuery(status="all", limit=500)


def test_counts_only_mode_when_full_and_back(sqlite_store):
    store = sqlite_store
    # A cap so small that even with no bodies the store is over it.
    s = Settings(max_db_mb=0.05, rate_limit_per_minute=0, sample_after=0)
    ing, hk, outbox, channel, clock = make_stack(store, s)
    p = store.create_project("app")
    _fill(ing, p, 50)
    res = Maintenance(store, s, global_channels=["test"], clock=clock).run(force=True, budget=10)
    assert res["mode"] == "counts_only"
    assert store.get_state_value("mode") == "counts_only"
    # ingest still answers 200, still groups and counts, but keeps no new bodies
    events_before = store.count_events()
    r = send(ing, p, make_event("Fresh"))
    assert r.status == 200
    fresh = [i for i in store.list_issues(_all())[0] if i.title.startswith("Fresh")][0]
    assert fresh.times_seen == 1 and store.count_events() == events_before
    assert {x[2]: x[3] for x in store.counters(p.id)}["counts_only"] == 1
    # one "store is full" alert, not one per pass
    Maintenance(store, s, global_channels=["test"], clock=clock).run(force=True, budget=10)
    assert [a.kind for a in store.list_alerts()].count("store_full") == 1
    # more room: back to normal
    s.max_db_mb = 100
    res = Maintenance(store, s, clock=clock).run(force=True)
    assert res["mode"] == "normal"


def test_two_cleanups_never_overlap(sqlite_store):
    store = sqlite_store
    s = Settings()
    inside = []
    overlap = []
    lock = threading.Lock()

    def slow():
        with lock:
            inside.append(1)
            if len(inside) > 1:
                overlap.append(1)
        time.sleep(0.3)
        with lock:
            inside.pop()

    results = []
    barrier = threading.Barrier(5)

    def run():
        barrier.wait()
        results.append(Maintenance(store, s, before_batch=slow).run(force=True))
    threads = [threading.Thread(target=run) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not overlap
    assert sum(r is not None for r in results) == 1


def test_cleanup_runs_from_ingest_at_most_every_interval(sqlite_store):
    store = sqlite_store
    s = Settings(cleanup_interval_seconds=600)
    ing, hk, _, _, clock = make_stack(store, s)
    p = store.create_project("app")
    r = send(ing, p, make_event())
    assert r.after is not None
    r.after()
    first = store.states()["cleanup"].last_run_at
    clock.advance(minutes=5)
    r = send(ing, p, make_event())
    if r.after:
        r.after()
    assert store.states()["cleanup"].last_run_at == first  # not due yet
    clock.advance(minutes=6)
    r = send(ing, p, make_event())
    r.after()
    assert store.states()["cleanup"].last_run_at == clock()
    assert json.loads(store.states()["cleanup"].value)["mode"] == "normal"


def test_ingest_never_fails_when_cleanup_breaks(sqlite_store, monkeypatch):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(store, "delete_events_before", boom)
    r = send(ing, p, make_event())
    assert r.status == 200
    r.after()
    assert "disk on fire" in json.loads(store.states()["cleanup"].value)["error"]
    assert store.states()["cleanup"].lease_until is None
