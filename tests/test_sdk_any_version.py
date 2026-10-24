"""The parts of the Sentry protocol every SDK version uses, run with whatever sentry-sdk is installed
(askrobots.com pins 1.9.10; the other SDK tests need 2.x for crons and new_scope)."""

import json
import subprocess
import sys
import time

import pytest

from foxray.core.sql import IssueQuery
from foxray.standalone.store import open_store
from tests.test_sdk_standalone import ENV, ROOT, foxray, server  # noqa: F401 - the server fixture


@pytest.mark.slow
def test_errors_and_messages_from_any_sdk_version(server):  # noqa: F811
    dsn = foxray("--db", server["db"], "create-project", "shop", "--base-url", server["base"]).strip()
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "sdk_any_client.py"), dsn], cwd=ROOT, env=ENV,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    ids = json.loads(out.strip().splitlines()[-1])
    store = open_store(server["db"])
    project = store.get_project(1)
    deadline = time.time() + 10
    while time.time() < deadline and store.find_event(project.id, ids["msg"]) is None:
        time.sleep(0.1)
    issues = {i.title: i for i in store.list_issues(IssueQuery(project_id=project.id, status="all"))[0]}
    assert issues["ZeroDivisionError: division by zero"].times_seen == 2
    assert issues["Payment provider slow: 1234 ms"].level == "warning"
    assert store.find_event(project.id, ids["msg"]).payload["tags"]["tenant"] == "acme"


def test_transport_takes_plain_events_from_sdk_1x():
    """sentry-sdk 1.x passes plain errors to Transport.capture_event, not capture_envelope."""
    from foxray.django.sdk import FoxrayTransport
    got = []
    t = FoxrayTransport(deliver_fn=got.append)
    t.capture_event({"event_id": "a" * 32, "level": "error", "message": "from 1.x"})
    t.flush(5)
    t.kill()
    assert len(got) == 1
    data = got[0] if isinstance(got[0], bytes) else got[0].encode()
    assert b'"type":"event"' in data.replace(b" ", b"") and b"from 1.x" in data
