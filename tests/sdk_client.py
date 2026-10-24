"""A real app using the official Sentry SDK, pointed at Foxray.

Run as a separate process (so the SDK's global state is clean):
    python tests/sdk_client.py <dsn>
Prints a JSON object of the ids the SDK reported, on the last line."""

import json
import logging
import sys
import time

import sentry_sdk
from sentry_sdk.crons import capture_checkin, monitor


def checkout(n):
    return 10 / n


def main(dsn: str) -> None:
    import os
    experiments = {}
    if os.environ.get("SDK_COMPRESSION"):  # "gzip" or "br" (the SDK picks br itself when brotli is installed)
        experiments["transport_compression_algo"] = os.environ["SDK_COMPRESSION"]
    sentry_sdk.init(dsn=dsn, traces_sample_rate=0, send_default_pii=False, release="shop@1.2.3",
                    environment="test", server_name="client-1", _experiments=experiments)
    ids: dict = {"exc": []}
    for _ in range(2):
        try:
            checkout(0)
        except ZeroDivisionError:
            ids["exc"].append(sentry_sdk.capture_exception())
    ids["msg"] = sentry_sdk.capture_message("Payment provider slow: 1234 ms", level="warning")
    with sentry_sdk.new_scope() as scope:
        scope.set_user({"id": "42", "email": "secret@example.com"})
        scope.set_tag("tenant", "acme")
        scope.set_extra("api_token", "tok_live_abcdef")
        try:
            {}["missing"]
        except KeyError:
            ids["keyerror"] = sentry_sdk.capture_exception()
    log = logging.getLogger("shop.db")
    log.error("db timeout on shard %s", 3)
    log.error("db timeout on shard %s", 7)

    @monitor(monitor_slug="nightly-report", monitor_config={
        "schedule": {"type": "crontab", "value": "0 3 * * *"}, "checkin_margin": 10, "max_runtime": 30,
        "timezone": "America/Chicago"})
    def nightly():
        time.sleep(0.05)
    nightly()

    @monitor(monitor_slug="broken-job")
    def broken():
        raise RuntimeError("job failed")
    try:
        broken()
    except RuntimeError:
        ids["job_error"] = sentry_sdk.capture_exception()

    ids["checkin"] = capture_checkin(monitor_slug="hourly-sync", status="ok", duration=1.5, monitor_config={
        "schedule": {"type": "interval", "value": 1, "unit": "hour"}})
    sentry_sdk.flush(timeout=10)
    print(json.dumps(ids))


if __name__ == "__main__":
    main(sys.argv[1])
