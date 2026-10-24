"""``foxray``: serve, create-project, create-user, projects, prune, tick, storage."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import sys
import threading

from .config import load


def _app(args):
    from .web import App
    cfg, settings = load(args.config)
    if args.db:
        cfg.database_url = args.db
    if getattr(args, "host", None):
        cfg.host = args.host
    if getattr(args, "port", None) is not None:
        cfg.port = args.port
    if getattr(args, "base_url", None):
        settings.base_url = args.base_url
    return App(cfg, settings)


def cmd_serve(args) -> int:
    from .web import make_server, start_ticker
    app = _app(args)
    server = make_server(app)
    host, port = server.server_address[:2]
    if app.cfg.port == 0 and app.settings.base_url.endswith(":0"):
        app.settings.base_url = app.links.base_url = f"http://{host}:{port}"
    stop = threading.Event()
    start_ticker(app, stop)

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Foxray listening on http://{host}:{port} (database: {app.store.dialect})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.store.db.close()
    return 0


def cmd_create_project(args) -> int:
    app = _app(args)
    project = app.store.create_project(args.name)
    print(app.dsn(project))
    return 0


def cmd_projects(args) -> int:
    app = _app(args)
    for p in app.store.list_projects():
        print(f"{p.id}\t{p.name}\t{app.dsn(p)}")
    return 0


def cmd_create_user(args) -> int:
    app = _app(args)
    password = os.environ.get("FOXRAY_PASSWORD") if args.password_env else None
    if password is None:
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password: ")
            if getpass.getpass("Again: ") != password:
                print("Passwords differ.", file=sys.stderr)
                return 1
    try:
        app.store.create_user(args.email, password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"User {args.email} can log in.")
    return 0


def cmd_prune(args) -> int:
    app = _app(args)
    result = app.maintenance.run(force=True, budget=args.budget or app.settings.tick_budget_seconds)
    if result is None:
        print("Another cleanup is running; nothing done.")
        return 0
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_tick(args) -> int:
    app = _app(args)
    print(json.dumps(app.housekeeper.tick(force_cleanup=args.force), indent=2, default=str))
    return 0


def cmd_storage(args) -> int:
    from foxray.core.maintenance import storage_report
    app = _app(args)
    print(json.dumps(storage_report(app.store, app.settings, app.maintenance), indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="foxray", description="A light error tracker for the Sentry SDKs.")
    parser.add_argument("--config", help="INI file with a [foxray] section (env: FOXRAY_CONFIG)")
    parser.add_argument("--db", help="SQLite path or postgres:// URL (env: DATABASE_URL or FOXRAY_DATABASE_URL)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server (ingest, UI, alerts, cleanup)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--base-url", help="public URL, for DSNs and alert links")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("create-project", help="create a project and print its DSN")
    p.add_argument("name")
    p.add_argument("--base-url")
    p.set_defaults(fn=cmd_create_project)

    p = sub.add_parser("projects", help="list projects and their DSNs")
    p.add_argument("--base-url")
    p.set_defaults(fn=cmd_projects)

    p = sub.add_parser("create-user", help="add a UI user (or reset a password)")
    p.add_argument("email")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.add_argument("--password-env", action="store_true", help="read the password from FOXRAY_PASSWORD")
    p.set_defaults(fn=cmd_create_user)

    p = sub.add_parser("prune", help="run a full cleanup now")
    p.add_argument("--budget", type=float, help="seconds to spend (default: tick_budget_seconds)")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("tick", help="check monitors, clean up if due, send alerts (for cron)")
    p.add_argument("--force", action="store_true", help="clean up even if not due")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("storage", help="print the storage report")
    p.set_defaults(fn=cmd_storage)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
