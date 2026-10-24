"""The store: one SQL implementation for SQLite and Postgres.

All state lives here, none in process memory: issues and events, counters,
rate-limit windows, alert cooldowns, the alert outbox, monitor state and the
leases that keep two cleanups from overlapping. That is what makes several
processes (uvicorn workers, a cron ``tick``) give one answer.

SQL is written once with ``%s`` placeholders and runs over three kinds of
connection: the stdlib ``sqlite3`` module, ``psycopg`` (Postgres), and
Django's own connection (see ``foxray.django.store``). Counters are one
atomic upsert each (``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``),
which SQLite (3.35+) and Postgres both support.
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache

from .models import (Alert, CheckIn, Event, FilterRule, Issue, Monitor, Project, State,
                     column_kinds, column_names)
from .util import from_db_datetime, to_utc_text, utcnow

TABLES = (
    "foxray_project", "foxray_issue", "foxray_event", "foxray_issue_day", "foxray_issue_user",
    "foxray_issue_tag", "foxray_monitor", "foxray_checkin", "foxray_filter", "foxray_counter",
    "foxray_rate", "foxray_alert", "foxray_state",
)


# --------------------------------------------------------------------------
# Connections


class _Pool:
    def __init__(self, factory, size: int = 8):
        self._factory = factory
        self._size = size
        self._q: queue.LifoQueue = queue.LifoQueue()

    def get(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return self._factory()

    def put(self, conn) -> None:
        if self._q.qsize() < self._size:
            self._q.put(conn)
        else:
            conn.close()

    def close(self) -> None:
        while True:
            try:
                self._q.get_nowait().close()
            except queue.Empty:
                return


@lru_cache(maxsize=1024)
def _qmark(sql: str) -> str:
    return re.sub(r"%%|%s", lambda m: "%" if m.group(0) == "%%" else "?", sql)


class _SqliteCursor:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cur = None

    def execute(self, sql: str, params=()):
        self._cur = self._conn.execute(_qmark(sql), tuple(params))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class SqliteBackend:
    """One SQLite file in WAL mode. Writes take ``BEGIN IMMEDIATE`` so two
    writers queue instead of deadlocking; connections are pooled."""

    dialect = "sqlite"

    def __init__(self, path: str):
        if path in ("", ":memory:"):
            raise ValueError("Foxray needs a SQLite file, not an in-memory database")
        self.path = path
        self._local = threading.local()
        self._pool = _Pool(self._connect)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def adapt(self, value):
        if isinstance(value, datetime):
            return to_utc_text(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    @contextmanager
    def tx(self):
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is not None:
            yield _SqliteCursor(conn)
            return
        conn = self._pool.get()
        local.conn = conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteCursor(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            local.conn = None
            self._pool.put(conn)

    @contextmanager
    def cursor(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield _SqliteCursor(conn)
            return
        conn = self._pool.get()
        try:
            yield _SqliteCursor(conn)
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        self._pool.close()


class PostgresBackend:
    """Postgres through psycopg 3 (``pip install foxray[postgres]``)."""

    dialect = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg  # noqa: F401
        except ImportError:  # pragma: no cover
            raise RuntimeError("Postgres needs psycopg: pip install 'foxray[postgres]'") from None
        self.url = url
        self._local = threading.local()
        self._pool = _Pool(self._connect)

    def _connect(self):
        import psycopg
        return psycopg.connect(self.url, autocommit=True)

    def adapt(self, value):
        return value

    def _release(self, conn) -> None:
        if conn.closed or getattr(conn, "broken", False):
            try:
                conn.close()
            except Exception:
                pass
        else:
            self._pool.put(conn)

    @contextmanager
    def tx(self):
        local = self._local
        conn = getattr(local, "conn", None)
        if conn is not None:
            with conn.cursor() as cur:
                yield cur
            return
        conn = self._pool.get()
        local.conn = conn
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    yield cur
        finally:
            local.conn = None
            self._release(conn)

    @contextmanager
    def cursor(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with conn.cursor() as cur:
                yield cur
            return
        conn = self._pool.get()
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            self._release(conn)

    def close(self) -> None:
        self._pool.close()


def open_backend(url: str):
    """``sqlite:///path/to/file.db``, a bare path, or ``postgres://...``."""
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(url)
    if url.startswith("sqlite:///"):  # sqlite:///relative.db or sqlite:////abs/path.db
        return SqliteBackend(url[len("sqlite:///"):])
    if url.startswith("sqlite://"):
        return SqliteBackend(url[len("sqlite://"):])
    return SqliteBackend(url)


# --------------------------------------------------------------------------
# Schema (the standalone server; the Django app creates the same tables
# through its migrations)

_TYPES = {
    "sqlite": dict(pk="integer NOT NULL PRIMARY KEY AUTOINCREMENT", dt="datetime", bool="bool",
                   big="bigint", float="real", true="1", false="0"),
    "postgres": dict(pk="bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
                     dt="timestamp with time zone", bool="boolean", big="bigint",
                     float="double precision", true="true", false="false"),
}

