# Foxray

A light error tracker that accepts errors from the official Sentry SDKs. It
runs two ways from one codebase:

- **inside a Django app**, as `foxray.django`: errors from the app itself are
  stored in-process (no DSN, no HTTP), and the Django admin is the UI;
- **on its own**, as one small stdlib-only process (`foxray serve`) with a
  SQLite file or Postgres behind it.

**Works with the Sentry SDKs; not affiliated with Sentry.**

Not on PyPI yet: install from a checkout with `pip install /path/to/foxray`
(add `[postgres]` or `[brotli]` for those extras).

**Design rule: one process plus one database; no Redis, Celery, Kafka or
ClickHouse.** Everything other trackers push into side services lives in the
database, kept as simple as possible:

| Elsewhere it's in | In Foxray it's |
| --- | --- |
| Redis rate-limit counters | `foxray_rate`: one atomic upsert per project per minute |
| Redis/memcached dedupe, cooldowns | a unique index on event ids; `last_alerted_at` on the issue |
| Celery alert tasks | `foxray_alert`: an outbox written in the ingest transaction, sent after commit, retried |
| Celery beat (cron checks, cleanup) | `foxray_state` leases; run after ingest when due, or by `foxray tick` |
| Kafka/ClickHouse counts | `foxray_issue_day`, `foxray_counter`: daily counters |

So several processes (uvicorn workers, a cron `tick`) always agree, and
nothing else needs running.

## Add it to a Django app

`pip install sentry-sdk` and Foxray, add `"foxray.django"` to `INSTALLED_APPS`,
add `path("foxray/", include("foxray.django.urls"))` to your URLs, and run
`python manage.py migrate`. That's all: the migration creates a "default"
project, `AppConfig.ready()` starts the Sentry SDK with an in-process transport
(DjangoIntegration, CeleryIntegration if Celery is installed,
`traces_sample_rate=0`, no PII), errors appear in the admin under **Foxray >
Issues**, and alert emails go to `settings.ADMINS` (or, if that is empty, the
superusers) through Django's email backend. Access is Django's own: superusers
see everything; give staff `foxray.view_issue` (and `foxray.change_issue` to
resolve or ignore) through groups. Postgres or SQLite; Django 4.2+; Python 3.10+.

```python
# settings.py
INSTALLED_APPS = [..., "foxray.django"]

# Optional; every key has a default. FOXRAY_<KEY> env vars fill in what isn't set here.
FOXRAY = {
    "BASE_URL": "https://askrobots.com",   # for links in alerts and DSNs
    "ALERT_EMAILS": ["ops@example.com"],   # default: ADMINS, else superusers
    "ALERT_WEBHOOK": "",                   # POSTs alert JSON here too
    "MAX_DB_MB": 500,                      # Foxray's own tables
}

# urls.py
urlpatterns += [path("foxray/", include("foxray.django.urls"))]
```

- If you already call `sentry_sdk.init(...)` before apps load, or `SENTRY_DSN`
  is set, Foxray leaves the SDK alone. To send the app's own errors to Foxray
  explicitly, call `foxray.django.sdk.init(**options)` instead of
  `sentry_sdk.init`. `FOXRAY = {"AUTO_INIT": False}` turns auto-start off. It
  also stays off under `manage.py test` and pytest, so a host app's test errors
  don't land in its test database.
- Storing never raises into the app: if it fails, the error is logged with
  `logging` (logger `foxray.sdk`, which the SDK is told to ignore).
- **Other apps** send over HTTP: copy a DSN from **Foxray > Projects** (or run
  `manage.py foxray_create_project webhive`). It looks like
  `https://<key>@askrobots.com/foxray/<project_id>`. The ingest views are
  CSRF-exempt and don't use sessions or auth; the key is the credential.
- Cleanup runs by itself (see below). `manage.py foxray_tick` (monitors,
  cleanup when due, alerts) and `manage.py foxray_prune` (cleanup now) are there
  if you'd rather run them from cron or your scheduler.

**The trade-off:** an app that reports to itself can't report while it's
down, and an error storm adds load to the same app and database (rate limits
and per-issue sampling bound it). The standalone server on another box avoids
both.

## Run it on its own

```sh
pip install foxray                       # SQLite: standard library only
pip install "foxray[postgres]"           # optional: Postgres via psycopg

export FOXRAY_DATABASE_URL=/var/lib/foxray/foxray.db    # or postgres://user@host/foxray
export FOXRAY_BASE_URL=https://foxray.example.com
foxray create-project shop               # prints the DSN
foxray create-user you@example.com       # a UI login (asks for a password)
foxray serve --host 127.0.0.1 --port 8765
```

