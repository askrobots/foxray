"""``settings.FOXRAY``: a dict. Every key is optional.

AUTO_INIT      True: start the Sentry SDK in-process if nothing else did
SDK_OPTIONS    extra ``sentry_sdk.init`` options for AUTO_INIT
PROJECT        the project the app's own errors go to ("default")
BASE_URL       public URL of the site, for DSNs and alert links
ALERT_EMAILS   list; default settings.ADMINS, else the superusers
ALERT_WEBHOOK  URL to POST alerts to as JSON
MAX_DB_MB, EVENT_RETENTION_DAYS, RATE_LIMIT_PER_MINUTE, ... any field of
foxray.core.settings.Settings, upper-cased.
"""

from django.conf import settings as django_settings

from foxray.core.settings import Settings


def conf() -> dict:
    value = getattr(django_settings, "FOXRAY", None) or {}
    return dict(value)


def core_settings() -> Settings:
    data = conf()
    data.pop("ALERT_EMAILS", None)  # resolved at send time (ADMINS, superusers)
    return Settings.from_mapping(data, upper=True)
