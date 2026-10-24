"""The storage contract. Every store (SQLite, Postgres, Django) must pass
all of these, so the two ways of running Foxray behave the same."""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from foxray.core.sql import IssueQuery
from tests.helpers import Clock, auth_headers, close_thread_connections, envelope, make_event, make_stack

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)


def new_issue(store, project, fp="fp1", now=T0, **kw):
    args = dict(title="ValueError: boom", culprit="app.views in index", level="error", platform="python",
                environment="production", release="1.0", now=now)
    args.update(kw)
    return store.insert_issue_if_new(project.id, fp, **args)


def test_projects(store):
    p = store.create_project("web", now=T0, rate_limit=10)
    assert len(p.public_key) == 32
    got = store.get_project(p.id)
    assert got == p and got.created_at == T0 and got.alert_new is True and got.filter_js_noise is False
    store.update_project(p.id, alert_min_level="warning", filter_js_noise=True, sample_after=None)
    got = store.get_project(p.id)
    assert got.alert_min_level == "warning" and got.filter_js_noise is True
    assert store.get_project_by_name("web").id == p.id
    assert store.get_project("nope") is None and store.get_project(99999) is None
    assert [x.name for x in store.list_projects()][-1] == "web"
    with pytest.raises(ValueError):
        store.update_project(p.id, bogus=1)


def test_issue_insert_lock_repeat(store):
    p = store.create_project("web")
    issue = new_issue(store, p)
    assert issue.times_seen == 1 and issue.status == "unresolved" and issue.first_seen == T0
    assert new_issue(store, p) is None  # already there
    with store.atomic():
        cur = store.lock_issue(p.id, "fp1")
        t1 = T0 + timedelta(minutes=5)
        again = store.record_repeat(cur.id, now=t1, title="ValueError: boom 2", culprit="c", level="fatal",
                                    environment="staging", release="1.1", status="unresolved")
    assert again.times_seen == 2 and again.last_seen == t1 and again.first_seen == T0
    assert (again.level, again.last_environment, again.last_release) == ("fatal", "staging", "1.1")


def test_datetimes_round_trip_aware_and_exact(store):
    p = store.create_project("web", now=T0)
    issue = new_issue(store, p, now=T0)
    got = store.get_issue(issue.id)
    assert got.first_seen == T0 and got.first_seen.tzinfo is not None
    store.update_issue(issue.id, ignore_until=T0 + timedelta(days=1, microseconds=1))
    assert store.get_issue(issue.id).ignore_until == T0 + timedelta(days=1, microseconds=1)


def test_counters_are_atomic_under_threads(store):
    p = store.create_project("web")
    seen = []
    lock = threading.Lock()

    def work():
        try:
            for _ in range(25):
                n = store.hit_rate(p.id, 1000)
                store.bump_counter(p.id, "2026-10-01", "dropped:session")
                with lock:
                    seen.append(n)
        finally:
            close_thread_connections()
    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(seen) == list(range(1, 151))
    assert store.counters(p.id) == [(p.id, "2026-10-01", "dropped:session", 150)]
    assert store.hit_rate(p.id, 1001) == 1


