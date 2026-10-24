"""The key test: the real Sentry SDK against the standalone server."""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from foxray.standalone.store import open_store
from tests.sdk_checks import check_sdk_run

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def foxray(*args, **kw):
    return subprocess.run([sys.executable, "-m", "foxray", *args], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, timeout=60, check=True, **kw).stdout


@pytest.fixture(params=["sqlite", "postgres"])
def server(request, tmp_path):
    db = str(tmp_path / "foxray.db") if request.param == "sqlite" else request.getfixturevalue("pg_fresh_url")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, "-m", "foxray", "--db", db, "serve", "--host", "127.0.0.1",
                             "--port", str(port), "--base-url", base], cwd=ROOT, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    assert "listening" in line, line
    try:
        yield {"db": db, "base": base, "proc": proc}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _has_brotli() -> bool:
    try:
        import brotli  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.slow
@pytest.mark.parametrize("compression", ["gzip", pytest.param("br", marks=pytest.mark.skipif(
    not _has_brotli(), reason="Brotli not installed"))])
def test_real_sentry_sdk_against_standalone(server, compression):
    dsn = foxray("--db", server["db"], "create-project", "shop", "--base-url", server["base"]).strip()
    assert dsn.startswith("http://") and dsn.endswith("/1")
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "sdk_client.py"), dsn], cwd=ROOT,
                         env={**ENV, "SDK_COMPRESSION": compression},
                         capture_output=True, text=True, timeout=60, check=True).stdout
    ids = json.loads(out.strip().splitlines()[-1])
    store = open_store(server["db"])
    project = store.get_project(1)
    deadline = time.time() + 10
    while time.time() < deadline and len(store.list_monitors(project.id)) < 3:
        time.sleep(0.1)
    check_sdk_run(store, project, ids)

    # The UI, behind its login.
    subprocess.run([sys.executable, "-m", "foxray", "--db", server["db"], "create-user", "dev@example.com",
                    "--password-stdin"], cwd=ROOT, env=ENV, input="correct horse battery\n", text=True,
                   check=True, capture_output=True, timeout=60)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = server["base"]
    with opener.open(base + "/issues") as r:  # redirected to the login page
        assert "/login" in r.url
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(base + "/login", urllib.parse.urlencode({"email": "dev@example.com", "password": "nope"}).encode())
    assert e.value.code == 401
    with opener.open(base + "/login", urllib.parse.urlencode(
            {"email": "dev@example.com", "password": "correct horse battery", "next": "/issues"}).encode()) as r:
        listing = r.read().decode()
    assert "ZeroDivisionError" in listing and "Payment provider slow" in listing
    zero = [i for i in store.list_issues()[0] if i.title.startswith("ZeroDivisionError")][0]
    with opener.open(f"{base}/issues/{zero.id}") as r:
        detail = r.read().decode()
    assert "checkout" in detail and "10 / n" in detail and "shop@1.2.3" in detail
    for path in ("/monitors", "/projects", "/projects/1", "/alerts", "/storage",
                 "/issues?status=all&level=warning&sort=count&q=payment"):
        with opener.open(base + path) as r:
            assert r.status == 200, path
    with opener.open(base + "/monitors") as r:
        assert "nightly-report" in r.read().decode()
    # Resolve from the UI (with the CSRF token from the page).
    token = detail.split('name="csrf" value="')[1].split('"')[0]
    with opener.open(f"{base}/issues/{zero.id}", urllib.parse.urlencode({"csrf": token, "action": "resolve"}).encode()):
        pass
    assert store.get_issue(zero.id).status == "resolved"
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(f"{base}/issues/{zero.id}", urllib.parse.urlencode({"csrf": "bad", "action": "reopen"}).encode())
    assert e.value.code == 403


@pytest.mark.slow
def test_standalone_http_edges(server):
    dsn = foxray("--db", server["db"], "create-project", "edge", "--base-url", server["base"]).strip()
    key = dsn.split("//")[1].split("@")[0]
    url = server["base"] + "/api/1/envelope/"

    def post(body, headers):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    auth = {"X-Sentry-Auth": f"Sentry sentry_key={key}, sentry_version=7"}
    status, headers, body = post(b'{}\n{"type":"session"}\n{}\n', auth)
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"
    assert post(b"x", {**auth, "Content-Encoding": "br" if False else "zstd"})[0] == 415
    assert post(b"{}\n", {})[0] == 401
    req = urllib.request.Request(url, method="OPTIONS")
    with urllib.request.urlopen(req) as r:
        assert r.status == 200 and "X-Sentry-Auth" in r.headers["Access-Control-Allow-Headers"]
    with urllib.request.urlopen(server["base"] + "/healthz") as r:
        assert r.read() == b"ok\n"
    # query-string auth and the legacy store endpoint
    req = urllib.request.Request(server["base"] + f"/api/1/store/?sentry_key={key}&sentry_version=7",
                                 data=json.dumps({"message": "legacy"}).encode(), method="POST")
    with urllib.request.urlopen(req) as r:
        assert "id" in json.loads(r.read())