`serve` is a threaded HTTP server: SDK ingest at `/api/<id>/envelope/` and
`/api/<id>/store/`, and an HTML UI behind a login (issues, issue pages with the
stack trace, monitors, projects with their filters and limits, alerts,
storage). It runs its own tick every minute. Put it behind a TLS proxy (Caddy,
nginx) and set `FOXRAY_TRUST_PROXY=1`. Other commands: `foxray projects`,
`foxray tick`, `foxray prune`, `foxray storage`.

Configuration comes from environment variables or an INI file
(`--config foxray.ini`, section `[foxray]`, same names in lower case; a `.toml`
file works on Python 3.11+). Environment variables win.

| Variable | Default | |
| --- | --- | --- |
| `FOXRAY_DATABASE_URL` / `DATABASE_URL` | `foxray.db` | SQLite path or `postgres://...` |
| `FOXRAY_HOST`, `FOXRAY_PORT` | `127.0.0.1`, `8765` | |
| `FOXRAY_BASE_URL` | `http://host:port` | public URL, for DSNs and alert links |
| `FOXRAY_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` | off, 587 | email alerts (STARTTLS; `FOXRAY_SMTP_SSL=1` for 465) |
| `FOXRAY_ALERT_EMAILS` | | comma-separated; per project too |
| `FOXRAY_ALERT_WEBHOOK` | | POST alert JSON; per project too |
| `FOXRAY_RATE_LIMIT_PER_MINUTE` | 600 | events per project; 0 = off; per project too |
| `FOXRAY_SAMPLE_AFTER` | 1000 | event bodies per issue per day, then counts only |
| `FOXRAY_MAX_DB_MB` | 500 | hard size cap |
| `FOXRAY_EVENT_RETENTION_DAYS` | 30 | also `EVENTS_PER_ISSUE` (100), `ISSUE_RETENTION_DAYS` (90) |
| `FOXRAY_ALERT_COOLDOWN_SECONDS` | 3600 | |
| `FOXRAY_SECRET_KEY` | generated, kept in the DB | signs UI sessions |

Measured on an M-series Mac, Python 3.10, `bench/measure.py` (1,000 SDK-shaped
4 KB events, gzip, one keep-alive connection, everything stored):

| | idle RSS | 1,000 events |
| --- | --- | --- |
| SQLite | 24.5 MB | 1.9 s (about 520/s) |
| Postgres 14 (local) | 34.9 MB | 5.7 s (about 175/s) |

## Point an SDK at it

```python
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration

sentry_sdk.init(
    dsn="https://<key>@foxray.example.com/<project_id>",
    integrations=[DjangoIntegration(), CeleryIntegration()],
    traces_sample_rate=0,
    send_default_pii=False,
)
```

- Recent sentry-sdk releases (2.73 at the time of writing) deprecate
  `send_default_pii` in favour of `data_collection`. Both default to off, so
  you can leave it out.
- Cron monitors: `@sentry_sdk.crons.monitor(monitor_slug=..., monitor_config=...)`
  or `capture_checkin(...)`. Crontab and interval schedules, timezones,
  `checkin_margin` and `max_runtime` are honoured.
- **Brotli:** the Python SDK compresses with brotli whenever the `Brotli`
  package is installed in the *sending* app (whitenoise setups often have it).
  Foxray decodes brotli only when `Brotli` is installed where Foxray runs
  (`pip install "foxray[brotli]"`). Otherwise it answers 415 and those events
  are lost, so either install it or set
  `_experiments={"transport_compression_algo": "gzip"}` on the senders. gzip
  and deflate always work.

## What it keeps, and what it drops

| Item | |
| --- | --- |
| `event` (and the legacy `/store/` JSON) | stored: exception chain, frames with source context, `in_app`, message, level, platform, release, environment, server_name, transaction, tags, contexts, breadcrumbs (last 50), extra |
| `check_in` | stored; monitors created and scheduled from `monitor_config` |
| `transaction`, `session(s)`, `client_report`, `attachment`, `profile`, `replay*`, `statsd`, anything unknown | accepted (200), dropped, counted per project per day |

- User data is cut to `id` and request data to `url` (query string removed)
  and `method` by default; frame local variables aren't kept unless
  `KEEP_FRAME_VARS` is on. Everything kept is scrubbed before storage:
  passwords, tokens, cookies, Authorization headers, anything under a
  secret-looking key, card numbers (Luhn-checked), private keys, JWTs, and
  Stripe/AWS/GitHub/Slack-style keys.
- **Grouping:** an SDK `fingerprint` wins (`{{ default }}` works). Otherwise
  each exception's type plus its in-app frames as `module:function` (no line
  numbers, so edits elsewhere don't split an issue). Messages group with
  numbers, hex and ids blanked, using the logging template when there is one.
- **Answers:** `200 {"id": ...}`; 401/403 for a missing or wrong key; 413 for
  too large (10 MB sent, 20 MB decompressed, 1 MB per event); 415 for an
  unknown encoding; 429 with `Retry-After` and `X-Sentry-Rate-Limits` over the
  rate limit. CORS is open, so browser SDKs work.

