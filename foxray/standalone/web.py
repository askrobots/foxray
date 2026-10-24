"""The standalone HTTP server: SDK ingest plus a small HTML UI behind a login.

Stdlib only: ``ThreadingHTTPServer`` with HTTP/1.1 keep-alive. The UI is
plain server-rendered HTML with no JavaScript, so the CSP can forbid scripts.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from foxray import __version__
from foxray.core.alerts import Links, Outbox
from foxray.core.dsn import make_dsn
from foxray.core.filters import RULE_KINDS, PatternError, validate_pattern
from foxray.core.ingest import Ingestor
from foxray.core.maintenance import Housekeeper, Maintenance, storage_report
from foxray.core.models import LEVELS
from foxray.core.present import (ago, day_bars, exceptions_for_display, kv_rows, reason_label, schedule_text,
                                 size_text, when)
from foxray.core.sql import IssueQuery
from foxray.core.util import UTC, days_ago_str, utcnow

from .mail import channels_for, senders
from .store import Sessions, StandaloneStore, open_store

log = logging.getLogger("foxray.http")
INGEST = re.compile(r"^(?:/[^/]+)*?/api/(\d+)/(envelope|store)/?$")
COOKIE = "foxray_session"


class App:
    """Everything one Foxray process needs, wired together."""

    def __init__(self, cfg, settings, store: StandaloneStore | None = None):
        self.cfg = cfg
        self.settings = settings
        self.store = store or open_store(cfg.database_url)
        if not settings.base_url:
            settings.base_url = f"http://{cfg.host}:{cfg.port}"
        self.links = Links(settings.base_url)
        self.channels_for = channels_for(cfg, settings)
        self.outbox = Outbox(self.store, settings, senders(cfg, settings))
        self.maintenance = Maintenance(self.store, settings, links=self.links,
                                       global_channels=self.channels_for(None))
        self.housekeeper = Housekeeper(self.store, settings, maintenance=self.maintenance, outbox=self.outbox,
                                       links=self.links, channels_for=self.channels_for)
        self.ingestor = Ingestor(self.store, settings, links=self.links, channels_for=self.channels_for,
                                 housekeeper=self.housekeeper)
        self.sessions = Sessions(self.store, self.store.secret(cfg.secret_key))

    def dsn(self, project) -> str:
        return make_dsn(self.settings.base_url, project.public_key, project.id)


# ---------------------------------------------------------------------------
# HTML

def h(value) -> str:
    # Braces are escaped too, so only our own "{csrf}" markers get the token.
    return "" if value is None else html.escape(str(value), quote=True).replace("{", "&#123;")


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2de;--card:#fff;--accent:#c2410c;--accent-fg:#fff;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--code:#f4f3f0;--hi:#fff4e5;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e6e1;--muted:#9c9a93;--line:#2e2d2a;--card:#1e1e1c;
--accent:#fb923c;--accent-fg:#1d1d1b;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--code:#232320;--hi:#3a2a16}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,
Segoe UI,sans-serif}a{color:inherit}main{max-width:1180px;margin:0 auto;padding:16px}
header{border-bottom:1px solid var(--line);background:var(--card)}header .in{max-width:1180px;margin:0 auto;
padding:10px 16px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}header b{color:var(--accent);font-size:16px}
nav a{text-decoration:none;color:var(--muted);margin-right:14px}nav a.on{color:var(--fg);font-weight:600}
.right{margin-left:auto;color:var(--muted)}h1{font-size:20px;margin:6px 0 4px;word-break:break-word}
h2{font-size:15px;margin:22px 0 8px}.muted{color:var(--muted)}.small{font-size:12px}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line)}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums}
.t a{text-decoration:none;font-weight:600}.t a:hover{text-decoration:underline}
.badge{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;border:1px solid var(--line)}
.fatal,.error,.missed,.timeout,.failed{color:var(--bad)}.warning,.pending,.in_progress{color:var(--warn)}
.ok,.resolved,.sent{color:var(--ok)}.banner{background:var(--hi);border:1px solid var(--warn);padding:10px 12px;
margin:0 0 14px;border-radius:6px}.f{display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:0 0 12px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
input,select,button{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:var(--card);
color:var(--fg)}button{cursor:pointer}button.p{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row form{margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:10px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.stat b{display:block;font-size:17px}.bars{display:flex;gap:2px;align-items:end;height:60px;background:var(--card);
border:1px solid var(--line);padding:6px;border-radius:6px}.bars i{flex:1;background:var(--accent);min-width:3px;
border-radius:2px 2px 0 0;opacity:.85}.bars i.z{background:var(--line);height:2px!important}
.frame{border:1px solid var(--line);border-radius:6px;margin:6px 0;background:var(--card)}
.frame summary{padding:6px 9px;cursor:pointer;font-family:var(--mono);font-size:12.5px}
.frame.lib summary{color:var(--muted)}pre{margin:0;padding:6px 0;background:var(--code);overflow-x:auto;
font:12px/1.5 var(--mono)}pre span{display:block;padding:0 9px;white-space:pre}pre span.cur{background:var(--hi)}
pre em{color:var(--muted);font-style:normal;display:inline-block;min-width:3.5em}
.exc{margin:12px 0}.exc h3{font-size:15px;margin:0 0 4px;word-break:break-word}
input.dsn{width:100%;font-family:var(--mono);font-size:12px}.meter{height:10px;background:var(--line);border-radius:5px;
overflow:hidden}.meter i{display:block;height:100%;background:var(--accent)}
.more{margin:-4px 0 12px}.more summary{cursor:pointer;margin-bottom:8px}
.login{max-width:340px;margin:60px auto;background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:20px}.login form{display:flex;flex-direction:column;gap:10px}
@media (max-width:700px){.hide-s{display:none}td,th{padding:6px}}
"""