def test_concurrent_ingest_groups_into_one_issue(store):
    p = store.create_project("web")
    ing, *_ = make_stack(store, clock=Clock())
    errors = []

    def work():
        try:
            for _ in range(5):
                r = ing.handle(p.id, "envelope", auth_headers(p), {}, envelope(({"type": "event"}, make_event())))
                assert r.status == 200, r.body
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            close_thread_connections()
    threads = [threading.Thread(target=work) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    issues, total = store.list_issues()
    assert total == 1 and issues[0].times_seen == 25
    assert store.count_events(issues[0].id) == 25
    assert [a.kind for a in store.list_alerts()] == ["new_issue"]


def test_events_users_tags_days(store):
    p = store.create_project("web")
    issue = new_issue(store, p)
    eid = "a" * 32
    row = store.insert_event(project_id=p.id, issue_id=issue.id, event_id=eid, received_at=T0, occurred_at=T0,
                             level="error", title="t", environment="production", release="1.0", user_key="u1",
                             data={"event_id": eid, "message": "hi"})
    assert row is not None
    assert store.insert_event(project_id=p.id, issue_id=issue.id, event_id=eid, received_at=T0, occurred_at=None,
                              level="error", title="t", environment="", release="", user_key="", data={}) is None
    ev = store.latest_event(issue.id)
    assert ev.payload["message"] == "hi" and ev.received_at == T0
    assert store.get_event(ev.id).event_id == eid and store.find_event(p.id, eid).id == ev.id
    assert [e.id for e in store.list_events(issue.id)] == [ev.id]
    assert store.add_issue_user(issue.id, "u1") is True
    assert store.add_issue_user(issue.id, "u1") is False
    assert store.add_issue_user(issue.id, "u2") is True
    assert store.get_issue(issue.id).users_seen == 2
    for env in ("production", "production", "staging"):
        store.bump_issue_tag(issue.id, "environment", env, T0)
    assert store.issue_tags(issue.id) == {"environment": [("production", 2), ("staging", 1)]}
    assert store.tag_values("environment") == ["production", "staging"]
    assert store.tag_values("environment", project_id=p.id + 1000) == []
    assert store.bump_issue_day(issue.id, p.id, "2026-10-01") == 1
    assert store.bump_issue_day(issue.id, p.id, "2026-10-01") == 2
    store.bump_issue_day(issue.id, p.id, "2026-10-02", n=5)
    assert store.issue_days(issue.id) == [("2026-10-01", 2), ("2026-10-02", 5)]
    assert store.issue_days(issue.id, since_day="2026-10-02") == [("2026-10-02", 5)]


def test_issue_list_filters_sorts_and_pages(store):
    p = store.create_project("web")
    q = store.create_project("other")
    a = new_issue(store, p, "a", title="KeyError: 'user'", culprit="app.auth in login", now=T0)
    b = new_issue(store, p, "b", title="Timeout 100% reached", culprit="jobs in run", level="warning",
                  now=T0 + timedelta(days=1))
    c = new_issue(store, p, "c", title="ZeroDivisionError", culprit="app.math", now=T0 + timedelta(days=2))
    new_issue(store, q, "a", now=T0)
    store.update_issue(a.id, times_seen=50, users_seen=1)
    store.update_issue(b.id, times_seen=5, users_seen=9)
    store.bump_issue_tag(a.id, "environment", "production", T0)
    store.bump_issue_tag(b.id, "environment", "staging", T0)
    store.bump_issue_tag(b.id, "release", "2.0", T0)
    store.set_issue_status(c.id, "resolved", now=T0)

    def titles(**kw):
        issues, total = store.list_issues(IssueQuery(project_id=p.id, **kw))
        return [i.title.split(":")[0].split(" ")[0] for i in issues], total

    assert titles() == (["Timeout", "KeyError"], 2)
    assert titles(status="all")[0] == ["ZeroDivisionError", "Timeout", "KeyError"]
    assert titles(status="resolved")[0] == ["ZeroDivisionError"]
    assert titles(level="warning")[0] == ["Timeout"]
    assert titles(environment="production")[0] == ["KeyError"]
    assert titles(release="2.0")[0] == ["Timeout"]
    assert titles(text="login")[0] == ["KeyError"]          # culprit
    assert titles(text="KEYERROR")[0] == ["KeyError"]       # case-insensitive
    assert titles(text="100%")[0] == ["Timeout"]            # % is literal
    assert titles(text="%")[0] == ["Timeout"]
    assert titles(text="_")[0] == []
    assert titles(sort="count")[0] == ["KeyError", "Timeout"]
    assert titles(sort="users")[0] == ["Timeout", "KeyError"]
    assert titles(sort="first_seen")[0] == ["Timeout", "KeyError"]
    assert titles(status="all", first_seen_from=T0 + timedelta(hours=1))[0] == ["ZeroDivisionError", "Timeout"]
    assert titles(status="all", first_seen_to=T0 + timedelta(hours=1))[0] == ["KeyError"]
    assert titles(status="all", last_seen_from=T0 + timedelta(days=2))[0] == ["ZeroDivisionError"]
    assert titles(status="all", last_seen_to=T0 + timedelta(days=1, hours=1))[0] == ["Timeout", "KeyError"]
    assert titles(status="all", limit=1, offset=1) == (["Timeout"], 3)
    assert store.list_issues(IssueQuery(status="all"))[1] == 4


def test_issue_status_changes(store):
    p = store.create_project("web")
    issue = new_issue(store, p)
    store.update_issue(issue.id, times_seen=10)
    store.set_issue_status(issue.id, "resolved", now=T0)
    got = store.get_issue(issue.id)
    assert got.status == "resolved" and got.resolved_at == T0
    store.set_issue_status(issue.id, "ignored", now=T0, ignore_more=5)
    got = store.get_issue(issue.id)
    assert got.status == "ignored" and got.ignore_until_count == 15
    until = T0 + timedelta(days=3)
    store.set_issue_status(issue.id, "ignored", now=T0, ignore_until=until)
    got = store.get_issue(issue.id)
    assert got.ignore_until == until and got.ignore_until_count is None
    store.set_issue_status(issue.id, "unresolved", now=T0)
    got = store.get_issue(issue.id)
    assert got.status == "unresolved" and got.ignore_until is None and got.resolved_at is None
    with pytest.raises(ValueError):
        store.set_issue_status(issue.id, "deleted", now=T0)


def test_monitors_and_checkins(store):
    p = store.create_project("web")
    store.insert_monitor(p.id, "job", T0)
    store.insert_monitor(p.id, "job", T0)  # idempotent
    m = store.get_monitor(p.id, "job")
    assert m.status == "new" and m.active and m.checkin_margin == 1 and m.max_runtime == 30
    due = T0 + timedelta(hours=1)
    store.update_monitor(m.id, schedule_type="crontab", schedule_value="0 * * * *", next_due_at=due)
    assert store.get_monitor_by_id(m.id).next_due_at == due
    assert store.due_monitors(T0) == []
    assert [x.id for x in store.due_monitors(due + timedelta(seconds=1))] == [m.id]
    later = due + timedelta(hours=1)
    assert store.claim_missed(m.id, due + timedelta(seconds=1), later) is True
    assert store.claim_missed(m.id, due + timedelta(seconds=1), later) is False
    store.update_monitor(m.id, active=False)
    assert store.due_monitors(later + timedelta(days=1)) == []
    c = store.upsert_checkin(project_id=p.id, monitor_id=m.id, check_in_id="c" * 32, status="in_progress",
                             duration=None, environment="production", release="", now=T0)
    assert c.status == "in_progress"
    assert [x.id for x in store.in_progress_checkins()] == [c.id]
    c2 = store.upsert_checkin(project_id=p.id, monitor_id=m.id, check_in_id="c" * 32, status="ok", duration=3.5,
                              environment="production", release="", now=T0 + timedelta(seconds=4))
    assert c2.id == c.id and c2.status == "ok" and c2.duration == 3.5 and c2.received_at == T0
    assert store.in_progress_checkins() == []
    assert store.set_checkin_status_if(c.id, "in_progress", "timeout", T0) is False
    assert store.set_checkin_status_if(c.id, "ok", "error", T0) is True
    assert [x.status for x in store.list_checkins(m.id)] == ["error"]
    assert [x.slug for x in store.list_monitors(p.id)] == ["job"]


def test_filter_rules(store):
    p = store.create_project("web")
    r = store.add_filter(p.id, "exception", "Http404", note="noise")
    store.add_filter(p.id, "environment", "development", enabled=False)
    assert [f.kind for f in store.list_filters(p.id)] == ["exception", "environment"]
    assert [f.id for f in store.list_filters(p.id, enabled_only=True)] == [r.id]
    store.update_filter(r.id, is_regex=True, pattern="Http40[34]")
    got = store.list_filters(p.id)[0]
    assert got.is_regex is True and got.pattern == "Http40[34]"
    store.delete_filter(r.id)
    assert [f.kind for f in store.list_filters(p.id)] == ["environment"]


def test_alert_outbox(store):
    p = store.create_project("web")
    aid = store.add_alert(project_id=p.id, kind="new_issue", channel="email", subject="s", body="b",
                          payload={"x": 1}, now=T0, issue_id=None)
    store.add_alert(project_id=None, kind="store_full", channel="log", subject="s2", body="b2", payload={},
                    now=T0)
    assert [a.id for a in store.due_alerts(T0)][0] == aid
    retry = T0 + timedelta(minutes=1)
    assert store.claim_alert(aid, T0, retry) is True
    assert store.claim_alert(aid, T0, retry) is False  # someone already has it
    store.mark_alert_error(aid, "smtp down")
    a = [x for x in store.list_alerts() if x.id == aid][0]
    assert a.attempts == 1 and a.last_error == "smtp down" and a.next_attempt_at == retry
    assert store.claim_alert(aid, retry, retry + timedelta(minutes=2)) is True
    store.mark_alert_sent(aid, retry, "ok")
    a = [x for x in store.list_alerts() if x.id == aid][0]
    assert a.status == "sent" and a.sent_at == retry
    assert store.pending_alert_count() == 1
    assert [x.id for x in store.list_alerts(project_id=p.id)] == [aid]


def test_leases_and_state(store):
    assert store.try_lease("cleanup", T0, T0 + timedelta(minutes=5)) is True
    assert store.try_lease("cleanup", T0, T0 + timedelta(minutes=5)) is False
    store.finish_lease("cleanup", T0, '{"ok": 1}')
    st = store.states()["cleanup"]
    assert st.last_run_at == T0 and st.lease_until is None and st.value == '{"ok": 1}'
    # due only when it last ran before the given time
    assert store.try_lease("cleanup", T0, T0 + timedelta(minutes=5), ran_before=T0 - timedelta(minutes=1)) is False
    assert store.try_lease("cleanup", T0, T0 + timedelta(minutes=5), ran_before=T0) is True
    # an expired lease (a crashed holder) can be taken over
    assert store.try_lease("cleanup", T0 + timedelta(minutes=6), T0 + timedelta(minutes=11)) is True
    store.set_state_value("mode", "counts_only")
    store.set_state_value("mode", "normal")
    assert store.get_state_value("mode") == "normal" and store.get_state_value("missing", "d") == "d"


def test_cleanup_primitives(store):
    p = store.create_project("web")
    issue = new_issue(store, p)
    for i in range(10):
        store.insert_event(project_id=p.id, issue_id=issue.id, event_id=f"{i:032x}",
                           received_at=T0 + timedelta(days=i), occurred_at=None, level="error", title="t",
                           environment="", release="", user_key="", data={"i": i})
    assert store.delete_events_before(p.id, T0 + timedelta(days=3), limit=2) == 2
    assert store.delete_events_before(p.id, T0 + timedelta(days=3), limit=2) == 1
    assert store.count_events(issue.id) == 7
    assert store.trim_issue_events(issue.id, keep=4, limit=100) == 3
    assert sorted(e.payload["i"] for e in store.list_events(issue.id, 100)) == [6, 7, 8, 9]
    assert store.delete_oldest_events(1) == 1
    assert store.issues_seen_since(None, 10) == [(issue.id, T0)]
    assert store.issues_seen_since(T0 + timedelta(seconds=1), 10) == []

    store.bump_issue_day(issue.id, p.id, "2025-01-01")
    store.bump_issue_day(issue.id, p.id, "2026-10-01")
    assert store.delete_days_before("2026-01-01", 100) == 1
    store.set_issue_status(issue.id, "resolved", now=T0)
    assert store.stale_closed_issues(p.id, T0 + timedelta(days=1), 10) == [issue.id]
    assert store.stale_closed_issues(p.id, T0, 10) == []
    store.add_issue_user(issue.id, "u")
    store.bump_issue_tag(issue.id, "environment", "x", T0)
    removed = store.delete_issues([issue.id])
    assert removed == {"events": 3, "issue_days": 1, "issues": 1}
    counts = store.table_counts()
    assert counts["foxray_issue_user"] == counts["foxray_issue_tag"] == counts["foxray_event"] == 0

    store.insert_monitor(p.id, "m", T0)
    m = store.get_monitor(p.id, "m")
    for i in range(6):
        store.upsert_checkin(project_id=p.id, monitor_id=m.id, check_in_id=f"{i:032x}", status="ok", duration=None,
                             environment="", release="", now=T0 + timedelta(days=i))
    assert store.trim_checkins(m.id, T0 + timedelta(days=1), keep=3, limit=100) == 3
    assert len(store.list_checkins(m.id)) == 3

    store.add_alert(project_id=p.id, kind="k", channel="log", subject="", body="", payload={}, now=T0)
    sent = store.add_alert(project_id=p.id, kind="k", channel="log", subject="", body="", payload={}, now=T0)
    store.mark_alert_sent(sent, T0)
    assert store.delete_alerts_before(T0, T0 + timedelta(seconds=1), 100) == 1   # the unsent one
    assert store.delete_alerts_before(T0 + timedelta(seconds=1), T0, 100) == 1   # the sent one

    store.hit_rate(p.id, 10)
    store.hit_rate(p.id, 20)
    assert store.delete_rate_before(15, 100) == 1
    store.bump_counter(p.id, "2026-09-01", "x")
    store.bump_counter(p.id, "2026-10-01", "x")
    assert store.delete_counters_before("2026-09-30", 100) == 1

    assert store.db_size() > 0 and store.free_bytes() >= 0
    store.reclaim()
    assert set(store.table_counts()) >= {"foxray_issue", "foxray_event", "foxray_alert", "foxray_state"}


def test_implements_the_store_protocol(store):
    from foxray.core.storage import Backend, Store
    assert isinstance(store, Store)
    assert isinstance(store.db, Backend)
    assert store.dialect in ("sqlite", "postgres")