_DDL = """
CREATE TABLE IF NOT EXISTS foxray_project (
  id {pk}, name text NOT NULL, public_key text NOT NULL UNIQUE, created_at {dt} NOT NULL,
  rate_limit integer, sample_after integer, event_retention_days integer,
  events_per_issue integer, issue_retention_days integer,
  alert_new {bool} NOT NULL, alert_regression {bool} NOT NULL,
  alert_min_level text NOT NULL, alert_environments text NOT NULL,
  alert_emails text NOT NULL, alert_webhook text NOT NULL,
  filter_crawlers {bool} NOT NULL, filter_js_noise {bool} NOT NULL,
  filter_health_checks {bool} NOT NULL, health_check_names text NOT NULL);
CREATE TABLE IF NOT EXISTS foxray_issue (
  id {pk}, project_id {big} NOT NULL, fingerprint text NOT NULL, title text NOT NULL,
  culprit text NOT NULL, level text NOT NULL, platform text NOT NULL, status text NOT NULL,
  first_seen {dt} NOT NULL, last_seen {dt} NOT NULL, times_seen {big} NOT NULL,
  users_seen {big} NOT NULL, last_environment text NOT NULL, last_release text NOT NULL,
  last_alerted_at {dt}, resolved_at {dt}, reopened_at {dt}, ignore_until {dt},
  ignore_until_count {big}, UNIQUE (project_id, fingerprint));
CREATE INDEX IF NOT EXISTS foxray_issue_list ON foxray_issue (project_id, status, last_seen);
CREATE INDEX IF NOT EXISTS foxray_issue_last_seen ON foxray_issue (last_seen);
CREATE TABLE IF NOT EXISTS foxray_event (
  id {pk}, project_id {big} NOT NULL, issue_id {big} NOT NULL, event_id text NOT NULL,
  received_at {dt} NOT NULL, occurred_at {dt}, level text NOT NULL, title text NOT NULL,
  environment text NOT NULL, release text NOT NULL, user_key text NOT NULL, data text NOT NULL,
  UNIQUE (project_id, event_id));
CREATE INDEX IF NOT EXISTS foxray_event_issue ON foxray_event (issue_id, id);
CREATE INDEX IF NOT EXISTS foxray_event_received ON foxray_event (received_at);
CREATE TABLE IF NOT EXISTS foxray_issue_day (
  id {pk}, issue_id {big} NOT NULL, project_id {big} NOT NULL, day text NOT NULL, n {big} NOT NULL,
  UNIQUE (issue_id, day));
CREATE INDEX IF NOT EXISTS foxray_issue_day_day ON foxray_issue_day (day);
CREATE TABLE IF NOT EXISTS foxray_issue_user (
  id {pk}, issue_id {big} NOT NULL, user_key text NOT NULL, UNIQUE (issue_id, user_key));
CREATE TABLE IF NOT EXISTS foxray_issue_tag (
  id {pk}, issue_id {big} NOT NULL, key text NOT NULL, value text NOT NULL, n {big} NOT NULL,
  last_seen {dt} NOT NULL, UNIQUE (issue_id, key, value));
CREATE INDEX IF NOT EXISTS foxray_issue_tag_kv ON foxray_issue_tag (key, value);
CREATE TABLE IF NOT EXISTS foxray_monitor (
  id {pk}, project_id {big} NOT NULL, slug text NOT NULL, name text NOT NULL,
  schedule_type text NOT NULL, schedule_value text NOT NULL, schedule_unit text NOT NULL,
  timezone text NOT NULL, checkin_margin integer NOT NULL, max_runtime integer NOT NULL,
  status text NOT NULL, active {bool} NOT NULL, last_checkin_at {dt}, next_due_at {dt},
  last_alerted_at {dt}, created_at {dt}, UNIQUE (project_id, slug));
CREATE TABLE IF NOT EXISTS foxray_checkin (
  id {pk}, project_id {big} NOT NULL, monitor_id {big} NOT NULL, check_in_id text NOT NULL,
  status text NOT NULL, duration {float}, environment text NOT NULL, release text NOT NULL,
  received_at {dt} NOT NULL, updated_at {dt} NOT NULL, UNIQUE (monitor_id, check_in_id));
CREATE INDEX IF NOT EXISTS foxray_checkin_monitor ON foxray_checkin (monitor_id, id);
CREATE INDEX IF NOT EXISTS foxray_checkin_status ON foxray_checkin (status);
CREATE TABLE IF NOT EXISTS foxray_filter (
  id {pk}, project_id {big} NOT NULL, kind text NOT NULL, pattern text NOT NULL,
  is_regex {bool} NOT NULL, environment text NOT NULL, release text NOT NULL,
  enabled {bool} NOT NULL, note text NOT NULL, created_at {dt} NOT NULL);
CREATE TABLE IF NOT EXISTS foxray_counter (
  id {pk}, project_id {big} NOT NULL, day text NOT NULL, reason text NOT NULL, n {big} NOT NULL,
  UNIQUE (project_id, day, reason));
CREATE TABLE IF NOT EXISTS foxray_rate (
  id {pk}, project_id {big} NOT NULL, window_start {big} NOT NULL, n {big} NOT NULL,
  UNIQUE (project_id, window_start));
CREATE TABLE IF NOT EXISTS foxray_alert (
  id {pk}, project_id {big}, kind text NOT NULL, channel text NOT NULL, issue_id {big},
  monitor_id {big}, subject text NOT NULL, body text NOT NULL, payload text NOT NULL,
  status text NOT NULL, attempts integer NOT NULL, next_attempt_at {dt} NOT NULL,
  created_at {dt} NOT NULL, sent_at {dt}, last_error text NOT NULL);
CREATE INDEX IF NOT EXISTS foxray_alert_due ON foxray_alert (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS foxray_state (
  id {pk}, name text NOT NULL UNIQUE, last_run_at {dt}, lease_until {dt}, value text NOT NULL);
CREATE TABLE IF NOT EXISTS foxray_user (
  id {pk}, email text NOT NULL UNIQUE, password_hash text NOT NULL, is_admin {bool} NOT NULL,
  created_at {dt} NOT NULL);
"""

