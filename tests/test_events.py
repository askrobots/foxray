"""Normalising, fingerprints and scrubbing."""

from foxray.core.fingerprint import fingerprint, fingerprint_components, normalize_message
from foxray.core.normalize import event_culprit, event_title, normalize_event
from foxray.core.scrub import FILTERED, scrub, scrub_event, scrub_string
from foxray.core.settings import Settings
from tests.helpers import make_event

S = Settings()


def norm(raw, **kw):
    return normalize_event(raw, Settings(**kw) if kw else S)[0]


# -- normalising ---------------------------------------------------------

def test_keeps_only_user_id_and_request_url_method_by_default():
    ev, meta = normalize_event({
        "user": {"id": 5, "email": "a@b.c", "ip_address": "1.2.3.4"},
        "request": {"url": "https://x.test/p?token=s3cret#frag", "method": "post",
                    "headers": {"User-Agent": "Googlebot/2.1", "Cookie": "a=b"}, "data": {"pw": "x"},
                    "query_string": "token=s3cret"},
    }, S)
    assert ev["user"] == {"id": "5"}
    assert ev["request"] == {"url": "https://x.test/p", "method": "POST"}
    assert meta["user_agent"] == "Googlebot/2.1"


def test_more_user_fields_when_configured():
    ev = norm({"user": {"id": 1, "email": "a@b.c"}}, user_fields=("id", "email"))
    assert ev["user"] == {"id": "1", "email": "a@b.c"}


def test_levels_platform_release_environment():
    ev = norm({"level": "warn", "platform": "javascript", "release": "1.2", "environment": "production",
               "server_name": "web1", "transaction": "/api/x", "dist": "7"})
    assert (ev["level"], ev["platform"], ev["release"], ev["environment"]) == ("warning", "javascript", "1.2", "production")
    assert ev["server_name"] == "web1" and ev["transaction"] == "/api/x"
    assert norm({"level": "nonsense"})["level"] == "error"
    assert norm({"level": "critical"})["level"] == "fatal"


def test_message_forms():
    assert norm({"message": "plain"})["message"] == "plain"
    ev = norm({"logentry": {"message": "user %s failed", "params": ["bob"]}})
    assert ev["message"] == "user bob failed" and ev["message_template"] == "user %s failed"
    assert norm({"message": {"formatted": "f"}})["message"] == "f"


def test_exception_chain_frames_and_in_app_guess():
    raw = {"exception": {"values": [
        {"type": "KeyError", "value": "'a'", "stacktrace": {"frames": [{"filename": "a.py", "function": "f"}]}},
        {"type": "ValueError", "value": "outer", "mechanism": {"type": "django", "handled": False},
         "stacktrace": {"frames": [
             {"abs_path": "/venv/lib/python3.10/site-packages/django/x.py", "function": "g", "lineno": 1},
             {"abs_path": "/srv/app/views.py", "module": "app.views", "function": "h", "lineno": 2,
              "vars": {"password": "x"}}]}},
    ]}}
    ev = norm(raw)
    values = ev["exception"]["values"]
    assert [v["type"] for v in values] == ["KeyError", "ValueError"]
    frames = values[1]["stacktrace"]["frames"]
    assert [f["in_app"] for f in frames] == [False, True]
    assert "vars" not in frames[1]  # off by default
    assert values[1]["mechanism"] == {"type": "django", "handled": False}
    assert event_title(ev) == "ValueError: outer"
    assert event_culprit(ev) == "app.views in h"
    assert "vars" in norm(raw, keep_frame_vars=True)["exception"]["values"][1]["stacktrace"]["frames"][1]


def test_breadcrumbs_are_capped_and_tags_normalised():
    crumbs = [{"message": f"c{i}", "category": "x", "timestamp": 1700000000 + i} for i in range(80)]
    ev = norm({"breadcrumbs": {"values": crumbs}, "tags": [["a", 1], ["b", "two"]]})
    assert len(ev["breadcrumbs"]) == 50 and ev["breadcrumbs"][-1]["message"] == "c79"
    assert ev["tags"] == {"a": "1", "b": "two"}
    assert len(norm({"breadcrumbs": crumbs}, breadcrumbs_cap=5)["breadcrumbs"]) == 5


def test_event_id_from_envelope_header_or_new():
    ev, _ = normalize_event({}, S, {"event_id": "AB" * 16})
    assert ev["event_id"] == "ab" * 16
    assert len(normalize_event({"event_id": "not-hex"}, S)[0]["event_id"]) == 32


def test_contexts_kept_and_bounded():
    ev = norm({"contexts": {"os": {"name": "Linux"}, "runtime": {"name": "CPython", "version": "3.10"},
                            "junk": "not a dict"}})
    assert ev["contexts"] == {"os": {"name": "Linux"}, "runtime": {"name": "CPython", "version": "3.10"}}


