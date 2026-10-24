"""Foxray as a Django app: ingest views, the in-process SDK transport, the
real SDK over HTTP, the admin pages, permissions, email alerts, commands."""

import gzip
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.management import call_command
from django.test import Client

from foxray.django.runtime import runtime
from tests.helpers import envelope, make_event
from tests.sdk_checks import check_sdk_run

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def rt():
    return runtime()


@pytest.fixture
def project(rt):
    return rt.store.create_project("web")


@pytest.fixture
def admin_client():
    user = User.objects.create_superuser("root", "root@example.com", "pw-for-tests")
    c = Client()
    c.force_login(user)
    return c


def post_envelope(client, project, body, **headers):
    return client.post(f"/foxray/api/{project.id}/envelope/", data=body, content_type="application/x-sentry-envelope",
                       HTTP_X_SENTRY_AUTH=f"Sentry sentry_key={project.public_key}, sentry_version=7", **headers)


def test_ingest_view_is_csrf_exempt_and_gzip(project, rt):
    client = Client(enforce_csrf_checks=True)
    ev = make_event()
    r = post_envelope(client, project, gzip.compress(envelope(({"type": "event"}, ev))), HTTP_CONTENT_ENCODING="gzip")
    assert r.status_code == 200 and r.json() == {"id": ev["event_id"]}
    assert r["Access-Control-Allow-Origin"] == "*"
    issue = rt.store.list_issues()[0][0]
    assert issue.title == "ValueError: boom" and issue.times_seen == 1


def test_ingest_view_errors(project, rt):
    c = Client()
    body = envelope(({"type": "event"}, make_event()))
    assert c.post(f"/foxray/api/{project.id}/envelope/", data=body, content_type="text/plain").status_code == 401
    assert post_envelope(c, project, b"x", HTTP_CONTENT_ENCODING="zstd").status_code == 415
    assert c.post("/foxray/api/999/envelope/", data=body, content_type="text/plain").status_code == 404
    assert c.get(f"/foxray/api/{project.id}/envelope/").status_code == 405
    assert c.options(f"/foxray/api/{project.id}/envelope/").status_code == 200
    rt.settings.max_body_bytes = 100
    try:
        assert post_envelope(c, project, b"x" * 500).status_code == 413
    finally:
        rt.settings.max_body_bytes = 10 * 1024 * 1024
    r = c.post(f"/foxray/api/{project.id}/store/?sentry_key={project.public_key}",
               data=json.dumps({"message": "legacy"}), content_type="application/json")
    assert r.status_code == 200