SCHEMA_VERSION = "1"


def ddl_statements(dialect: str) -> list[str]:
    text = _DDL.format(**_TYPES[dialect])
    return [s.strip() for s in text.split(";") if s.strip()]


def init_schema(backend) -> None:
    """Create the tables (idempotent). For a new SQLite file, turn on
    incremental auto-vacuum first, so cleanup can give space back."""
    if backend.dialect == "sqlite":
        with backend.cursor() as c:
            pages = c.execute("PRAGMA page_count").fetchone()[0]
            if pages == 0:
                c.execute("PRAGMA auto_vacuum=INCREMENTAL")
            c.execute("PRAGMA journal_mode=WAL")
    with backend.tx() as c:
        for stmt in ddl_statements(backend.dialect):
            c.execute(stmt, ())
        c.execute("INSERT INTO foxray_state (name, last_run_at, lease_until, value) "
                  "VALUES (%s, NULL, NULL, %s) ON CONFLICT (name) DO NOTHING", ("schema", SCHEMA_VERSION))


# --------------------------------------------------------------------------
# The store


@dataclass
class IssueQuery:
    project_id: int | None = None
    status: str = "unresolved"     # "" or "all" for every status
    level: str = ""
    environment: str = ""
    release: str = ""
    first_seen_from: datetime | None = None
    first_seen_to: datetime | None = None
    last_seen_from: datetime | None = None
    last_seen_to: datetime | None = None
    text: str = ""
    sort: str = "last_seen"        # last_seen, first_seen, count, users
    limit: int = 50
    offset: int = 0


_SORTS = {"last_seen": "last_seen DESC", "first_seen": "first_seen DESC",
          "count": "times_seen DESC", "users": "users_seen DESC"}


class Duplicate(Exception):
    """An event id this project already stored."""


def _ph(n: int) -> str:
    return ", ".join(["%s"] * n)


