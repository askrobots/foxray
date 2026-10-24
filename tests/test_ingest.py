"""The ingest pipeline: answers to SDKs, rate limits, item types, alert rules."""

import gzip
import json
from datetime import timedelta

from foxray.core.settings import Settings
from tests.helpers import auth_headers, envelope, make_event, make_stack


def post(ing, project, body: bytes, kind="envelope", headers=None, gz=True):
    h = auth_headers(project, **(headers or {}))
    if gz:
        h["content-encoding"] = "gzip"
        body = gzip.compress(body)
    return ing.handle(project.id, kind, h, {}, body)


def event_env(ev):
    return envelope(({"type": "event"}, ev), header={"event_id": ev["event_id"]})


def test_store_and_group(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store)
    p = store.create_project("app")
    ids = []
    for i in range(3):
        ev = make_event(value=f"bad {i}")
        ids.append(ev["event_id"])
        r = post(ing, p, event_env(ev))
        assert r.status == 200 and json.loads(r.body) == {"id": ev["event_id"]}
    issues, total = store.list_issues()
    assert total == 1 and issues[0].times_seen == 3
    assert store.count_events(issues[0].id) == 3


def test_auth_failures_and_unknown_project(sqlite_store):
    ing, *_ = make_stack(sqlite_store)
    p = sqlite_store.create_project("app")
    body = event_env(make_event())
    assert ing.handle(p.id, "envelope", {}, {}, body).status == 401
    assert ing.handle(p.id, "envelope", {"x-sentry-auth": "Sentry sentry_key=wrong"}, {}, body).status == 403
    assert ing.handle(999, "envelope", auth_headers(p), {}, body).status == 404
    assert ing.handle(p.id, "envelope", {}, {"sentry_key": p.public_key}, body).status == 200


def test_size_and_encoding_errors(sqlite_store):
    ing, *_ = make_stack(sqlite_store, Settings(max_body_bytes=1000))
    p = sqlite_store.create_project("app")
    assert post(ing, p, b"x" * 2000, gz=False).status == 413
    r = ing.handle(p.id, "envelope", auth_headers(p, **{"content-encoding": "zstd"}), {}, b"abc")
    assert r.status == 415
    assert post(ing, p, b"garbage\n", gz=False).status == 400


def test_other_item_types_are_accepted_dropped_and_counted(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store)
    p = store.create_project("app")
    body = envelope(({"type": "transaction"}, {"x": 1}), ({"type": "session"}, {}), ({"type": "sessions"}, {}),
                    ({"type": "client_report"}, {}), ({"type": "attachment", "filename": "a"}, b"\x00\xff"),
                    ({"type": "profile"}, {}), ({"type": "replay_event"}, {}), ({"type": "statsd"}, b"a:1|c"),
                    ({"type": "brand_new_thing"}, {}))
    r = post(ing, p, body)
    assert r.status == 200
    reasons = {row[2]: row[3] for row in store.counters(p.id)}
    for t in ("transaction", "session", "sessions", "client_report", "attachment", "profile", "replay_event",
              "statsd", "brand_new_thing"):
        assert reasons["dropped:" + t] == 1
    assert store.list_issues()[1] == 0


def test_legacy_store_endpoint(sqlite_store):
    ing, *_ = make_stack(sqlite_store)
    p = sqlite_store.create_project("app")
    ev = make_event()
    r = post(ing, p, json.dumps(ev).encode(), kind="store")
    assert r.status == 200 and json.loads(r.body)["id"] == ev["event_id"]
    assert sqlite_store.list_issues()[1] == 1


def test_duplicate_event_id_counts_once(sqlite_store):
    ing, *_ = make_stack(sqlite_store)
    p = sqlite_store.create_project("app")
    ev = make_event()
    assert post(ing, p, event_env(ev)).status == 200
    assert post(ing, p, event_env(ev)).status == 200
    assert sqlite_store.list_issues()[0][0].times_seen == 1


def test_rate_limit_answers_429_with_headers(sqlite_store):
    store = sqlite_store
    ing, _, _, _, clock = make_stack(store, Settings(rate_limit_per_minute=3))
    p = store.create_project("app")
    statuses = [post(ing, p, event_env(make_event())) for _ in range(5)]
    assert [r.status for r in statuses] == [200, 200, 200, 429, 429]
    limited = statuses[3]
    retry = int(limited.headers["Retry-After"])
    assert 1 <= retry <= 60
    assert limited.headers["X-Sentry-Rate-Limits"] == f"{retry}:error;default;security:project"
    assert {r[2]: r[3] for r in store.counters(p.id)}["rate_limited"] == 2
    clock.advance(minutes=1)
    assert post(ing, p, event_env(make_event())).status == 200
    # per-project override
    store.update_project(p.id, rate_limit=0)
    p = store.get_project(p.id)
    assert all(post(ing, p, event_env(make_event())).status == 200 for _ in range(5))


def test_filters_drop_with_200_and_count(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store)
    p = store.create_project("app")
    rule = store.add_filter(p.id, "environment", "development")
    r = post(ing, p, event_env(make_event(environment="development")))
    assert r.status == 200
    assert store.list_issues()[1] == 0
    assert {x[2]: x[3] for x in store.counters(p.id)} == {f"filter:environment#{rule.id}": 1}


