"""Measure the standalone server: idle memory and the time to ingest 1,000 events.

    python bench/measure.py                       # SQLite in a temp dir
    python bench/measure.py --db postgres://...   # an existing, empty Postgres database

Starts ``foxray serve`` in a subprocess, waits, reads its RSS with ``ps``,
then posts SDK-shaped, gzip-compressed envelopes (about 4 KB of JSON each:
an exception with in-app frames and source context, breadcrumbs, contexts,
tags) over keep-alive connections. Rate limits and sampling are switched
off so every event is fully stored."""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rss_kb(pid: int) -> int:
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).strip())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_envelope(i: int) -> bytes:
    kind = i % 20  # 20 distinct issues
    event_id = uuid.uuid4().hex
    frames = [{"filename": f"lib/framework/{n}.py", "module": f"framework.mod{n}", "function": f"call{n}",
               "lineno": 10 + n, "in_app": False, "context_line": f"    return handler{n}(request)",
               "pre_context": ["", "def call():", "    request = get()"], "post_context": ["", ""]}
              for n in range(8)]
    frames += [{"filename": "shop/views.py", "module": "shop.views", "function": f"view_{kind}", "lineno": 40 + kind,
                "in_app": True, "context_line": f"    total = compute_{kind}(cart)",
                "pre_context": ["def view(request):", "    cart = load(request)", "    # sum it"],
                "post_context": ["    return render(total)", ""]}]
    event = {
        "event_id": event_id, "timestamp": time.time(), "platform": "python", "level": "error",
        "release": "shop@1.2.3", "environment": "production", "server_name": "web-1",
        "transaction": f"/checkout/{kind}/",
        "exception": {"values": [{"type": f"Error{kind}", "value": f"failed on order {i}",
                                  "mechanism": {"type": "django", "handled": False},
                                  "stacktrace": {"frames": frames}}]},
        "breadcrumbs": {"values": [{"timestamp": time.time(), "category": "query", "message": f"SELECT {n} FROM t",
                                    "level": "info"} for n in range(10)]},
        "contexts": {"runtime": {"name": "CPython", "version": "3.10.1"}, "os": {"name": "Linux"},
                     "trace": {"trace_id": uuid.uuid4().hex, "span_id": uuid.uuid4().hex[:16]}},
        "tags": {"tenant": f"t{i % 7}", "browser": "Firefox"},
        "user": {"id": str(i % 50)},
        "request": {"url": f"https://shop.example/checkout/{kind}/", "method": "POST",
                    "headers": {"User-Agent": "Mozilla/5.0 Firefox/120"}},
        "sdk": {"name": "sentry.python.django", "version": "2.73.0"},
    }
    payload = json.dumps(event).encode()
    body = (json.dumps({"event_id": event_id, "sent_at": "2026-10-24T00:00:00Z"}).encode() + b"\n"
            + json.dumps({"type": "event", "length": len(payload)}).encode() + b"\n" + payload + b"\n")
    return gzip.compress(body)


def post_all(port: int, key: str, bodies: list[bytes]) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"X-Sentry-Auth": f"Sentry sentry_key={key}, sentry_version=7", "Content-Encoding": "gzip",
               "Content-Type": "application/x-sentry-envelope"}
    for body in bodies:
        conn.request("POST", "/api/1/envelope/", body=body, headers=headers)
        r = conn.getresponse()
        r.read()
        if r.status != 200:
            raise SystemExit(f"unexpected status {r.status}")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="database URL (default: a new SQLite file)")
    ap.add_argument("--events", type=int, default=1000)
    ap.add_argument("--clients", type=int, default=1, help="parallel keep-alive connections")
    ap.add_argument("--idle", type=float, default=3.0, help="seconds to wait before reading idle RSS")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="foxray-bench-")
    db = args.db or os.path.join(tmp, "foxray.db")
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "FOXRAY_RATE_LIMIT_PER_MINUTE": "0", "FOXRAY_SAMPLE_AFTER": "0"}
    base = f"http://127.0.0.1:{port}"
    dsn = subprocess.check_output([sys.executable, "-m", "foxray", "--db", db, "create-project", "bench",
                                   "--base-url", base], env=env, text=True).strip()
    key = dsn.split("//")[1].split("@")[0]
    proc = subprocess.Popen([sys.executable, "-m", "foxray", "--db", db, "serve", "--port", str(port),
                             "--base-url", base], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "listening" in proc.stdout.readline()
        threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()  # drain logs
        time.sleep(args.idle)
        idle = rss_kb(proc.pid)
        bodies = [make_envelope(i) for i in range(args.events)]
        raw = sum(len(gzip.decompress(b)) for b in bodies) / len(bodies)
        chunks = [bodies[i::args.clients] for i in range(args.clients)]
        t0 = time.perf_counter()
        threads = [threading.Thread(target=post_all, args=(port, key, c)) for c in chunks]
        [t.start() for t in threads]
        [t.join() for t in threads]
        elapsed = time.perf_counter() - t0
        time.sleep(1.0)
        after = rss_kb(proc.pid)
        sys.path.insert(0, str(ROOT))
        from foxray.standalone.store import open_store
        store = open_store(db)
        issues, total = store.list_issues()
        result = {
            "database": "postgres" if db.startswith("postgres") else "sqlite",
            "python": sys.version.split()[0],
            "events": args.events, "clients": args.clients,
            "avg_event_json_bytes": round(raw),
            "idle_rss_mb": round(idle / 1024, 1),
            "rss_after_ingest_mb": round(after / 1024, 1),
            "ingest_seconds": round(elapsed, 2),
            "events_per_second": round(args.events / elapsed),
            "ms_per_event": round(1000 * elapsed / args.events, 2),
            "issues": total, "stored_events": store.count_events(),
            "db_bytes": store.db_size(),
        }
        print(json.dumps(result, indent=2))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