def page(title: str, body: str, *, user=None, active: str = "", banner: str = "") -> str:
    nav = ""
    if user:
        items = [("issues", "/issues", "Issues"), ("monitors", "/monitors", "Monitors"),
                 ("projects", "/projects", "Projects"), ("alerts", "/alerts", "Alerts"),
                 ("storage", "/storage", "Storage")]
        links = "".join(f'<a href="{u}" class="{"on" if k == active else ""}">{t}</a>' for k, u, t in items)
        nav = (f'<nav>{links}</nav><span class="right small">{h(user[1])} '
               f'<form method="post" action="/logout" style="display:inline"><input type="hidden" name="csrf" '
               f'value="{{csrf}}"><button class="small">Log out</button></form></span>')
    banner_html = f'<div class="banner">{banner}</div>' if banner else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{h(title)} · Foxray</title><style>{CSS}</style>'
            f'</head><body><header><div class="in"><b>Foxray</b>{nav}</div></header><main>{banner_html}{body}'
            f'</main></body></html>')


def level_badge(level: str) -> str:
    return f'<span class="badge {h(level)}">{h(level)}</span>'


def status_badge(status: str) -> str:
    return f'<span class="badge {h(status)}">{h(status)}</span>'


def options(values, selected, labels=None) -> str:
    out = []
    for v in values:
        label = (labels or {}).get(v, v) if v != "" else (labels or {}).get("", "any")
        out.append(f'<option value="{h(v)}"{" selected" if str(v) == str(selected) else ""}>{h(label)}</option>')
    return "".join(out)