def test_sampling_keeps_count_drops_body(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store, Settings(sample_after=2))
    p = store.create_project("app")
    for _ in range(5):
        post(ing, p, event_env(make_event()))
    issue = store.list_issues()[0][0]
    assert issue.times_seen == 5
    assert store.count_events(issue.id) == 2
    assert store.issue_days(issue.id)[0][1] == 5
    assert {x[2]: x[3] for x in store.counters(p.id)}["sampled"] == 3


# -- alert rules -----------------------------------------------------------

def kinds(store):
    return [a.kind for a in reversed(store.list_alerts())]


def test_new_issue_alerts_repeats_do_not(sqlite_store):
    store = sqlite_store
    ing, *_ = make_stack(store)
    p = store.create_project("app")
    for _ in range(4):
        post(ing, p, event_env(make_event()))
    assert kinds(store) == ["new_issue"]
    post(ing, p, event_env(make_event("TypeError")))
    assert kinds(store) == ["new_issue", "new_issue"]


def test_regression_alerts_after_resolve_respecting_cooldown(sqlite_store):
    store = sqlite_store
    ing, _, _, _, clock = make_stack(store, Settings(alert_cooldown_seconds=3600))
    p = store.create_project("app")
    post(ing, p, event_env(make_event()))
    issue = store.list_issues()[0][0]
    # resolved and back within the cooldown: reopened, but quiet
    store.set_issue_status(issue.id, "resolved", now=clock())
    clock.advance(minutes=10)
    post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "unresolved"
    assert kinds(store) == ["new_issue"]
    # resolved and back after the cooldown: a regression alert
    store.set_issue_status(issue.id, "resolved", now=clock())
    clock.advance(hours=2)
    post(ing, p, event_env(make_event()))
    got = store.get_issue(issue.id)
    assert got.status == "unresolved" and got.reopened_at == clock()
    assert kinds(store) == ["new_issue", "regression"]


def test_ignore_until_count_and_until_date(sqlite_store):
    store = sqlite_store
    ing, _, _, _, clock = make_stack(store, Settings(alert_cooldown_seconds=0))
    p = store.create_project("app")
    post(ing, p, event_env(make_event()))
    issue = store.list_issues()[0][0]
    store.set_issue_status(issue.id, "ignored", now=clock(), ignore_more=3)
    post(ing, p, event_env(make_event()))
    post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "ignored"
    post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "unresolved"
    assert kinds(store)[-1] == "unignored"
    store.set_issue_status(issue.id, "ignored", now=clock(), ignore_until=clock() + timedelta(days=1))
    post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "ignored"
    clock.advance(days=2)
    post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "unresolved"
    # plain ignore stays ignored forever and never alerts
    store.set_issue_status(issue.id, "ignored", now=clock())
    before = len(store.list_alerts())
    for _ in range(3):
        post(ing, p, event_env(make_event()))
    assert store.get_issue(issue.id).status == "ignored" and len(store.list_alerts()) == before


def test_alert_rules_level_environment_new_vs_regression(sqlite_store):
    store = sqlite_store
    ing, _, _, _, clock = make_stack(store, Settings(alert_cooldown_seconds=0))
    p = store.create_project("app", alert_min_level="error", alert_environments="production", alert_regression=False)
    post(ing, p, event_env(make_event("A", level="warning", environment="production")))
    post(ing, p, event_env(make_event("B", environment="staging")))
    post(ing, p, event_env(make_event("C", environment="production")))
    assert [a.subject.split(": ", 1)[1] for a in store.list_alerts()] == ["C: boom"]
    issue = [i for i in store.list_issues()[0] if i.title.startswith("C")][0]
    store.set_issue_status(issue.id, "resolved", now=clock())
    clock.advance(hours=1)
    post(ing, p, event_env(make_event("C", environment="production")))
    assert len(store.list_alerts()) == 1  # regressions switched off
    store.update_project(p.id, alert_new=False, alert_regression=True)
    p = store.get_project(p.id)
    post(ing, p, event_env(make_event("D", environment="production")))
    assert len(store.list_alerts()) == 1  # new issues switched off


def test_outbox_sends_after_commit_and_retries(sqlite_store):
    from tests.helpers import Recorder
    store = sqlite_store
    channel = Recorder(fail=True)
    ing, hk, outbox, _, clock = make_stack(store, channel=channel)
    p = store.create_project("app")
    r = post(ing, p, event_env(make_event()))
    assert r.after is not None
    r.after()  # what the server runs once the response is sent
    alert = store.list_alerts()[0]
    assert alert.status == "pending" and alert.attempts == 1 and "mail server down" in alert.last_error
    assert outbox.flush()["sent"] == 0  # not due yet: backoff
    channel.fail = False
    clock.advance(minutes=5)
    assert outbox.flush()["sent"] == 1
    assert store.list_alerts()[0].status == "sent" and len(channel.sent) == 1
    assert outbox.flush()["sent"] == 0  # never twice


def test_outbox_gives_up_after_retention(sqlite_store):
    from tests.helpers import Recorder
    store = sqlite_store
    ing, hk, outbox, _, clock = make_stack(store, channel=Recorder(fail=True))
    p = store.create_project("app")
    post(ing, p, event_env(make_event()))
    clock.advance(days=8)
    outbox.flush()
    assert store.list_alerts()[0].status == "failed"
