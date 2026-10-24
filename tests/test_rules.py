"""Inbound filters and cron schedules."""

from datetime import datetime, timezone

import pytest

from foxray.core.cron import ScheduleError, add_interval, crontab, next_expected, parse_schedule
from foxray.core.filters import PatternError, filter_reason, validate_pattern
from foxray.core.models import FilterRule, Project
from foxray.core.normalize import normalize_event
from foxray.core.settings import Settings
from tests.helpers import make_event

S = Settings()
UTC = timezone.utc


def project(**kw):
    return Project(id=1, name="p", public_key="k", created_at=datetime.now(UTC), **kw)


def rule(kind, pattern, rid=1, **kw):
    return FilterRule(id=rid, project_id=1, kind=kind, pattern=pattern, is_regex=kw.pop("is_regex", False),
                      environment=kw.pop("environment", ""), release=kw.pop("release", ""),
                      enabled=kw.pop("enabled", True), note="", created_at=datetime.now(UTC))


def reason(raw, p=None, rules=()):
    ev, meta = normalize_event(raw, S)
    return filter_reason(p or project(), S, list(rules), ev, meta)


def test_crawler_filter_by_user_agent():
    bot = make_event(request={"url": "https://x/", "headers": {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}})
    human = make_event(request={"url": "https://x/", "headers": {"User-Agent": "Mozilla/5.0 Firefox/120"}})
    assert reason(bot) == "filter:crawler"
    assert reason(human) is None
    assert reason(bot, project(filter_crawlers=False)) is None


def test_health_check_filter():
    assert reason({"message": "x", "transaction": "/healthz"}) == "filter:health-check"
    assert reason({"message": "x", "request": {"url": "https://x.test/ping/"}}) == "filter:health-check"
    assert reason({"message": "x", "transaction": "/orders"}) is None
    assert reason({"message": "x", "transaction": "/up"}, project(health_check_names="/up")) == "filter:health-check"


def test_js_noise_is_opt_in():
    ext = {"platform": "javascript", "exception": {"values": [{"type": "TypeError", "value": "x", "stacktrace":
           {"frames": [{"abs_path": "chrome-extension://abc/content.js", "function": "f"}]}}]}}
    local = {"platform": "javascript", "message": "x", "request": {"url": "http://localhost:3000/app"}}
    assert reason(ext) is None and reason(local) is None
    on = project(filter_js_noise=True)
    assert reason(ext, on) == "filter:browser-extension"
    assert reason(local, on) == "filter:localhost"
    assert reason({"platform": "python", "message": "x", "request": {"url": "http://localhost/"}}, on) is None


def test_rules_by_type_message_environment_release():
    ev = make_event("django.http.Http404", "No Page matches", environment="development", release="1.0")
    assert reason(ev, rules=[rule("exception", "Http404")]) == "filter:exception#1"
    assert reason(ev, rules=[rule("message", r"no page \w+", is_regex=True, rid=2)]) == "filter:message#2"
    assert reason(ev, rules=[rule("environment", "development", rid=3)]) == "filter:environment#3"
    assert reason(ev, rules=[rule("release", "1.0", rid=4)]) == "filter:release#4"
    assert reason(ev, rules=[rule("release", "2.0")]) is None
    # limited to an environment
    assert reason(ev, rules=[rule("exception", "Http404", environment="production")]) is None
    assert reason(ev, rules=[rule("exception", "Http404", environment="development", release="1.0")]) is not None
    assert reason(ev, rules=[rule("exception", "Http404", enabled=False)]) is None


def test_pattern_safety():
    with pytest.raises(PatternError):
        validate_pattern("x" * 201, False)
    with pytest.raises(PatternError):
        validate_pattern("(a+)+$", True)
    with pytest.raises(PatternError):
        validate_pattern("([a-z]*)*", True)
    with pytest.raises(PatternError):
        validate_pattern("(unclosed", True)
    assert validate_pattern(r"Timeout \d+", True)
    # a bad stored rule never matches (and never raises)
    assert reason(make_event(), rules=[rule("message", "(a+)+", is_regex=True)]) is None


# -- cron ----------------------------------------------------------------

def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_crontab_next_after():
    assert crontab("0 * * * *").next_after(dt("2026-10-01 10:00:05")) == dt("2026-10-01 11:00:00")
    assert crontab("*/15 * * * *").next_after(dt("2026-10-01 10:14:59")) == dt("2026-10-01 10:15:00")
    assert crontab("30 2 * * 1-5").next_after(dt("2026-10-02 03:00:00")) == dt("2026-10-05 02:30:00")  # Fri -> Mon
    assert crontab("0 0 1 jan *").next_after(dt("2026-10-01 00:00:00")) == dt("2027-01-01 00:00:00")
    assert crontab("@daily").next_after(dt("2026-10-01 00:00:00")) == dt("2026-10-02 00:00:00")
    assert crontab("0 12 * * sun").next_after(dt("2026-10-01 00:00:00")) == dt("2026-10-04 12:00:00")
    # day-of-month OR day-of-week when both are restricted
    assert crontab("0 0 15 * mon").next_after(dt("2026-10-01 00:00:00")) == dt("2026-10-05 00:00:00")


def test_crontab_timezone():
    # 09:00 in Chicago (CDT, UTC-5) on 2026-10-01
    assert next_expected("crontab", "0 9 * * *", "", "America/Chicago", dt("2026-10-01 12:00:00")) == \
        dt("2026-10-01 14:00:00")


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* * * * mon-xyz", "*/0 * * * *"])
def test_bad_crontabs(bad):
    with pytest.raises(ScheduleError):
        crontab(bad)


def test_intervals_and_schedule_parsing():
    assert add_interval(dt("2026-01-31 00:00:00"), 1, "month") == dt("2026-02-28 00:00:00")
    assert add_interval(dt("2026-10-01 00:00:00"), 2, "hour") == dt("2026-10-01 02:00:00")
    assert parse_schedule({"type": "interval", "value": 10, "unit": "minutes"}) == ("interval", "10", "minute")
    assert parse_schedule({"type": "crontab", "value": "0 * * * *"}) == ("crontab", "0 * * * *", "")
    with pytest.raises(ScheduleError):
        parse_schedule({"type": "interval", "value": 0, "unit": "minute"})
