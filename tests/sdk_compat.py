"""The same checks on sentry-sdk 1.x (askrobots.com pins 1.9.10) and 2.x."""

import sentry_sdk


def sdk_client():
    get = getattr(sentry_sdk, "get_client", None)
    return get() if get else sentry_sdk.Hub.current.client


def sdk_active() -> bool:
    c = sdk_client()
    if c is None:
        return False
    return c.is_active() if hasattr(c, "is_active") else c.transport is not None