# -- fingerprints --------------------------------------------------------

def fp(raw):
    return fingerprint(norm(raw))


def test_same_place_same_issue_despite_line_numbers_and_values():
    a = make_event("ValueError", "bad id 123", lineno=10)
    b = make_event("ValueError", "bad id 456", lineno=99)
    assert fp(a) == fp(b)


def test_different_function_or_type_differs():
    assert fp(make_event(function="index")) != fp(make_event(function="detail"))
    assert fp(make_event("ValueError")) != fp(make_event("TypeError"))


def test_library_frames_ignored_when_app_frames_exist():
    a = make_event()
    b = make_event()
    b["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] = "other_lib_fn"
    assert fp(a) == fp(b)


def test_sdk_fingerprint_wins_and_default_placeholder():
    a = make_event(fingerprint=["db-down"])
    b = make_event("TypeError", function="x", fingerprint=["db-down"])
    assert fp(a) == fp(b)
    c = make_event(fingerprint=["{{ default }}", "tenant-1"])
    d = make_event(fingerprint=["{{default}}", "tenant-2"])
    assert fp(c) != fp(d)
    assert fingerprint_components(norm(c))[0] == "type:ValueError"


def test_message_fallback_strips_numbers_and_hex():
    assert normalize_message("Order 1234 failed at 0xdeadbeef id 3f2a9c1e7b") == \
        "Order <n> failed at <hex> id <hex>"
    assert normalize_message("user 550e8400-e29b-41d4-a716-446655440000 gone") == "user <id> gone"
    assert fp({"message": "Timeout after 30s on job 991"}) == fp({"message": "Timeout after 45s on job 17"})
    assert fp({"message": "Timeout"}) != fp({"message": "Refused"})
    # the template groups logging calls regardless of their arguments
    assert fp({"logentry": {"message": "user %s failed", "params": ["bob"]}}) == \
        fp({"logentry": {"message": "user %s failed", "params": ["alice"]}})


def test_js_bundle_hash_does_not_split_issues():
    def js(name):
        return {"platform": "javascript", "exception": {"values": [{"type": "TypeError", "value": "x",
                "stacktrace": {"frames": [{"filename": name, "function": "go", "in_app": True}]}}]}}
    assert fp(js("https://x.test/static/main.3f2a1b9c.js")) == fp(js("https://x.test/static/main.77ab12ff.js"))


# -- scrubbing -----------------------------------------------------------

def test_sensitive_keys():
    out = scrub({"password": "hunter2", "Authorization": "Bearer abc", "api_key": "k", "sessionid": "s",
                 "csrftoken": "t", "nested": {"db_password": "p", "safe": "ok"}, "list": [{"secret": 1}],
                 "author": "dan", "empty_password": ""})
    assert out["password"] == out["Authorization"] == out["api_key"] == FILTERED
    assert out["sessionid"] == out["csrftoken"] == FILTERED
    assert out["nested"] == {"db_password": FILTERED, "safe": "ok"}
    assert out["list"] == [{"secret": FILTERED}]
    assert out["author"] == "dan" and out["empty_password"] == ""


def test_card_numbers_only_when_luhn_valid():
    assert scrub_string("card 4111 1111 1111 1111 ok") == f"card {FILTERED} ok"
    assert scrub_string("4111-1111-1111-1111") == FILTERED
    assert scrub_string("order 1234567890123456") == "order 1234567890123456"  # fails Luhn
    assert scrub_string("ts 1700000000123") == "ts 1700000000123"


def test_secret_looking_strings():
    s = scrub_string
    assert FILTERED in s("key sk_live_" + "a1" * 12)
    assert FILTERED in s("AKIAABCDEFGHIJKLMNOP")
    assert FILTERED in s("ghp_" + "x" * 36)
    assert s("Authorization: Bearer abcdefghijklmnop") == f"Authorization: Bearer {FILTERED}"
    assert s("postgres://bob:pa55@db:5432/x") == f"postgres://bob:{FILTERED}@db:5432/x"
    assert s("GET /cb?token=abc123&x=1") == f"GET /cb?token={FILTERED}&x=1"
    assert FILTERED in s("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----")
    assert FILTERED in s("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    assert s("nothing to see here 42") == "nothing to see here 42"


def test_scrub_event_covers_exception_values_breadcrumbs_and_extra():
    ev = norm({"exception": {"values": [{"type": "E", "value": "login failed password=hunter2"}]},
               "breadcrumbs": [{"message": "card 4111111111111111", "data": {"token": "t"}}],
               "extra": {"cookie": "c", "note": "fine"}})
    out = scrub_event(ev)
    assert "hunter2" not in str(out) and "4111111111111111" not in str(out)
    assert out["breadcrumbs"][0]["data"]["token"] == FILTERED
    assert out["extra"] == {"cookie": FILTERED, "note": "fine"}