def _like(text: str) -> str:
    esc = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class SqlStore:
    def __init__(self, backend):
        self.db = backend
        self.dialect = backend.dialect

    # -- plumbing ----------------------------------------------------------

    def _p(self, params) -> tuple:
        return tuple(self.db.adapt(p) for p in params)

    def _all(self, sql: str, params=()) -> list:
        with self.db.cursor() as c:
            c.execute(sql, self._p(params))
            return c.fetchall()

    def _one(self, sql: str, params=()):
        with self.db.cursor() as c:
            c.execute(sql, self._p(params))
            return c.fetchone()

    def _write(self, sql: str, params=()) -> int:
        with self.db.tx() as c:
            c.execute(sql, self._p(params))
            return c.rowcount

    def _write_one(self, sql: str, params=()):
        with self.db.tx() as c:
            c.execute(sql, self._p(params))
            return c.fetchone()

    def atomic(self):
        """A transaction; store calls inside it join it."""
        return self.db.tx()

    @staticmethod
    def _obj(cls, row):
        if row is None:
            return None
        kinds = column_kinds(cls)
        values = {}
        for name, v in zip(column_names(cls), row):
            k = kinds[name]
            if k == "dt":
                v = from_db_datetime(v)
            elif k == "bool":
                v = bool(v)
            values[name] = v
        return cls(**values)

    def _objs(self, cls, rows) -> list:
        return [self._obj(cls, r) for r in rows]

    @staticmethod
    def _cols(cls, prefix: str = "") -> str:
        return ", ".join(prefix + c for c in column_names(cls))

    def _for_update(self) -> str:
        return " FOR UPDATE" if self.dialect == "postgres" else ""

    def _set_clause(self, cls, fields: dict) -> tuple[str, list]:
        allowed = set(column_names(cls)) - {"id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        return ", ".join(f"{k} = %s" for k in fields), list(fields.values())

    # -- projects ----------------------------------------------------------

    def create_project(self, name: str, *, now: datetime | None = None, public_key: str | None = None,
                       **fields) -> Project:
        p = Project(id=0, name=name, public_key=public_key or secrets.token_hex(16),
                    created_at=now or utcnow())
        for k, v in fields.items():
            if not hasattr(p, k) or k == "id":
                raise ValueError(f"unknown project field: {k}")
            setattr(p, k, v)
        cols = column_names(Project)[1:]
        row = self._write_one(
            f"INSERT INTO foxray_project ({', '.join(cols)}) VALUES ({_ph(len(cols))}) RETURNING id",
            [getattr(p, c) for c in cols])
        p.id = row[0]
        return p

    def get_project(self, project_id) -> Project | None:
        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            return None
        return self._obj(Project, self._one(
            f"SELECT {self._cols(Project)} FROM foxray_project WHERE id = %s", (pid,)))

    def get_project_by_name(self, name: str) -> Project | None:
        return self._obj(Project, self._one(
            f"SELECT {self._cols(Project)} FROM foxray_project WHERE name = %s ORDER BY id LIMIT 1", (name,)))

    def list_projects(self) -> list[Project]:
        return self._objs(Project, self._all(f"SELECT {self._cols(Project)} FROM foxray_project ORDER BY id"))

    def update_project(self, project_id: int, **fields) -> None:
        if not fields:
            return
        clause, values = self._set_clause(Project, fields)
        self._write(f"UPDATE foxray_project SET {clause} WHERE id = %s", values + [project_id])

    # -- counters and rate limits -----------------------------------------

    def hit_rate(self, project_id: int, window_start: int, n: int = 1) -> int:
        """Count ``n`` events in this project's window; returns the new total."""
        row = self._write_one(
            "INSERT INTO foxray_rate (project_id, window_start, n) VALUES (%s, %s, %s) "
            "ON CONFLICT (project_id, window_start) DO UPDATE SET n = foxray_rate.n + excluded.n RETURNING n",
            (project_id, window_start, n))
        return int(row[0])

    def bump_counter(self, project_id: int, day: str, reason: str, n: int = 1) -> int:
        row = self._write_one(
            "INSERT INTO foxray_counter (project_id, day, reason, n) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id, day, reason) DO UPDATE SET n = foxray_counter.n + excluded.n RETURNING n",
            (project_id, day, reason[:100], n))
        return int(row[0])

    def counters(self, project_id: int | None = None, since_day: str | None = None) -> list[tuple]:
        where, params = [], []
        if project_id is not None:
            where.append("project_id = %s")
            params.append(project_id)
        if since_day:
            where.append("day >= %s")
            params.append(since_day)
        sql = "SELECT project_id, day, reason, n FROM foxray_counter"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [tuple(r) for r in self._all(sql + " ORDER BY day DESC, reason", params)]

    # -- issues and events -------------------------------------------------

    def insert_issue_if_new(self, project_id: int, fingerprint: str, *, title: str, culprit: str,
                            level: str, platform: str, environment: str, release: str,
                            now: datetime) -> Issue | None:
        cols = column_names(Issue)[1:]
        values = dict(project_id=project_id, fingerprint=fingerprint, title=title, culprit=culprit,
                      level=level, platform=platform, status="unresolved", first_seen=now, last_seen=now,
                      times_seen=1, users_seen=0, last_environment=environment, last_release=release,
                      last_alerted_at=None, resolved_at=None, reopened_at=None, ignore_until=None,
                      ignore_until_count=None)
        row = self._write_one(
            f"INSERT INTO foxray_issue ({', '.join(cols)}) VALUES ({_ph(len(cols))}) "
            f"ON CONFLICT (project_id, fingerprint) DO NOTHING RETURNING {self._cols(Issue)}",
            [values[c] for c in cols])
        return self._obj(Issue, row)

    def lock_issue(self, project_id: int, fingerprint: str) -> Issue | None:
        """Read an issue for update (a row lock on Postgres; SQLite writers are
        already serialised by BEGIN IMMEDIATE). Call inside ``atomic()``."""
        with self.db.tx() as c:
            c.execute(f"SELECT {self._cols(Issue)} FROM foxray_issue WHERE project_id = %s AND fingerprint = %s"
                      + self._for_update(), self._p((project_id, fingerprint)))
            return self._obj(Issue, c.fetchone())

    def record_repeat(self, issue_id: int, *, now: datetime, title: str, culprit: str, level: str,
                      environment: str, release: str, **changes) -> Issue:
        sets = ["times_seen = times_seen + 1", "last_seen = %s", "title = %s", "culprit = %s", "level = %s",
                "last_environment = %s", "last_release = %s"]
        params: list = [now, title, culprit, level, environment, release]
        if changes:
            clause, values = self._set_clause(Issue, changes)
            sets.append(clause)
            params += values
        row = self._write_one(f"UPDATE foxray_issue SET {', '.join(sets)} WHERE id = %s "
                              f"RETURNING {self._cols(Issue)}", params + [issue_id])
        return self._obj(Issue, row)

    def update_issue(self, issue_id: int, **fields) -> None:
        if fields:
            clause, values = self._set_clause(Issue, fields)
            self._write(f"UPDATE foxray_issue SET {clause} WHERE id = %s", values + [issue_id])

    def bump_issue_day(self, issue_id: int, project_id: int, day: str, n: int = 1) -> int:
        row = self._write_one(
            "INSERT INTO foxray_issue_day (issue_id, project_id, day, n) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (issue_id, day) DO UPDATE SET n = foxray_issue_day.n + excluded.n RETURNING n",
            (issue_id, project_id, day, n))
        return int(row[0])

    def insert_event(self, *, project_id: int, issue_id: int, event_id: str, received_at: datetime,
                     occurred_at: datetime | None, level: str, title: str, environment: str, release: str,
                     user_key: str, data: dict | str) -> int | None:
        """Returns the new row id, or None if the event id was already stored."""
        text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"), default=str)
        row = self._write_one(
            "INSERT INTO foxray_event (project_id, issue_id, event_id, received_at, occurred_at, level, "
            "title, environment, release, user_key, data) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id, event_id) DO NOTHING RETURNING id",
            (project_id, issue_id, event_id, received_at, occurred_at, level, title, environment, release,
             user_key, text))
        return None if row is None else int(row[0])

    def add_issue_user(self, issue_id: int, user_key: str) -> bool:
        with self.db.tx():
            row = self._write_one(
                "INSERT INTO foxray_issue_user (issue_id, user_key) VALUES (%s, %s) "
                "ON CONFLICT (issue_id, user_key) DO NOTHING RETURNING id", (issue_id, user_key[:200]))
            if row is None:
                return False
            self._write("UPDATE foxray_issue SET users_seen = users_seen + 1 WHERE id = %s", (issue_id,))
            return True

    def bump_issue_tag(self, issue_id: int, key: str, value: str, now: datetime) -> None:
        self._write(
            "INSERT INTO foxray_issue_tag (issue_id, key, value, n, last_seen) VALUES (%s, %s, %s, 1, %s) "
            "ON CONFLICT (issue_id, key, value) DO UPDATE SET n = foxray_issue_tag.n + 1, "
            "last_seen = excluded.last_seen", (issue_id, key[:32], value[:200], now))

    def issue_tags(self, issue_id: int) -> dict[str, list[tuple[str, int]]]:
        out: dict[str, list[tuple[str, int]]] = {}
        for key, value, n in self._all(
                "SELECT key, value, n FROM foxray_issue_tag WHERE issue_id = %s ORDER BY key, n DESC, value",
                (issue_id,)):
            out.setdefault(key, []).append((value, int(n)))
        return out

    def tag_values(self, key: str, project_id: int | None = None, limit: int = 200) -> list[str]:
        if project_id is None:
            rows = self._all("SELECT DISTINCT value FROM foxray_issue_tag WHERE key = %s ORDER BY value LIMIT %s",
                             (key, limit))
        else:
            rows = self._all(
                "SELECT DISTINCT t.value FROM foxray_issue_tag t JOIN foxray_issue i ON i.id = t.issue_id "
                "WHERE t.key = %s AND i.project_id = %s ORDER BY t.value LIMIT %s", (key, project_id, limit))
        return [r[0] for r in rows]

    def get_issue(self, issue_id) -> Issue | None:
        try:
            iid = int(issue_id)
        except (TypeError, ValueError):
            return None
        return self._obj(Issue, self._one(f"SELECT {self._cols(Issue)} FROM foxray_issue WHERE id = %s", (iid,)))

    def _issue_where(self, q: IssueQuery) -> tuple[str, list]:
        where, params = [], []
        if q.project_id is not None:
            where.append("project_id = %s")
            params.append(q.project_id)
        if q.status and q.status != "all":
            where.append("status = %s")
            params.append(q.status)
        if q.level:
            where.append("level = %s")
            params.append(q.level)
        for key, value in (("environment", q.environment), ("release", q.release)):
            if value:
                where.append("EXISTS (SELECT 1 FROM foxray_issue_tag t WHERE t.issue_id = foxray_issue.id "
                             "AND t.key = %s AND t.value = %s)")
                params += [key, value]
        for col, op, value in (("first_seen", ">=", q.first_seen_from), ("first_seen", "<", q.first_seen_to),
                               ("last_seen", ">=", q.last_seen_from), ("last_seen", "<", q.last_seen_to)):
            if value is not None:
                where.append(f"{col} {op} %s")
                params.append(value)
        if q.text:
            where.append("(lower(title) LIKE %s ESCAPE '\\' OR lower(culprit) LIKE %s ESCAPE '\\')")
            params += [_like(q.text), _like(q.text)]
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list_issues(self, q: IssueQuery | None = None) -> tuple[list[Issue], int]:
        q = q or IssueQuery()
        where, params = self._issue_where(q)
        order = _SORTS.get(q.sort, _SORTS["last_seen"])
        rows = self._all(f"SELECT {self._cols(Issue)} FROM foxray_issue{where} ORDER BY {order}, id DESC "
                         f"LIMIT %s OFFSET %s", params + [max(1, min(q.limit, 500)), max(0, q.offset)])
        total = self._one(f"SELECT count(*) FROM foxray_issue{where}", params)[0]
        return self._objs(Issue, rows), int(total)

    def set_issue_status(self, issue_id: int, status: str, *, now: datetime,
                         ignore_until: datetime | None = None, ignore_more: int | None = None) -> None:
        if status == "resolved":
            self._write("UPDATE foxray_issue SET status = 'resolved', resolved_at = %s, ignore_until = NULL, "
                        "ignore_until_count = NULL WHERE id = %s", (now, issue_id))
        elif status == "ignored":
            if ignore_more:
                self._write("UPDATE foxray_issue SET status = 'ignored', ignore_until = %s, "
                            "ignore_until_count = times_seen + %s WHERE id = %s",
                            (ignore_until, int(ignore_more), issue_id))
            else:
                self._write("UPDATE foxray_issue SET status = 'ignored', ignore_until = %s, "
                            "ignore_until_count = NULL WHERE id = %s", (ignore_until, issue_id))
        elif status == "unresolved":
            self._write("UPDATE foxray_issue SET status = 'unresolved', resolved_at = NULL, ignore_until = NULL, "
                        "ignore_until_count = NULL WHERE id = %s", (issue_id,))
        else:
            raise ValueError(f"unknown status: {status}")

    def issue_days(self, issue_id: int, since_day: str | None = None) -> list[tuple[str, int]]:
        if since_day:
            rows = self._all("SELECT day, n FROM foxray_issue_day WHERE issue_id = %s AND day >= %s ORDER BY day",
                             (issue_id, since_day))
        else:
            rows = self._all("SELECT day, n FROM foxray_issue_day WHERE issue_id = %s ORDER BY day", (issue_id,))
        return [(r[0], int(r[1])) for r in rows]

    def latest_event(self, issue_id: int) -> Event | None:
        return self._obj(Event, self._one(
            f"SELECT {self._cols(Event)} FROM foxray_event WHERE issue_id = %s ORDER BY id DESC LIMIT 1",
            (issue_id,)))

    def get_event(self, row_id) -> Event | None:
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        return self._obj(Event, self._one(f"SELECT {self._cols(Event)} FROM foxray_event WHERE id = %s", (rid,)))

    def find_event(self, project_id: int, event_id: str) -> Event | None:
        return self._obj(Event, self._one(
            f"SELECT {self._cols(Event)} FROM foxray_event WHERE project_id = %s AND event_id = %s",
            (project_id, event_id)))

    def list_events(self, issue_id: int, limit: int = 20) -> list[Event]:
        return self._objs(Event, self._all(
            f"SELECT {self._cols(Event)} FROM foxray_event WHERE issue_id = %s ORDER BY id DESC LIMIT %s",
            (issue_id, limit)))

    def count_events(self, issue_id: int | None = None) -> int:
        if issue_id is None:
            return int(self._one("SELECT count(*) FROM foxray_event")[0])
        return int(self._one("SELECT count(*) FROM foxray_event WHERE issue_id = %s", (issue_id,))[0])

    # -- monitors and check-ins -------------------------------------------

    def get_monitor(self, project_id: int, slug: str, *, for_update: bool = False) -> Monitor | None:
        sql = f"SELECT {self._cols(Monitor)} FROM foxray_monitor WHERE project_id = %s AND slug = %s"
        if for_update:
            with self.db.tx() as c:
                c.execute(sql + self._for_update(), self._p((project_id, slug)))
                return self._obj(Monitor, c.fetchone())
        return self._obj(Monitor, self._one(sql, (project_id, slug)))

    def get_monitor_by_id(self, monitor_id) -> Monitor | None:
        try:
            mid = int(monitor_id)
        except (TypeError, ValueError):
            return None
        return self._obj(Monitor, self._one(f"SELECT {self._cols(Monitor)} FROM foxray_monitor WHERE id = %s", (mid,)))

    def insert_monitor(self, project_id: int, slug: str, now: datetime) -> None:
        cols = column_names(Monitor)[1:]
        values = dict(project_id=project_id, slug=slug, name=slug, schedule_type="", schedule_value="",
                      schedule_unit="", timezone="", checkin_margin=1, max_runtime=30, status="new", active=True,
                      last_checkin_at=None, next_due_at=None, last_alerted_at=None, created_at=now)
        self._write(f"INSERT INTO foxray_monitor ({', '.join(cols)}) VALUES ({_ph(len(cols))}) "
                    f"ON CONFLICT (project_id, slug) DO NOTHING", [values[c] for c in cols])

    def update_monitor(self, monitor_id: int, **fields) -> None:
        if fields:
            clause, values = self._set_clause(Monitor, fields)
            self._write(f"UPDATE foxray_monitor SET {clause} WHERE id = %s", values + [monitor_id])

    def list_monitors(self, project_id: int | None = None) -> list[Monitor]:
        if project_id is None:
            rows = self._all(f"SELECT {self._cols(Monitor)} FROM foxray_monitor ORDER BY project_id, slug")
        else:
            rows = self._all(f"SELECT {self._cols(Monitor)} FROM foxray_monitor WHERE project_id = %s ORDER BY slug",
                             (project_id,))
        return self._objs(Monitor, rows)

    def due_monitors(self, now: datetime, limit: int = 500) -> list[Monitor]:
        return self._objs(Monitor, self._all(
            f"SELECT {self._cols(Monitor)} FROM foxray_monitor WHERE active = %s AND next_due_at IS NOT NULL "
            f"AND next_due_at < %s ORDER BY next_due_at LIMIT %s", (True, now, limit)))

    def claim_missed(self, monitor_id: int, now: datetime, next_due_at: datetime | None) -> bool:
        """Move a late monitor's deadline on; only one caller wins."""
        return self._write("UPDATE foxray_monitor SET next_due_at = %s WHERE id = %s AND next_due_at < %s",
                           (next_due_at, monitor_id, now)) == 1

    def upsert_checkin(self, *, project_id: int, monitor_id: int, check_in_id: str, status: str,
                       duration: float | None, environment: str, release: str, now: datetime) -> CheckIn:
        row = self._write_one(
            "INSERT INTO foxray_checkin (project_id, monitor_id, check_in_id, status, duration, environment, "
            "release, received_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (monitor_id, check_in_id) DO UPDATE SET status = excluded.status, "
            "duration = COALESCE(excluded.duration, foxray_checkin.duration), updated_at = excluded.updated_at "
            f"RETURNING {self._cols(CheckIn)}",
            (project_id, monitor_id, check_in_id, status, duration, environment, release, now, now))
        return self._obj(CheckIn, row)

    def list_checkins(self, monitor_id: int, limit: int = 50) -> list[CheckIn]:
        return self._objs(CheckIn, self._all(
            f"SELECT {self._cols(CheckIn)} FROM foxray_checkin WHERE monitor_id = %s ORDER BY id DESC LIMIT %s",
            (monitor_id, limit)))

    def in_progress_checkins(self, limit: int = 1000) -> list[CheckIn]:
        return self._objs(CheckIn, self._all(
            f"SELECT {self._cols(CheckIn)} FROM foxray_checkin WHERE status = 'in_progress' ORDER BY id LIMIT %s",
            (limit,)))

    def set_checkin_status_if(self, checkin_id: int, from_status: str, to_status: str, now: datetime) -> bool:
        return self._write("UPDATE foxray_checkin SET status = %s, updated_at = %s WHERE id = %s AND status = %s",
                           (to_status, now, checkin_id, from_status)) == 1

    # -- inbound filter rules ---------------------------------------------

    def add_filter(self, project_id: int, kind: str, pattern: str, *, is_regex: bool = False,
                   environment: str = "", release: str = "", enabled: bool = True, note: str = "",
                   now: datetime | None = None) -> FilterRule:
        now = now or utcnow()
        row = self._write_one(
            "INSERT INTO foxray_filter (project_id, kind, pattern, is_regex, environment, release, enabled, note, "
            f"created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {self._cols(FilterRule)}",
            (project_id, kind, pattern, is_regex, environment, release, enabled, note, now))
        return self._obj(FilterRule, row)

    def list_filters(self, project_id: int, *, enabled_only: bool = False) -> list[FilterRule]:
        sql = f"SELECT {self._cols(FilterRule)} FROM foxray_filter WHERE project_id = %s"
        params: list = [project_id]
        if enabled_only:
            sql += " AND enabled = %s"
            params.append(True)
        return self._objs(FilterRule, self._all(sql + " ORDER BY id", params))

    def update_filter(self, filter_id: int, **fields) -> None:
        if fields:
            clause, values = self._set_clause(FilterRule, fields)
            self._write(f"UPDATE foxray_filter SET {clause} WHERE id = %s", values + [filter_id])

    def delete_filter(self, filter_id: int) -> None:
        self._write("DELETE FROM foxray_filter WHERE id = %s", (filter_id,))

    # -- alert outbox ------------------------------------------------------

    def add_alert(self, *, project_id: int | None, kind: str, channel: str, subject: str, body: str,
                  payload: dict, now: datetime, issue_id: int | None = None,
                  monitor_id: int | None = None) -> int:
        row = self._write_one(
            "INSERT INTO foxray_alert (project_id, kind, channel, issue_id, monitor_id, subject, body, payload, "
            "status, attempts, next_attempt_at, created_at, sent_at, last_error) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s, NULL, '') RETURNING id",
            (project_id, kind, channel, issue_id, monitor_id, subject[:300], body,
             json.dumps(payload, default=str), now, now))
        return int(row[0])

    def due_alerts(self, now: datetime, limit: int = 50) -> list[Alert]:
        return self._objs(Alert, self._all(
            f"SELECT {self._cols(Alert)} FROM foxray_alert WHERE status = 'pending' AND next_attempt_at <= %s "
            f"ORDER BY id LIMIT %s", (now, limit)))

    def claim_alert(self, alert_id: int, now: datetime, retry_at: datetime) -> bool:
        """Take an alert to send. The retry time is set up front, so a sender
        that dies leaves it to be retried, and two senders never both win."""
        return self._write("UPDATE foxray_alert SET attempts = attempts + 1, next_attempt_at = %s "
                           "WHERE id = %s AND status = 'pending' AND next_attempt_at <= %s",
                           (retry_at, alert_id, now)) == 1

    def mark_alert_sent(self, alert_id: int, now: datetime, note: str = "") -> None:
        self._write("UPDATE foxray_alert SET status = 'sent', sent_at = %s, last_error = %s WHERE id = %s",
                    (now, note[:500], alert_id))

    def mark_alert_error(self, alert_id: int, error: str, *, failed: bool = False) -> None:
        if failed:
            self._write("UPDATE foxray_alert SET status = 'failed', last_error = %s WHERE id = %s",
                        (error[:500], alert_id))
        else:
            self._write("UPDATE foxray_alert SET last_error = %s WHERE id = %s", (error[:500], alert_id))

    def list_alerts(self, limit: int = 50, project_id: int | None = None) -> list[Alert]:
        if project_id is None:
            rows = self._all(f"SELECT {self._cols(Alert)} FROM foxray_alert ORDER BY id DESC LIMIT %s", (limit,))
        else:
            rows = self._all(f"SELECT {self._cols(Alert)} FROM foxray_alert WHERE project_id = %s "
                             f"ORDER BY id DESC LIMIT %s", (project_id, limit))
        return self._objs(Alert, rows)

    def pending_alert_count(self) -> int:
        return int(self._one("SELECT count(*) FROM foxray_alert WHERE status = 'pending'")[0])

    # -- state and leases --------------------------------------------------

    def states(self) -> dict[str, State]:
        return {r[0]: self._obj(State, r) for r in self._all(
            "SELECT name, last_run_at, lease_until, value FROM foxray_state")}

    def get_state_value(self, name: str, default: str = "") -> str:
        row = self._one("SELECT value FROM foxray_state WHERE name = %s", (name,))
        return default if row is None or row[0] in (None, "") else row[0]

    def set_state_value(self, name: str, value: str) -> None:
        self._write("INSERT INTO foxray_state (name, last_run_at, lease_until, value) VALUES (%s, NULL, NULL, %s) "
                    "ON CONFLICT (name) DO UPDATE SET value = excluded.value", (name, value))

    def try_lease(self, name: str, now: datetime, until: datetime, *, ran_before: datetime | None = None) -> bool:
        """Claim the named job with one conditional UPDATE. It fails if
        someone else holds an unexpired lease or, with ``ran_before``, if the
        job last ran after that time. Works the same on SQLite and Postgres."""
        self._write("INSERT INTO foxray_state (name, last_run_at, lease_until, value) VALUES (%s, NULL, NULL, '') "
                    "ON CONFLICT (name) DO NOTHING", (name,))
        sql = "UPDATE foxray_state SET lease_until = %s WHERE name = %s AND (lease_until IS NULL OR lease_until < %s)"
        params: list = [until, name, now]
        if ran_before is not None:
            sql += " AND (last_run_at IS NULL OR last_run_at <= %s)"
            params.append(ran_before)
        return self._write(sql, params) == 1

    def finish_lease(self, name: str, now: datetime, value: str | None = None) -> None:
        if value is None:
            self._write("UPDATE foxray_state SET last_run_at = %s, lease_until = NULL WHERE name = %s", (now, name))
        else:
            self._write("UPDATE foxray_state SET last_run_at = %s, lease_until = NULL, value = %s WHERE name = %s",
                        (now, value, name))

    # -- cleanup primitives (each removes at most ``limit`` rows) ------------

    def _delete_batch(self, table: str, where: str, params, limit: int) -> int:
        return self._write(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {where} "
                           f"ORDER BY id LIMIT %s)", list(params) + [limit])

    def delete_events_before(self, project_id: int, cutoff: datetime, limit: int) -> int:
        return self._delete_batch("foxray_event", "project_id = %s AND received_at < %s", (project_id, cutoff), limit)

    def issues_seen_since(self, since: datetime | None, limit: int) -> list[tuple[int, datetime]]:
        if since is None:
            rows = self._all("SELECT id, last_seen FROM foxray_issue ORDER BY last_seen, id LIMIT %s", (limit,))
        else:
            rows = self._all("SELECT id, last_seen FROM foxray_issue WHERE last_seen >= %s "
                             "ORDER BY last_seen, id LIMIT %s", (since, limit))
        return [(int(r[0]), from_db_datetime(r[1])) for r in rows]

    def trim_issue_events(self, issue_id: int, keep: int, limit: int) -> int:
        row = self._one("SELECT id FROM foxray_event WHERE issue_id = %s ORDER BY id DESC LIMIT 1 OFFSET %s",
                        (issue_id, keep))
        if row is None:
            return 0
        return self._delete_batch("foxray_event", "issue_id = %s AND id <= %s", (issue_id, row[0]), limit)

    def stale_closed_issues(self, project_id: int, cutoff: datetime, limit: int) -> list[int]:
        rows = self._all("SELECT id FROM foxray_issue WHERE project_id = %s AND status IN ('resolved', 'ignored') "
                         "AND last_seen < %s ORDER BY id LIMIT %s", (project_id, cutoff, limit))
        return [int(r[0]) for r in rows]

    def delete_issues(self, issue_ids: list[int]) -> dict[str, int]:
        out = {"events": 0, "issue_days": 0, "issues": 0}
        if not issue_ids:
            return out
        ph = _ph(len(issue_ids))
        with self.db.tx():
            out["events"] = self._write(f"DELETE FROM foxray_event WHERE issue_id IN ({ph})", issue_ids)
            out["issue_days"] = self._write(f"DELETE FROM foxray_issue_day WHERE issue_id IN ({ph})", issue_ids)
            self._write(f"DELETE FROM foxray_issue_user WHERE issue_id IN ({ph})", issue_ids)
            self._write(f"DELETE FROM foxray_issue_tag WHERE issue_id IN ({ph})", issue_ids)
            out["issues"] = self._write(f"DELETE FROM foxray_issue WHERE id IN ({ph})", issue_ids)
        return out

    def delete_days_before(self, day: str, limit: int) -> int:
        return self._delete_batch("foxray_issue_day", "day < %s", (day,), limit)

    def trim_checkins(self, monitor_id: int, cutoff: datetime, keep: int, limit: int) -> int:
        n = self._delete_batch("foxray_checkin", "monitor_id = %s AND received_at < %s", (monitor_id, cutoff), limit)
        row = self._one("SELECT id FROM foxray_checkin WHERE monitor_id = %s ORDER BY id DESC LIMIT 1 OFFSET %s",
                        (monitor_id, keep))
        if row is not None:
            n += self._delete_batch("foxray_checkin", "monitor_id = %s AND id <= %s", (monitor_id, row[0]), limit)
        return n

    def delete_alerts_before(self, sent_before: datetime, unsent_before: datetime, limit: int) -> int:
        return self._delete_batch(
            "foxray_alert", "(status = 'sent' AND created_at < %s) OR (status <> 'sent' AND created_at < %s)",
            (sent_before, unsent_before), limit)

    def delete_rate_before(self, window_start: int, limit: int) -> int:
        return self._delete_batch("foxray_rate", "window_start < %s", (window_start,), limit)

    def delete_counters_before(self, day: str, limit: int) -> int:
        return self._delete_batch("foxray_counter", "day < %s", (day,), limit)

    def delete_oldest_events(self, limit: int) -> int:
        return self._write("DELETE FROM foxray_event WHERE id IN (SELECT id FROM foxray_event ORDER BY id LIMIT %s)",
                           (limit,))

    def avg_event_bytes(self, sample: int = 500) -> float:
        fn = "octet_length" if self.dialect == "postgres" else "length"
        row = self._one(f"SELECT avg({fn}(data)) FROM (SELECT data FROM foxray_event ORDER BY id LIMIT %s) s",
                        (sample,))
        return float(row[0] or 0)

    # -- size --------------------------------------------------------------

    def db_size(self) -> int:
        """Bytes used: the SQLite file's pages, or Foxray's own Postgres
        tables and indexes (``pg_total_relation_size``)."""
        if self.dialect == "postgres":
            row = self._one(
                "SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                f"WHERE c.relkind = 'r' AND pg_table_is_visible(c.oid) AND c.relname IN ({_ph(len(TABLES))})",
                TABLES)
            return int(row[0])
        page_size = self._one("PRAGMA page_size")[0]
        return int(self._one("PRAGMA page_count")[0]) * int(page_size)

    def free_bytes(self) -> int:
        """SQLite pages on the freelist (reusable, not yet given back)."""
        if self.dialect != "sqlite":
            return 0
        return int(self._one("PRAGMA freelist_count")[0]) * int(self._one("PRAGMA page_size")[0])

    def reclaim(self) -> None:
        """Give freed space back. SQLite: incremental vacuum plus a WAL
        checkpoint. Postgres: nothing (autovacuum; never VACUUM FULL)."""
        if self.dialect != "sqlite":
            return
        with self.db.cursor() as c:
            if int(c.execute("PRAGMA auto_vacuum").fetchone()[0]) == 2:
                c.execute("PRAGMA incremental_vacuum").fetchall()
            if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def table_counts(self) -> dict[str, int]:
        return {t: int(self._one(f"SELECT count(*) FROM {t}")[0]) for t in TABLES}
