"""What a run of tests/sdk_client.py must leave in a store, whichever kind."""

from foxray.core.sql import IssueQuery


def check_sdk_run(store, project, ids: dict) -> None:
    issues = {i.title: i for i in store.list_issues(IssueQuery(project_id=project.id, status="all"))[0]}
    titles = set(issues)
    assert "ZeroDivisionError: division by zero" in titles, titles
    assert "KeyError: 'missing'" in titles, titles
    assert "Payment provider slow: 1234 ms" in titles, titles
    assert "db timeout on shard 3" in titles or "db timeout on shard 7" in titles, titles
    assert "RuntimeError: job failed" in titles, titles

    # grouping: the same place twice is one issue; logging calls group by their template
    zero = issues["ZeroDivisionError: division by zero"]
    assert zero.times_seen == 2 and store.count_events(zero.id) == 2
    shard = [i for t, i in issues.items() if t.startswith("db timeout on shard")]
    assert len(shard) == 1 and shard[0].times_seen == 2

    # the stored event: frames with context, release/environment, PII cut down
    ev = store.find_event(project.id, ids["exc"][0]).payload
    frame = ev["exception"]["values"][-1]["stacktrace"]["frames"][-1]
    assert frame["function"] == "checkout" and frame["in_app"] is True
    assert "10 / n" in frame["context_line"]
    assert ev["release"] == "shop@1.2.3" and ev["environment"] == "test" and ev["server_name"] == "client-1"
    assert zero.culprit.endswith("in checkout")

    key = store.find_event(project.id, ids["keyerror"]).payload
    assert key["user"] == {"id": "42"}
    assert key["tags"]["tenant"] == "acme"
    assert key["extra"]["api_token"] == "[Filtered]"

    msg = issues["Payment provider slow: 1234 ms"]
    assert msg.level == "warning"
    tags = store.issue_tags(zero.id)
    assert tags["environment"] == [("test", 2)] and tags["release"] == [("shop@1.2.3", 2)]

    # monitors and check-ins
    nightly = store.get_monitor(project.id, "nightly-report")
    assert (nightly.schedule_type, nightly.schedule_value, nightly.timezone) == ("crontab", "0 3 * * *", "America/Chicago")
    assert nightly.checkin_margin == 10 and nightly.max_runtime == 30 and nightly.status == "ok"
    assert nightly.next_due_at is not None
    runs = store.list_checkins(nightly.id)
    assert len(runs) == 1 and runs[0].status == "ok" and runs[0].duration and runs[0].duration > 0
    broken = store.get_monitor(project.id, "broken-job")
    assert broken.status == "error" and [c.status for c in store.list_checkins(broken.id)] == ["error"]
    hourly = store.get_monitor(project.id, "hourly-sync")
    assert (hourly.schedule_type, hourly.schedule_value, hourly.schedule_unit) == ("interval", "1", "hour")

    # alerts were decided and queued
    kinds = [a.kind for a in store.list_alerts(100)]
    assert kinds.count("new_issue") == len(issues)
    assert kinds.count("monitor_error") == 1
