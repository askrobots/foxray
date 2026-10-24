"""DSNs, auth, envelopes and bodies."""

import base64
import gzip
import json
import zlib

import pytest

from foxray.core.auth import extract_public_key, key_matches, parse_auth_header
from foxray.core.dsn import DsnError, make_dsn, parse_dsn
from foxray.core.envelope import PayloadError, decode_body, parse_envelope, parse_store_body
from tests.helpers import envelope


def test_parse_dsn_with_path_prefix():
    d = parse_dsn("https://abc123@example.com/foxray/42")
    assert (d.public_key, d.host, d.path, d.project_id) == ("abc123", "example.com", "/foxray", "42")
    assert d.endpoint() == "https://example.com/foxray/api/42/envelope/"
    assert str(d) == "https://abc123@example.com/foxray/42"


def test_parse_dsn_with_port_and_secret():
    d = parse_dsn("http://pub:sec@127.0.0.1:9000/7")
    assert (d.secret_key, d.port, d.path, d.project_id) == ("sec", 9000, "", "7")
    assert d.endpoint("store") == "http://127.0.0.1:9000/api/7/store/"


@pytest.mark.parametrize("bad", ["", "ftp://k@h/1", "https://h/1", "https://k@h/"])
def test_bad_dsns(bad):
    with pytest.raises(DsnError):
        parse_dsn(bad)


def test_make_dsn_round_trip():
    assert make_dsn("https://askrobots.com/foxray/", "k", 3) == "https://k@askrobots.com/foxray/3"
    assert make_dsn("http://127.0.0.1:8765", "k", 1) == "http://k@127.0.0.1:8765/1"
    assert parse_dsn(make_dsn("https://h.example/x/y", "k", 9)).endpoint() == "https://h.example/x/y/api/9/envelope/"


def test_auth_header_parsing():
    h = parse_auth_header("Sentry sentry_key=abc, sentry_version=7, sentry_client=sentry.python/2.0")
    assert h == {"sentry_key": "abc", "sentry_version": "7", "sentry_client": "sentry.python/2.0"}


def test_key_sources():
    assert extract_public_key({"x-sentry-auth": "Sentry sentry_key=a"}, {}) == "a"
    assert extract_public_key({"authorization": "Sentry sentry_key=b, sentry_version=7"}, {}) == "b"
    assert extract_public_key({}, {"sentry_key": "c", "sentry_version": "7"}) == "c"
    assert extract_public_key({}, {}, {"dsn": "https://d@h/1"}) == "d"
    assert extract_public_key({"authorization": "Bearer x"}, {}) is None
    assert key_matches("abc", "abc") and not key_matches("abd", "abc") and not key_matches(None, "abc")


def test_envelope_with_lengths_multi_item():
    body = envelope(({"type": "event"}, {"message": "a\nb"}), ({"type": "attachment"}, b"\x00\x01\n\x02"),
                    ({"type": "check_in"}, {"monitor_slug": "m"}), header={"event_id": "e" * 32})
    header, items = parse_envelope(body)
    assert header["event_id"] == "e" * 32
    assert [i.type for i in items] == ["event", "attachment", "check_in"]
    assert items[0].json()["message"] == "a\nb"
    assert items[1].payload == b"\x00\x01\n\x02"


def test_envelope_without_lengths_and_without_trailing_newline():
    body = b'{}\n{"type":"event"}\n{"message":"x"}\n{"type":"session"}\n{"sid":"1"}'
    _, items = parse_envelope(body)
    assert [i.type for i in items] == ["event", "session"]
    assert items[1].json() == {"sid": "1"}


def test_envelope_empty_payload_and_blank_header():
    _, items = parse_envelope(b'\n{"type":"client_report","length":0}\n\n')
    assert items[0].type == "client_report" and items[0].payload == b""


@pytest.mark.parametrize("body", [b"not json\n", b'{}\n{"type":"event","length":99}\n{}', b'{}\n[1]\n{}',
                                  b'{}\n{"type":"event","length":-1}\n'])
def test_bad_envelopes(body):
    with pytest.raises(PayloadError) as e:
        parse_envelope(body)
    assert e.value.status == 400


def test_too_many_items():
    body = envelope(*[({"type": "session"}, {}) for _ in range(5)])
    with pytest.raises(PayloadError) as e:
        parse_envelope(body, max_items=3)
    assert e.value.status == 413


def test_decode_gzip_deflate_identity():
    raw = b'{"hello": "world"}' * 10
    assert decode_body(gzip.compress(raw), "gzip", 10_000) == raw
    assert decode_body(zlib.compress(raw), "deflate", 10_000) == raw
    co = zlib.compressobj(wbits=-15)
    assert decode_body(co.compress(raw) + co.flush(), "deflate", 10_000) == raw
    assert decode_body(raw, None, 10_000) == raw
    assert decode_body(raw, "identity", 10_000) == raw


def test_unknown_encoding_is_415():
    with pytest.raises(PayloadError) as e:
        decode_body(b"x", "zstd", 100)
    assert e.value.status == 415


def test_decompression_bomb_is_413():
    bomb = gzip.compress(b"0" * 5_000_000)
    with pytest.raises(PayloadError) as e:
        decode_body(bomb, "gzip", 1_000_000)
    assert e.value.status == 413


def test_corrupt_gzip_is_400():
    with pytest.raises(PayloadError) as e:
        decode_body(b"\x1f\x8bnope", "gzip", 100)
    assert e.value.status == 400


def test_store_body_plain_and_old_base64_zlib():
    ev = {"message": "hi"}
    assert parse_store_body(json.dumps(ev).encode(), 1000) == ev
    old = base64.b64encode(zlib.compress(json.dumps(ev).encode()))
    assert parse_store_body(old, 1000) == ev
    with pytest.raises(PayloadError):
        parse_store_body(b"[]", 1000)