def test_rate_limit_through_django(project, rt):
    rt.store.update_project(project.id, rate_limit=2)
    c = Client()
    codes = [post_envelope(c, project, envelope(({"type": "event"}, make_event()))).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    r = post_envelope(c, project, envelope(({"type": "event"}, make_event())))
    assert r["X-Sentry-Rate-Limits"].endswith(":error;default;security:project") and int(r["Retry-After"]) > 0


def test_email_alerts_go_to_admins_then_superusers_then_override(project, rt, settings):
    c = Client()
    post_envelope(c, project, envelope(({"type": "event"}, make_event("First"))))
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["ops@example.com"]
    assert mail.outbox[0].subject.startswith("[Foxray] New issue: First")
    assert "/admin/foxray/issue/" in mail.outbox[0].body
    settings.ADMINS = []
    User.objects.create_superuser("boss", "boss@example.com", "x")
    post_envelope(c, project, envelope(({"type": "event"}, make_event("Second"))))
    assert mail.outbox[-1].to == ["boss@example.com"]
    settings.FOXRAY = {**settings.FOXRAY, "ALERT_EMAILS": ["oncall@example.com"]}
    rt = runtime()
    post_envelope(c, project, envelope(({"type": "event"}, make_event("Third"))))
    assert mail.outbox[-1].to == ["oncall@example.com"]
    # repeats never alert
    post_envelope(c, project, envelope(({"type": "event"}, make_event("Third"))))
    assert len(mail.outbox) == 3
    assert all(a.status == "sent" for a in rt.store.list_alerts())


def test_in_process_transport_needs_no_dsn(rt):
    import sentry_sdk

    from foxray.django import sdk
    sdk.init(release="app@9")
    try:
        assert sentry_sdk.get_client().is_active()
        try:
            {}["nope"]
        except KeyError:
            event_id = sentry_sdk.capture_exception()
        sentry_sdk.capture_message("hello from inside")
        sentry_sdk.flush(timeout=10)
    finally:
        sentry_sdk.get_client().close(timeout=5)  # stops the transport thread
        sentry_sdk.init()  # back to an inactive client
    default = rt.store.get_project_by_name("default")
    ev = rt.store.find_event(default.id, event_id)
    assert ev is not None and ev.payload["release"] == "app@9"
    titles = {i.title for i in rt.store.list_issues()[0]}
    assert {"KeyError: 'nope'", "hello from inside"} <= titles


def test_in_process_transport_never_raises(caplog):
    from foxray.django.sdk import FoxrayTransport

    def broken(data):
        raise RuntimeError("database is down")
    t = FoxrayTransport(deliver_fn=broken)

    class Env:
        def serialize(self):
            return b'{}\n{"type":"event"}\n{"message":"lost"}\n'
    t.capture_envelope(Env())
    t.flush(5)
    assert any(r.getMessage() == "Foxray transport failed" for r in caplog.records)


def test_deliver_falls_back_to_logging(caplog, monkeypatch):
    from foxray.django import sdk
    from foxray.django import runtime as rt_mod

    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr(rt_mod, "runtime", boom)
    sdk.deliver(b'{}\n{"type":"event"}\n{"exception":{"values":[{"type":"ValueError","value":"x"}]}}\n')
    assert any("could not store an event" in r.getMessage() and "ValueError: x" in r.getMessage()
               for r in caplog.records)


def test_real_sentry_sdk_over_http_to_django(live_server, rt):
    project = rt.store.create_project("shop")
    host = live_server.url.split("//")[1]
    dsn = f"http://{project.public_key}@{host}/foxray/{project.id}"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "sdk_client.py"), dsn], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    ids = json.loads(out.strip().splitlines()[-1])
    check_sdk_run(rt.store, project, ids)


def test_admin_pages(admin_client, project, rt):
    c = Client()
    for i in range(3):
        post_envelope(c, project, envelope(({"type": "event"}, make_event(environment="production", release="1.0"))))
    post_envelope(c, project, envelope(({"type": "event"}, make_event("TypeError", "other", level="warning"))))
    post_envelope(c, project, envelope(({"type": "check_in"}, {"monitor_slug": "nightly", "status": "ok",
                                        "monitor_config": {"schedule": {"type": "crontab", "value": "0 3 * * *"}}})))
    rt.store.add_filter(project.id, "environment", "development")
    r = admin_client.get("/admin/")
    assert r.status_code == 200
    page = r.content.decode()
    assert "Foxray" in page
    for name in ("Issues", "Events", "Monitors", "Check-ins", "Filters", "Projects", "Alerts", "Storage"):
        assert name in page, name
    r = admin_client.get("/admin/foxray/issue/")
    assert r.status_code == 200 and "ValueError: boom" in r.content.decode()
    for q in ("?status=all", "?level__exact=warning", "?environment=production", "?release=1.0", "?q=boom",
              "?o=4", "?status=resolved"):
        assert admin_client.get("/admin/foxray/issue/" + q).status_code == 200, q
    assert "TypeError" not in admin_client.get("/admin/foxray/issue/?environment=production").content.decode()
    issue = [i for i in rt.store.list_issues()[0] if i.title == "ValueError: boom"][0]
    r = admin_client.get(f"/admin/foxray/issue/{issue.id}/change/")
    detail = r.content.decode()
    assert r.status_code == 200 and "app.views in index" in detail and "raise ValueError" in detail
    assert "production" in detail and "Last 30 days" in detail
    # actions on the detail page
    r = admin_client.post(f"/admin/foxray/issue/{issue.id}/change/", {"action": "ignore_count", "count": "10"})
    assert r.status_code == 302 and rt.store.get_issue(issue.id).ignore_until_count == 13
    admin_client.post(f"/admin/foxray/issue/{issue.id}/change/", {"action": "reopen"})
    admin_client.post(f"/admin/foxray/issue/{issue.id}/change/", {"action": "ignore_until", "until": "2030-01-01"})
    assert rt.store.get_issue(issue.id).ignore_until.year == 2030
    # bulk actions on the list
    r = admin_client.post("/admin/foxray/issue/?status=all", {"action": "resolve", "_selected_action": [issue.id]})
    assert r.status_code == 302 and rt.store.get_issue(issue.id).status == "resolved"
    admin_client.post("/admin/foxray/issue/?status=all", {"action": "reopen", "_selected_action": [issue.id]})
    assert rt.store.get_issue(issue.id).status == "unresolved"
    # the other pages
    for url in ("/admin/foxray/event/", "/admin/foxray/monitor/", "/admin/foxray/checkin/", "/admin/foxray/filterrule/",
                "/admin/foxray/project/", f"/admin/foxray/project/{project.id}/change/", "/admin/foxray/alert/",
                "/admin/foxray/storage/", "/foxray/"):
        r = admin_client.get(url, follow=True)
        assert r.status_code == 200, url
    page = admin_client.get(f"/admin/foxray/project/{project.id}/change/").content.decode()
    assert f"http://{project.public_key}@testserver/foxray/{project.id}" in page
    ev = rt.store.latest_event(issue.id)
    assert admin_client.get(f"/admin/foxray/event/{ev.id}/change/").status_code == 200
    mon = rt.store.get_monitor(project.id, "nightly")
    assert "cron 0 3 * * *" in admin_client.get(f"/admin/foxray/monitor/{mon.id}/change/").content.decode()
    storage = admin_client.get("/admin/foxray/storage/").content.decode()
    assert "foxray_event" in storage and "Clean up now" in storage
    r = admin_client.post("/admin/foxray/storage/", follow=True)
    assert "Cleanup removed" in r.content.decode() or "already running" in r.content.decode()
    # a bad regex is refused by the filter form
    r = admin_client.post("/admin/foxray/filterrule/add/", {"project": project.id, "kind": "message",
                                                           "pattern": "(a+)+", "is_regex": "on", "enabled": "on"})
    assert r.status_code == 200 and "nested repeats" in r.content.decode()


