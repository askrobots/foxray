"""Cron monitors: check-ins, schedules, misses, timeouts, alerts."""

import uuid

from tests.helpers import auth_headers, envelope, make_stack


def checkin(ing, p, slug, status, config=None, check_in_id=None, duration=None):
    payload = {"check_in_id": check_in_id or uuid.uuid4().hex, "monitor_slug": slug, "status": status,
               "environment": "production"}
    if duration is not None:
        payload["duration"] = duration
    if config:
        payload["monitor_config"] = config
    r = ing.handle(p.id, "envelope", auth_headers(p), {}, envelope(({"type": "check_in"}, payload)))
    assert r.status == 200
    return payload["check_in_id"]


HOURLY = {"schedule": {"type": "crontab", "value": "0 * * * *"}, "checkin_margin": 5, "max_runtime": 10}


def alert_kinds(store):
    return [a.kind for a in reversed(store.list_alerts())]


def test_checkins_create_monitor_and_update_in_place(sqlite_store):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    cid = checkin(ing, p, "Nightly Backup", "in_progress", HOURLY)
    clock.advance(seconds=42)
    checkin(ing, p, "nightly-backup", "ok", check_in_id=cid, duration=42.0)
    m = store.get_monitor(p.id, "nightly-backup")
    assert (m.schedule_type, m.schedule_value, m.checkin_margin, m.max_runtime) == ("crontab", "0 * * * *", 5, 10)
    assert m.status == "ok"
    rows = store.list_checkins(m.id)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].duration == 42.0
    # 12:00:42 -> next run 13:00, deadline 13:05
    assert m.next_due_at.strftime("%H:%M") == "13:05"


def test_missed_schedule_alerts_once_until_recovery(sqlite_store):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    checkin(ing, p, "hourly", "ok", HOURLY)
    clock.advance(minutes=64)  # 13:04: inside the margin
    assert hk.check_monitors(force=True) == 0
    clock.advance(minutes=2)   # 13:06: missed
    assert hk.check_monitors(force=True) == 1
    m = store.get_monitor(p.id, "hourly")
    assert m.status == "missed" and m.next_due_at.strftime("%H:%M") == "14:05"
    assert [c.status for c in store.list_checkins(m.id)][0] == "missed"
    clock.advance(hours=1)     # 14:06: missed again, already failing: no new alert
    assert hk.check_monitors(force=True) == 0
    assert store.get_monitor(p.id, "hourly").status == "missed"
    checkin(ing, p, "hourly", "ok")
    assert store.get_monitor(p.id, "hourly").status == "ok"
    assert alert_kinds(store) == ["monitor_missed"]


def test_interval_schedule(sqlite_store):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    checkin(ing, p, "every-10", "ok", {"schedule": {"type": "interval", "value": 10, "unit": "minute"},
                                       "checkin_margin": 1})
    clock.advance(minutes=11)
    assert hk.check_monitors(force=True) == 0
    clock.advance(minutes=1)
    assert hk.check_monitors(force=True) == 1


def test_error_checkin_alerts_on_transition_only(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store)
    p = store.create_project("app")
    checkin(ing, p, "job", "ok", HOURLY)
    checkin(ing, p, "job", "error")
    checkin(ing, p, "job", "error")
    assert alert_kinds(store) == ["monitor_error"]
    checkin(ing, p, "job", "ok")
    checkin(ing, p, "job", "error")
    assert alert_kinds(store) == ["monitor_error", "monitor_error"]


def test_in_progress_past_max_runtime_times_out(sqlite_store):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    checkin(ing, p, "slow", "in_progress", HOURLY)
    clock.advance(minutes=9)
    hk.check_monitors(force=True)
    assert store.get_monitor(p.id, "slow").status != "timeout"
    clock.advance(minutes=2)
    hk.check_monitors(force=True)
    m = store.get_monitor(p.id, "slow")
    assert m.status == "timeout" and store.list_checkins(m.id)[0].status == "timeout"
    assert alert_kinds(store) == ["monitor_timeout"]


def test_monitor_without_schedule_never_misses(sqlite_store):
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    checkin(ing, p, "adhoc", "ok")
    clock.advance(days=3)
    assert hk.check_monitors(force=True) == 0
    assert store.get_monitor(p.id, "adhoc").next_due_at is None


def test_two_checks_at_once_claim_a_miss_once(sqlite_store):
    import threading
    store = sqlite_store
    ing, hk, _, _, clock = make_stack(store)
    p = store.create_project("app")
    for i in range(5):
        checkin(ing, p, f"m{i}", "ok", HOURLY)
    clock.advance(hours=2)
    from foxray.core.monitors import check_monitors
    results = []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(check_monitors(store, clock(), links=hk.links, channels_for=lambda p: ["test"]))
    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 5
    assert len(store.list_alerts()) == 5