def _date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Request handling


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"Foxray/{__version__}"
    app: App = None  # set by make_server

    # quieter logs: ingest at debug, the rest at info
    def log_message(self, fmt, *args):
        (log.debug if "/api/" in (self.path or "") else log.info)("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str):
        self._csrf = ""
        try:
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            m = INGEST.match(url.path)
            if m:
                return self._ingest(method, m.group(1), m.group(2), query)
            if url.path == "/healthz":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return self._ui(method, url.path, query)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("error handling %s %s", method, self.path)
            try:
                self._send(500, b"internal error\n", "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    # -- plumbing ----------------------------------------------------------

    def _read_body(self, limit: int) -> bytes | None:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(1024)
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                total += size
                if total > limit:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(8)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > limit:
            return None
        return self.rfile.read(n) if n > 0 else b""

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _ingest(self, method: str, project_id: str, kind: str, query: dict):
        app = self.app
        if method == "OPTIONS":
            r = app.ingestor.preflight()
            return self._send(r.status, r.body, "text/plain", r.headers)
        if method != "POST":
            return self._send(405, b'{"detail":"use POST"}', "application/json", {"Allow": "POST, OPTIONS"})
        body = self._read_body(app.settings.max_body_bytes)
        if body is None:
            self.close_connection = True
            return self._send(413, b'{"detail":"body too large"}', "application/json")
        headers = {k.lower(): v for k, v in self.headers.items()}
        r = app.ingestor.handle(project_id, kind, headers, query, body)
        ctype = r.headers.pop("Content-Type", "application/json")
        self._send(r.status, r.body, ctype, r.headers)
        if r.after is not None:  # cleanup, monitor checks, alerts: after the SDK has its answer
            threading.Thread(target=r.after, name="foxray-after", daemon=True).start()

    def _cookie(self) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:  # noqa: BLE001
            return ""
        return jar[COOKIE].value if COOKIE in jar else ""

    def _secure(self) -> bool:
        if self.app.settings.base_url.startswith("https://"):
            return True
        return self.app.cfg.trust_proxy and (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _html(self, status: int, text: str, headers: dict | None = None):
        text = text.replace("{csrf}", h(self._csrf))
        hdrs = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                           "frame-ancestors 'none'; base-uri 'none'",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                "Cache-Control": "no-store"}
        hdrs.update(headers or {})
        self._send(status, text.encode(), "text/html; charset=utf-8", hdrs)

    def _redirect(self, location: str, headers: dict | None = None):
        hdrs = {"Location": location}
        hdrs.update(headers or {})
        self._send(303, b"", "text/plain", hdrs)

    def _form(self) -> dict | None:
        body = self._read_body(256 * 1024)
        if body is None:
            self.close_connection = True
            return None
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    # -- UI ----------------------------------------------------------------

    def _ui(self, method: str, path: str, query: dict):
        app = self.app
        cookie = self._cookie()
        if path == "/login":
            return self._login(method, query)
        user = app.sessions.user(cookie)
        if user is None:
            return self._redirect("/login?next=" + quote(path))
        self._csrf = app.sessions.csrf(cookie)
        form: dict = {}
        if method == "POST":
            form = self._form()
            if form is None:
                return self._html(413, page("Too large", "<p>That form was too large.</p>", user=user))
            if not hmac.compare_digest(form.get("csrf", ""), self._csrf):
                return self._html(403, page("Forbidden", "<p>The form expired. Go back and try again.</p>", user=user))
            if path == "/logout":
                return self._redirect("/login", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        elif method != "GET":
            return self._send(405, b"", "text/plain")
        routes = [
            (r"/", self.p_root), (r"/issues", self.p_issues), (r"/issues/(\d+)", self.p_issue),
            (r"/monitors", self.p_monitors), (r"/monitors/(\d+)", self.p_monitor),
            (r"/projects", self.p_projects), (r"/projects/(\d+)", self.p_project),
            (r"/alerts", self.p_alerts), (r"/storage", self.p_storage),
        ]
        for pattern, fn in routes:
            m = re.fullmatch(pattern, path.rstrip("/") or "/")
            if m:
                return fn(method, user, query, form, *m.groups())
        return self._html(404, page("Not found", "<h1>Not found</h1>", user=user))

    def _banner(self) -> str:
        if self.app.store.get_state_value("mode", "normal") == "counts_only":
            return ('<b>Storage is full.</b> Foxray is counting and alerting, but not keeping new event bodies '
                    'until there is room. <a href="/storage">Storage</a>')
        return ""

    def _render(self, title, body, user, active, status=200):
        self._html(status, page(title, body, user=user, active=active, banner=self._banner()))

    def _login(self, method: str, query: dict):
        app = self.app
        error = ""
        def safe_next(value):
            value = value or "/issues"
            ok = value.startswith("/") and not value.startswith(("//", "/\\")) and "\r" not in value \
                and "\n" not in value
            return value if ok else "/issues"
        nxt = safe_next(query.get("next"))
        if method == "POST":
            form = self._form() or {}
            nxt = safe_next(form.get("next") or nxt)
            row = app.store.user_by_email(form.get("email", ""))
            from .store import verify_password
            if row and verify_password(form.get("password", ""), row[2]):
                secure = "; Secure" if self._secure() else ""
                cookie = f"{COOKIE}={app.sessions.make(row)}; Path=/; Max-Age={app.sessions.max_age}; HttpOnly; " \
                         f"SameSite=Lax{secure}"
                return self._redirect(nxt, {"Set-Cookie": cookie})
            time.sleep(1.0)  # slow down guessing
            error = '<p class="bad">Wrong email or password.</p>'
        hint = ""
        if app.store.user_count() == 0:
            hint = '<p class="muted small">No users yet. Create one with <code>foxray create-user you@example.com</code>.</p>'
        body = (f'<div class="login"><h1>Foxray</h1>{error}{hint}<form method="post" action="/login">'
                f'<input type="hidden" name="next" value="{h(nxt)}">'
                f'<label>Email<input name="email" type="email" autocomplete="username" required autofocus></label>'
                f'<label>Password<input name="password" type="password" autocomplete="current-password" required>'
                f'</label><button class="p">Log in</button></form></div>')
        self._html(200 if not error else 401, page("Log in", body))

    def p_root(self, method, user, query, form):
        self._redirect("/issues")

    # -- issues --------------------------------------------------------------

    def p_issues(self, method, user, query, form):
        store = self.app.store
        projects = store.list_projects()
        q = IssueQuery(
            project_id=int(query["project"]) if query.get("project", "").isdigit() else None,
            status=query.get("status", "unresolved"), level=query.get("level", ""),
            environment=query.get("environment", ""), release=query.get("release", ""),
            first_seen_from=_date(query.get("first_from", "")), last_seen_from=_date(query.get("last_from", "")),
            text=query.get("q", "").strip()[:200], sort=query.get("sort", "last_seen"), limit=50,
            offset=max(0, int(query["offset"])) if query.get("offset", "").isdigit() else 0)
        to = _date(query.get("first_to", ""))
        q.first_seen_to = to + timedelta(days=1) if to else None
        to = _date(query.get("last_to", ""))
        q.last_seen_to = to + timedelta(days=1) if to else None
        issues, total = store.list_issues(q)
        names = {p.id: p.name for p in projects}
        now = utcnow()
        envs = store.tag_values("environment", q.project_id)
        rels = store.tag_values("release", q.project_id)
        f = query.get
        more_set = any(f(k) for k in ("project", "environment", "release", "first_from", "first_to", "last_from",
                                      "last_to"))
        project_select = (f'<label>Project<select name="project">{options([""] + [p.id for p in projects], q.project_id or "", {"": "any", **names})}</select></label>'
                          if len(projects) > 1 else "")
        filters = (
            '<form method="get" action="/issues"><div class="f">'
            f'<label>Search<input name="q" value="{h(f("q", ""))}" placeholder="title or where"></label>'
            f'<label>Status<select name="status">{options(["unresolved", "resolved", "ignored", "all"], q.status)}'
            f'</select></label>'
            f'<label>Level<select name="level">{options([""] + list(LEVELS), q.level)}</select></label>'
            f'<label>Sort<select name="sort">{options(["last_seen", "first_seen", "count", "users"], q.sort, {"last_seen": "last seen", "first_seen": "first seen", "count": "count", "users": "users"})}</select></label>'
            '<button class="p">Filter</button></div>'
            f'<details class="more"{" open" if more_set else ""}><summary class="small muted">More filters</summary>'
            f'<div class="f">{project_select}'
            f'<label>Environment<select name="environment">{options([""] + envs, q.environment)}</select></label>'
            f'<label>Release<select name="release">{options([""] + rels, q.release)}</select></label>'
            f'<label>First seen from<input type="date" name="first_from" value="{h(f("first_from", ""))}"></label>'
            f'<label>to<input type="date" name="first_to" value="{h(f("first_to", ""))}"></label>'
            f'<label>Last seen from<input type="date" name="last_from" value="{h(f("last_from", ""))}"></label>'
            f'<label>to<input type="date" name="last_to" value="{h(f("last_to", ""))}"></label>'
            '</div></details></form>')
        rows = []
        for i in issues:
            proj = f'<span class="badge">{h(names.get(i.project_id, ""))}</span> ' if len(projects) > 1 else ""
            rows.append(
                f'<tr><td class="t">{proj}<a href="/issues/{i.id}">{h(i.title)}</a><div class="small muted">'
                f'{h(i.culprit)}</div></td><td>{level_badge(i.level)}</td><td class="n">{i.times_seen}</td>'
                f'<td class="n hide-s">{i.users_seen}</td><td class="hide-s" title="{h(when(i.first_seen))}">'
                f'{ago(i.first_seen, now)}</td><td title="{h(when(i.last_seen))}">{ago(i.last_seen, now)}</td>'
                f'<td class="hide-s">{status_badge(i.status)}</td></tr>')
        if not rows:
            rows.append('<tr><td colspan="7" class="muted">No issues match. '
                        + ('Point an SDK at a DSN from <a href="/projects">Projects</a>.' if total == 0 and q.status == "unresolved" and not q.text else "")
                        + '</td></tr>')
        pager = ""
        if total > q.limit:
            base = {k: v for k, v in query.items() if k != "offset"}
            prev_off, next_off = max(0, q.offset - q.limit), q.offset + q.limit
            pager = '<p class="row">'
            if q.offset > 0:
                pager += f'<a href="/issues?{h(urlencode({**base, "offset": prev_off}))}">Newer</a>'
            pager += f'<span class="muted">{q.offset + 1}-{min(total, q.offset + q.limit)} of {total}</span>'
            if next_off < total:
                pager += f'<a href="/issues?{h(urlencode({**base, "offset": next_off}))}">Older</a>'
            pager += "</p>"
        body = (f'<h1>Issues <span class="muted small">{total}</span></h1>{filters}<table><thead><tr><th>Issue</th>'
                f'<th>Level</th><th class="n">Count</th><th class="n hide-s">Users</th><th class="hide-s">First seen'
                f'</th><th>Last seen</th><th class="hide-s">Status</th></tr></thead><tbody>{"".join(rows)}</tbody>'
                f'</table>{pager}')
        self._render("Issues", body, user, "issues")

    def p_issue(self, method, user, query, form, issue_id):
        store = self.app.store
        issue = store.get_issue(issue_id)
        if issue is None:
            return self._render("Not found", "<h1>No such issue</h1>", user, "issues", 404)
        now = utcnow()
        if method == "POST":
            action = form.get("action")
            if action == "resolve":
                store.set_issue_status(issue.id, "resolved", now=now)
            elif action == "reopen":
                store.set_issue_status(issue.id, "unresolved", now=now)
            elif action == "ignore":
                store.set_issue_status(issue.id, "ignored", now=now)
            elif action == "ignore_count" and form.get("count", "").isdigit() and int(form["count"]) > 0:
                store.set_issue_status(issue.id, "ignored", now=now, ignore_more=int(form["count"]))
            elif action == "ignore_until" and _date(form.get("until", "")):
                store.set_issue_status(issue.id, "ignored", now=now, ignore_until=_date(form["until"]))
            return self._redirect(f"/issues/{issue.id}")
        project = store.get_project(issue.project_id)
        event = store.get_event(query["event"]) if query.get("event", "").isdigit() else None
        if event is None or event.issue_id != issue.id:
            event = store.latest_event(issue.id)
        tags = store.issue_tags(issue.id)
        bars = day_bars(store.issue_days(issue.id, days_ago_str(now, 30)), now)
        btn = lambda action, label, cls="": (  # noqa: E731
            f'<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" '
            f'name="action" value="{action}"><button class="{cls}">{label}</button></form>')
        if issue.status == "unresolved":
            actions = btn("resolve", "Resolve", "p") + btn("ignore", "Ignore")
            actions += ('<form method="post" class="row"><input type="hidden" name="csrf" value="{csrf}">'
                        '<input type="hidden" name="action" value="ignore_count"><input name="count" type="number" '
                        'min="1" value="100" style="width:6em" aria-label="more events"><button>Ignore until this many more</button></form>'
                        '<form method="post" class="row"><input type="hidden" name="csrf" value="{csrf}">'
                        '<input type="hidden" name="action" value="ignore_until"><input name="until" type="date" '
                        'aria-label="date" required><button>Ignore until</button></form>')
        else:
            note = ""
            if issue.status == "ignored" and issue.ignore_until_count:
                note = f" until {issue.ignore_until_count} events ({max(0, issue.ignore_until_count - issue.times_seen)} more)"
            elif issue.status == "ignored" and issue.ignore_until:
                note = f" until {when(issue.ignore_until)}"
            actions = f'<span class="muted">{h(issue.status.capitalize())}{h(note)}</span>' + btn("reopen", "Reopen", "p")
        tag_html = "".join(
            f'<div class="stat"><span class="muted small">{h(key)}</span>' +
            "".join(f'<div class="small">{h(v)} <span class="muted">{n}</span></div>' for v, n in values[:6]) + "</div>"
            for key, values in tags.items())
        chart = "".join(f'<i class="{"z" if not b["n"] else ""}" style="height:{b["pct"]}%" '
                        f'title="{b["day"]}: {b["n"]}"></i>' for b in bars)
        body = (f'<p class="small muted"><a href="/issues">Issues</a> · {h(project.name if project else "")}</p>'
                f'<h1>{h(issue.title)}</h1><p class="muted">{h(issue.culprit)}</p>'
                f'<div class="row">{level_badge(issue.level)} {status_badge(issue.status)} {actions}</div>'
                f'<div class="grid"><div class="stat"><span class="muted small">Events</span><b>{issue.times_seen}</b></div>'
                f'<div class="stat"><span class="muted small">Users</span><b>{issue.users_seen}</b></div>'
                f'<div class="stat"><span class="muted small">First seen</span><b title="{h(when(issue.first_seen))}">{ago(issue.first_seen, now)}</b></div>'
                f'<div class="stat"><span class="muted small">Last seen</span><b title="{h(when(issue.last_seen))}">{ago(issue.last_seen, now)}</b></div>'
                f'{tag_html}</div><h2>Last 30 days</h2><div class="bars">{chart}</div>')
        body += self._event_html(event, issue)
        events = store.list_events(issue.id, 15)
        if len(events) > 1:
            body += "<h2>Recent events</h2><table><tbody>" + "".join(
                f'<tr><td><a href="/issues/{issue.id}?event={e.id}">{h(e.event_id[:12])}</a></td>'
                f'<td>{h(when(e.received_at))}</td><td>{h(e.environment)}</td><td>{h(e.release)}</td></tr>'
                for e in events) + "</tbody></table>"
        self._render(issue.title, body, user, "issues")

    def _event_html(self, event, issue) -> str:
        if event is None:
            return ('<h2>Latest event</h2><p class="muted">No event body is kept for this issue (sampled, '
                    'past retention, or the store was full). The counts above are complete.</p>')
        ev = event.payload
        out = [f'<h2>Event <span class="muted small">{h(event.event_id)} · {h(when(event.received_at))}</span></h2>']
        if ev.get("message") and not ev.get("exception"):
            out.append(f'<pre><span>{h(ev["message"])}</span></pre>')
        for exc in exceptions_for_display(ev):
            mech = exc["mechanism"]
            handled = "" if "handled" not in mech else (" · handled" if mech["handled"] else " · unhandled")
            out.append(f'<div class="exc"><h3>{h(exc["type"])}<span class="muted">{": " if exc["value"] else ""}'
                       f'{h(exc["value"])}</span></h3><div class="small muted">{h(mech.get("type", ""))}{h(handled)}</div>')
            for i, fr in enumerate(exc["frames"]):
                lines = "".join(
                    f'<span class="{"cur" if cur else ""}"><em>{"" if n is None else n}</em>{h(text)}</span>'
                    for n, text, cur in fr["lines"])
                vars_html = ""
                if fr["vars"]:
                    vars_html = "<table>" + "".join(f"<tr><td>{h(k)}</td><td><code>{h(v)}</code></td></tr>"
                                                    for k, v in kv_rows(fr["vars"])) + "</table>"
                open_ = " open" if fr["in_app"] and i < 3 else ""
                cls = "" if fr["in_app"] else " lib"
                out.append(f'<details class="frame{cls}"{open_}><summary>{h(fr["label"])} <span class="muted">'
                           f'{h(fr["path"])}</span></summary>{"<pre>" + lines + "</pre>" if lines else ""}{vars_html}'
                           f'</details>')
            out.append("</div>")
        crumbs = ev.get("breadcrumbs") or []
        if crumbs:
            out.append("<h2>Breadcrumbs</h2><table><tbody>" + "".join(
                f'<tr><td class="small muted">{h((c.get("timestamp") or "")[11:19])}</td><td>{h(c.get("category", ""))}'
                f'</td><td class="{h(c.get("level", ""))}">{h(c.get("message", ""))}'
                f'{(" <span class=small muted>" + h(json.dumps(c["data"])[:200]) + "</span>") if c.get("data") else ""}'
                f'</td></tr>' for c in reversed(crumbs)) + "</tbody></table>")
        sections = [("Request", ev.get("request")), ("User", ev.get("user")), ("Tags", ev.get("tags")),
                    ("Context", ev.get("contexts")), ("Extra", ev.get("extra")),
                    ("Event", {k: ev.get(k) for k in ("level", "platform", "environment", "release", "server_name",
                                                      "transaction", "logger") if ev.get(k)})]
        for title, value in sections:
            rows = kv_rows(value) if isinstance(value, dict) else []
            if rows:
                out.append(f"<h2>{title}</h2><table><tbody>" + "".join(
                    f'<tr><td class="muted" style="width:28%">{h(k)}</td><td><code>{h(v)}</code></td></tr>'
                    for k, v in rows) + "</tbody></table>")
        return "".join(out)

    # -- monitors ------------------------------------------------------------

    def p_monitors(self, method, user, query, form):
        store = self.app.store
        now = utcnow()
        names = {p.id: p.name for p in store.list_projects()}
        rows = "".join(
            f'<tr><td class="t"><a href="/monitors/{m.id}">{h(m.slug)}</a> <span class="muted small">'
            f'{h(names.get(m.project_id, ""))}</span></td><td>{status_badge(m.status if m.active else "paused")}</td>'
            f'<td>{h(schedule_text(m))}</td><td>{ago(m.last_checkin_at, now)}</td>'
            f'<td class="hide-s">{ago(m.next_due_at, now) if m.next_due_at else "-"}</td></tr>'
            for m in store.list_monitors()) or ('<tr><td colspan="5" class="muted">No monitors yet. They appear with '
                                               'the first check-in from <code>sentry_sdk.crons.monitor</code>.</td></tr>')
        body = (f'<h1>Monitors</h1><table><thead><tr><th>Monitor</th><th>Status</th><th>Schedule</th>'
                f'<th>Last check-in</th><th class="hide-s">Deadline</th></tr></thead><tbody>{rows}</tbody></table>')
        self._render("Monitors", body, user, "monitors")

    def p_monitor(self, method, user, query, form, monitor_id):
        store = self.app.store
        m = store.get_monitor_by_id(monitor_id)
        if m is None:
            return self._render("Not found", "<h1>No such monitor</h1>", user, "monitors", 404)
        if method == "POST":
            if form.get("action") in ("pause", "resume"):
                store.update_monitor(m.id, active=form["action"] == "resume")
            return self._redirect(f"/monitors/{m.id}")
        now = utcnow()
        toggle = "resume" if not m.active else "pause"
        checkins = "".join(
            f'<tr><td>{status_badge(c.status)}</td><td>{h(when(c.received_at))}</td>'
            f'<td class="n">{"" if c.duration is None else f"{c.duration:.1f}s"}</td><td>{h(c.environment)}</td></tr>'
            for c in store.list_checkins(m.id, 50))
        body = (f'<p class="small muted"><a href="/monitors">Monitors</a></p><h1>{h(m.slug)}</h1>'
                f'<div class="row">{status_badge(m.status if m.active else "paused")}<form method="post">'
                f'<input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="action" value="{toggle}">'
                f'<button>{toggle.capitalize()} alerts</button></form></div>'
                f'<div class="grid"><div class="stat"><span class="muted small">Schedule</span><b>{h(schedule_text(m))}</b></div>'
                f'<div class="stat"><span class="muted small">Margin / max runtime</span><b>{m.checkin_margin}m / {m.max_runtime}m</b></div>'
                f'<div class="stat"><span class="muted small">Last check-in</span><b>{ago(m.last_checkin_at, now)}</b></div>'
                f'<div class="stat"><span class="muted small">Deadline</span><b>{h(when(m.next_due_at))}</b></div></div>'
                f'<h2>Check-ins</h2><table><thead><tr><th>Status</th><th>When</th><th class="n">Duration</th>'
                f'<th>Environment</th></tr></thead><tbody>{checkins}</tbody></table>')
        self._render(m.slug, body, user, "monitors")

    # -- projects ------------------------------------------------------------

    def p_projects(self, method, user, query, form):
        store = self.app.store
        if method == "POST":
            name = (form.get("name") or "").strip()[:100]
            if name:
                p = store.create_project(name)
                return self._redirect(f"/projects/{p.id}")
            return self._redirect("/projects")
        rows = "".join(
            f'<tr><td class="t"><a href="/projects/{p.id}">{h(p.name)}</a></td><td><input class="dsn" readonly '
            f'value="{h(self.app.dsn(p))}" aria-label="DSN"></td></tr>' for p in store.list_projects())
        body = (f'<h1>Projects</h1><table><thead><tr><th>Project</th><th>DSN</th></tr></thead><tbody>{rows}</tbody>'
                f'</table><h2>New project</h2><form class="f" method="post"><input type="hidden" name="csrf" '
                f'value="{{csrf}}"><label>Name<input name="name" required maxlength="100"></label>'
                f'<button class="p">Create</button></form>')
        self._render("Projects", body, user, "projects")

    _INT_FIELDS = ("rate_limit", "sample_after", "event_retention_days", "events_per_issue", "issue_retention_days")
    _BOOL_FIELDS = ("alert_new", "alert_regression", "filter_crawlers", "filter_js_noise", "filter_health_checks")
    _TEXT_FIELDS = ("name", "alert_min_level", "alert_environments", "alert_emails", "alert_webhook",
                    "health_check_names")

    def p_project(self, method, user, query, form, project_id):
        store = self.app.store
        p = store.get_project(project_id)
        if p is None:
            return self._render("Not found", "<h1>No such project</h1>", user, "projects", 404)
        error = ""
        if method == "POST":
            action = form.get("action")
            if action == "settings":
                changes = {}
                for k in self._INT_FIELDS:
                    v = (form.get(k) or "").strip()
                    changes[k] = int(v) if v.isdigit() else None
                for k in self._BOOL_FIELDS:
                    changes[k] = form.get(k) == "on"
                for k in self._TEXT_FIELDS:
                    changes[k] = (form.get(k) or "").strip()[:1000]
                if changes["alert_min_level"] not in ("",) + LEVELS:
                    changes["alert_min_level"] = ""
                if not changes["name"]:
                    changes.pop("name")
                store.update_project(p.id, **changes)
                return self._redirect(f"/projects/{p.id}")
            if action == "add_filter":
                kind = form.get("kind")
                is_regex = form.get("is_regex") == "on"
                try:
                    if kind not in RULE_KINDS:
                        raise PatternError("pick what to match")
                    validate_pattern(form.get("pattern", ""), is_regex)
                    store.add_filter(p.id, kind, form["pattern"].strip(), is_regex=is_regex,
                                     environment=form.get("environment", "").strip()[:64],
                                     release=form.get("release", "").strip()[:200],
                                     note=form.get("note", "").strip()[:200])
                    return self._redirect(f"/projects/{p.id}#filters")
                except PatternError as exc:
                    error = f'<p class="banner">{h(exc)}</p>'
            elif action in ("toggle_filter", "delete_filter") and form.get("filter", "").isdigit():
                rule = next((r for r in store.list_filters(p.id) if r.id == int(form["filter"])), None)
                if rule and action == "toggle_filter":
                    store.update_filter(rule.id, enabled=not rule.enabled)
                elif rule:
                    store.delete_filter(rule.id)
                return self._redirect(f"/projects/{p.id}#filters")
        s = self.app.settings
        val = lambda k: "" if getattr(p, k) is None else getattr(p, k)  # noqa: E731
        num = lambda k, label, default: (  # noqa: E731
            f'<label>{label}<input name="{k}" value="{h(val(k))}" inputmode="numeric" placeholder="{default}" '
            f'style="width:8em"></label>')
        chk = lambda k, label: (  # noqa: E731
            f'<label style="flex-direction:row;gap:6px;align-items:center"><input type="checkbox" name="{k}"'
            f'{" checked" if getattr(p, k) else ""}>{label}</label>')
        txt = lambda k, label, ph="", w="16em": (  # noqa: E731
            f'<label>{label}<input name="{k}" value="{h(val(k))}" placeholder="{h(ph)}" style="width:{w}"></label>')
        rules = store.list_filters(p.id)
        rule_rows = "".join(
            f'<tr><td>{h(r.kind)}</td><td><code>{h(r.pattern)}</code>{" <span class=muted>(regex)</span>" if r.is_regex else ""}'
            f'</td><td>{h(r.environment or "any")}</td><td>{h(r.release or "any")}</td><td>{h(r.note)}</td>'
            f'<td class="row"><form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" '
            f'name="action" value="toggle_filter"><input type="hidden" name="filter" value="{r.id}"><button>'
            f'{"Turn off" if r.enabled else "Turn on"}</button></form><form method="post"><input type="hidden" '
            f'name="csrf" value="{{csrf}}"><input type="hidden" name="action" value="delete_filter"><input '
            f'type="hidden" name="filter" value="{r.id}"><button>Remove</button></form></td></tr>'
            for r in rules) or '<tr><td colspan="6" class="muted">No rules.</td></tr>'
        by_id = {r.id: r for r in rules}
        counts = store.counters(p.id, days_ago_str(utcnow(), 2))
        count_rows = "".join(f'<tr><td>{h(day)}</td><td>{h(reason_label(reason, by_id))}</td><td class="n">{n}</td></tr>'
                             for _, day, reason, n in counts) or '<tr><td colspan="3" class="muted">Nothing dropped.</td></tr>'
        body = (
            f'<p class="small muted"><a href="/projects">Projects</a></p><h1>{h(p.name)}</h1>{error}'
            f'<h2>DSN</h2><input class="dsn" readonly value="{h(self.app.dsn(p))}" aria-label="DSN">'
            f'<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="action" value="settings">'
            f'<h2>Alerts</h2><div class="f">{chk("alert_new", "New issues")}{chk("alert_regression", "Regressions")}'
            f'<label>Minimum level<select name="alert_min_level">{options([""] + list(LEVELS), p.alert_min_level, {"": "any"})}</select></label>'
            f'{txt("alert_environments", "Environments", "all, or e.g. production")}'
            f'{txt("alert_emails", "Email to", ", ".join(s.alert_emails) or "you@example.com", "20em")}'
            f'{txt("alert_webhook", "Webhook URL", s.alert_webhook or "https://...", "20em")}</div>'
            f'<h2>Noise</h2><div class="f">{chk("filter_crawlers", "Drop crawlers and bots")}'
            f'{chk("filter_health_checks", "Drop health checks")}{chk("filter_js_noise", "Drop browser-extension and localhost JS errors")}'
            f'{txt("health_check_names", "More health-check paths", "/up, /alive")}</div>'
            f'<h2>Limits</h2><p class="small muted">Empty means the server default.</p><div class="f">'
            f'{num("rate_limit", "Events a minute", s.rate_limit_per_minute)}'
            f'{num("sample_after", "Bodies per issue a day", s.sample_after)}'
            f'{num("events_per_issue", "Bodies kept per issue", s.events_per_issue)}'
            f'{num("event_retention_days", "Keep bodies (days)", s.event_retention_days)}'
            f'{num("issue_retention_days", "Keep closed issues (days)", s.issue_retention_days)}'
            f'{txt("name", "Name")}</div><button class="p">Save</button></form>'
            f'<h2 id="filters">Inbound filters</h2><p class="small muted">Matching events get a 200 and are counted, not stored.</p>'
            f'<table><thead><tr><th>Match</th><th>Pattern</th><th>Environment</th><th>Release</th><th>Note</th><th></th></tr>'
            f'</thead><tbody>{rule_rows}</tbody></table>'
            f'<form class="f" method="post" style="margin-top:8px"><input type="hidden" name="csrf" value="{{csrf}}">'
            f'<input type="hidden" name="action" value="add_filter"><label>Match<select name="kind">'
            f'{options(list(RULE_KINDS), "exception", {"exception": "exception type", "message": "message", "environment": "environment", "release": "release"})}'
            f'</select></label><label>Pattern<input name="pattern" required maxlength="200" placeholder="Http404"></label>'
            f'<label style="flex-direction:row;gap:6px;align-items:center"><input type="checkbox" name="is_regex">regex</label>'
            f'<label>Only environment<input name="environment" style="width:9em"></label>'
            f'<label>Only release<input name="release" style="width:9em"></label>'
            f'<label>Note<input name="note"></label><button>Add rule</button></form>'
            f'<h2>Dropped, last 2 days</h2><table><thead><tr><th>Day</th><th>Why</th><th class="n">Count</th></tr></thead>'
            f'<tbody>{count_rows}</tbody></table>')
        self._render(p.name, body, user, "projects")

    # -- alerts and storage ------------------------------------------------------

    def p_alerts(self, method, user, query, form):
        store = self.app.store
        rows = "".join(
            f'<tr><td>{status_badge(a.status)}</td><td>{h(a.subject)}<div class="small muted">{h(a.last_error)}</div></td>'
            f'<td>{h(a.channel)}</td><td class="n">{a.attempts}</td><td>{h(when(a.created_at))}</td></tr>'
            for a in store.list_alerts(100)) or '<tr><td colspan="5" class="muted">No alerts yet.</td></tr>'
        body = (f'<h1>Alerts</h1><p class="small muted">Sent after the event is stored; failed sends are retried for '
                f'{self.app.settings.failed_alert_retention_days} days.</p><table><thead><tr><th>Status</th><th>Alert</th>'
                f'<th>Via</th><th class="n">Tries</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table>')
        self._render("Alerts", body, user, "alerts")

    def p_storage(self, method, user, query, form):
        app = self.app
        if method == "POST" and form.get("action") == "cleanup":
            app.maintenance.run(force=True, budget=app.settings.tick_budget_seconds)
            return self._redirect("/storage")
        r = storage_report(app.store, app.settings, app.maintenance)
        pct = r["percent"] or 0
        last = r["last_cleanup"] or {}
        removed = last.get("removed") or {}
        removed_rows = "".join(f"<tr><td>{h(k.replace('_', ' '))}</td><td class='n'>{n}</td></tr>"
                               for k, n in removed.items()) or "<tr><td class='muted'>Nothing.</td></tr>"
        tables = "".join(f"<tr><td>{h(t.replace('foxray_', ''))}</td><td class='n'>{n}</td></tr>"
                         for t, n in r["tables"].items())
        mode = ("counting only (store full)" if r["mode"] == "counts_only" else "normal")
        body = (
            f'<h1>Storage</h1><div class="grid"><div class="stat"><span class="muted small">Used</span>'
            f'<b>{size_text(r["used_bytes"])}</b></div><div class="stat"><span class="muted small">Cap</span>'
            f'<b>{size_text(r["cap_bytes"]) if r["cap_bytes"] else "none"}</b></div><div class="stat"><span '
            f'class="muted small">Mode</span><b class="{"bad" if r["mode"] == "counts_only" else "ok"}">{mode}</b></div>'
            f'<div class="stat"><span class="muted small">Last cleanup</span><b>{ago(r["last_cleanup_at"])}</b></div>'
            f'<div class="stat"><span class="muted small">Database</span><b>{h(r["dialect"])}</b>'
            f'<span class="small muted">file {size_text(r["file_bytes"])}</span></div></div>'
            f'<div class="meter" title="{pct}%"><i style="width:{min(100, pct)}%"></i></div>'
            f'<p class="small muted">Cleanup runs by itself at most every {app.settings.cleanup_interval_seconds // 60} '
            f'minutes. Over 90% of the cap the oldest event bodies go until it is under 80%; issues and counts stay.</p>'
            f'<form method="post"><input type="hidden" name="csrf" value="{{csrf}}"><input type="hidden" name="action" '
            f'value="cleanup"><button>Clean up now</button></form>'
            f'<h2>Last cleanup removed</h2><table><tbody>{removed_rows}</tbody></table>'
            f'<h2>Rows</h2><table><tbody>{tables}</tbody></table>'
            f'<p class="small muted">Alerts waiting to send: {r["pending_alerts"]}</p>')
        self._render("Storage", body, user, "storage")


def make_server(app: App, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("FoxrayHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host or app.cfg.host, app.cfg.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def start_ticker(app: App, stop: threading.Event) -> threading.Thread:
    """The standalone clock: monitors every minute, cleanup when due, alerts."""
    def loop():
        while not stop.wait(app.cfg.tick_seconds):
            try:
                app.housekeeper.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
    t = threading.Thread(target=loop, name="foxray-tick", daemon=True)
    t.start()
    return t