def test_permissions_use_django_auth(project, rt):
    c = Client()
    post_envelope(c, project, envelope(({"type": "event"}, make_event())))
    issue = rt.store.list_issues()[0][0]
    staff = User.objects.create_user("staff", "s@example.com", "x", is_staff=True)
    c.force_login(staff)
    assert c.get("/admin/foxray/issue/").status_code == 403
    staff.user_permissions.add(Permission.objects.get(codename="view_issue"))
    staff = User.objects.get(pk=staff.pk)
    c.force_login(staff)
    assert c.get("/admin/foxray/issue/").status_code == 200
    assert c.get(f"/admin/foxray/issue/{issue.id}/change/").status_code == 200
    assert c.post(f"/admin/foxray/issue/{issue.id}/change/", {"action": "resolve"}).status_code == 403
    staff.user_permissions.add(Permission.objects.get(codename="change_issue"))
    c.force_login(User.objects.get(pk=staff.pk))
    assert c.post(f"/admin/foxray/issue/{issue.id}/change/", {"action": "resolve"}).status_code == 302
    assert rt.store.get_issue(issue.id).status == "resolved"
    anon = Client()
    assert anon.get("/admin/foxray/issue/").status_code == 302  # to the admin login


def test_management_commands(project, rt):
    out = io.StringIO()
    call_command("foxray_create_project", "webhive", stdout=out)
    dsn = out.getvalue().strip()
    assert dsn.startswith("http://") and "@testserver/foxray/" in dsn
    out = io.StringIO()
    call_command("foxray_tick", "--force-cleanup", stdout=out)
    assert "monitor_alerts" in out.getvalue()
    out = io.StringIO()
    call_command("foxray_prune", stdout=out)
    assert '"mode"' in out.getvalue()


def test_ingest_never_raises_into_django(project, rt, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(rt.ingestor, "_process", boom)
    r = post_envelope(Client(), project, envelope(({"type": "event"}, make_event())))
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}


def test_uuid_project_ids_rejected_cleanly(project):
    r = Client().post(f"/foxray/api/{uuid.uuid4().hex}/envelope/", data=b"{}", content_type="text/plain")
    assert r.status_code == 404
