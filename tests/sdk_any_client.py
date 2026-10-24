"""A client that works on sentry-sdk 1.x and 2.x: errors and messages only (no crons, no new_scope).

    python tests/sdk_any_client.py <dsn>   prints the reported ids as JSON on the last line"""

import json
import sys

import sentry_sdk


def checkout(n):
    return 10 / n


def main(dsn: str) -> None:
    sentry_sdk.init(dsn=dsn, traces_sample_rate=0, release="shop@1.0.0", environment="test")
    ids = {"exc": []}
    for _ in range(2):
        try:
            checkout(0)
        except ZeroDivisionError as e:
            ids["exc"].append(sentry_sdk.capture_exception(e))
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("tenant", "acme")
        ids["msg"] = sentry_sdk.capture_message("Payment provider slow: 1234 ms", level="warning")
    sentry_sdk.flush(timeout=10)
    print(json.dumps(ids))


if __name__ == "__main__":
    main(sys.argv[1])