## Filters, alerts, cleanup

- **Inbound filters** (per project, in the DB, edited in the UI or the admin)
  are applied before anything is stored. A dropped event still gets a 200 and
  is counted by reason per day. You can drop by exception type or message
  (substring or regex, 200-character limit, nested repeats refused, optionally
  only for one environment or release), by environment, or by release. Built-in
  switches drop crawlers by user agent and health-check transactions, and
  (opt-in) browser-extension frames and localhost URLs on JavaScript events.
  After N events per issue per day (1,000) only the count is kept. Issues can
  be ignored for good, until N more events, or until a date.
- **Alerts** fire on a new issue or a regression (a resolved issue seen again,
  or an ignored one past its "until"). Repeats never alert, and regressions
  respect a per-issue cooldown. Per project they can be limited to new issues
  and/or regressions, a minimum level, and environments. Monitors alert when they
  go from healthy to missed, failed or timed out. Delivery is email and/or a
  webhook, through the outbox.
- **Cleanup runs by itself.** At most every 10 minutes, the ingest request
  that notices it's due runs one small batch on a short thread after
  answering. A lease row means two never overlap. Defaults:
  - event bodies older than 30 days go;
  - each issue keeps its newest 100 bodies;
  - resolved/ignored issues not seen for 90 days go, with their daily counts;
  - daily counts go after 400 days;
  - check-ins: 30 days or the newest 500 per monitor;
  - sent alerts go after 30 days, failed ones after 7 days of retries;
  - rate windows and drop counters go after 2 days.

  Issue counts and first/last seen survive all of it.
- **Size cap** (`MAX_DB_MB`, 500). Over 90%, the oldest bodies go until the
  store is under 80%. Still over 100%, Foxray counts only: it keeps grouping,
  counting and alerting but stores no new bodies, shows a banner, and sends
  one "storage full" alert. Ingest never fails because the store is full. On
  SQLite the file is created with incremental auto-vacuum, so space goes back
  to the disk after each prune (plus a WAL checkpoint). Postgres keeps freed
  space for reuse: Foxray deletes in batches, lets autovacuum work, never runs
  `VACUUM FULL`, and only prunes again once the tables grow. The Storage page
  shows the size, cap, mode, rows per table and the last cleanup.

## Compared with GlitchTip and self-hosted Sentry

- **Self-hosted Sentry** is the full product: performance tracing, replays,
  profiling, source maps and symbolication, release health, integrations,
  teams. It is also a couple of dozen services (Kafka, ClickHouse, Redis,
  Postgres, Snuba, Relay, ...) and its docs ask for about 16 GB of RAM. Its
  server is under the Functional Source License.
- **GlitchTip** is the closest relative: open source (MIT), Sentry-SDK
  compatible, a Django app with a proper multi-user UI, organizations and
  teams, performance and uptime monitoring. A standard install runs PostgreSQL
  plus a Redis-compatible store and a worker.
- **Foxray** is smaller on purpose: one process and one database (even just a
  SQLite file), about 25 MB of RAM, or no extra process at all inside an
  existing Django app. It does errors, grouping, alerts, cron monitors,
  filters and its own cleanup. It does not do tracing, replays, profiles,
  attachments, source maps (minified JavaScript stack traces stay minified),
  release health, organizations/teams, or integrations beyond email and a
  webhook. If you need those, use GlitchTip or Sentry.

## The name

Foxray: add up the letters of "sentry" as you go (a=1…z=26, wrapping past z)
and you get s x l f x w; in the phonetic alphabet that's Sierra X-ray Lima
Foxtrot X-ray Whiskey, and Foxtrot X-ray gives the name. It looks inside a
crash.

## Development

```sh
python3.10 -m venv .venv && .venv/bin/pip install -e ".[dev,postgres,brotli]"
.venv/bin/pytest                                     # SQLite everywhere
FOXRAY_TEST_PG_URL=postgres://you@localhost/postgres .venv/bin/pytest
                                                     # + Postgres (creates and drops its own databases)
FOXRAY_TEST_DJANGO_PG=postgres://you@localhost/postgres .venv/bin/pytest tests/test_django.py tests/test_store_contract.py
python bench/measure.py [--db postgres://...]        # idle RSS and 1,000-event ingest time
```

Layout: `foxray/core` (stdlib only: protocol, normalising, grouping, scrubbing,
filters, cron, alerts, the SQL store, cleanup), `foxray/standalone` (server,
UI, CLI), `foxray/django` (models, migrations, admin, ingest views, in-process
transport). The storage contract in `tests/test_store_contract.py` runs
against SQLite, Postgres and the Django store alike.

MIT licensed; see `LICENSE` and `NOTICE`.
