"""One set of Foxray objects per process, built from settings.FOXRAY on
first use. Holds no state of its own: everything lives in the database."""

from __future__ import annotations

import logging
import threading

from django.conf import settings as django_settings
from django.db import connections
from django.urls import NoReverseMatch, reverse

from foxray.core.alerts import Links, Outbox, webhook_sender
from foxray.core.dsn import make_dsn
from foxray.core.ingest import Ingestor
from foxray.core.maintenance import Housekeeper, Maintenance

from .conf import conf, core_settings
from .sdk import quiet
from .store import get_store

log = logging.getLogger("foxray.django")


class AdminLinks(Links):
    def _url(self, name, *args) -> str:
        try:
            return self.base_url + reverse(name, args=args)
        except NoReverseMatch:
            return self.base_url + "/admin/"

    def issue(self, issue_id):
        return self._url("admin:foxray_issue_change", issue_id)

    def monitor(self, monitor_id):
        return self._url("admin:foxray_monitor_change", monitor_id)

    def storage(self):
        return self._url("admin:foxray_storage_changelist")


def recipients(project) -> list[str]:
    own = [e.strip() for e in (getattr(project, "alert_emails", "") or "").split(",") if e.strip()]
    if own:
        return own
    configured = conf().get("ALERT_EMAILS")
    if configured:
        return [configured] if isinstance(configured, str) else list(configured)
    # ADMINS is (name, address) pairs before Django 6.0, plain addresses from 6.0
    admins = [a if isinstance(a, str) else a[1] for a in getattr(django_settings, "ADMINS", [])]
    if admins:
        return admins
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return list(User.objects.filter(is_superuser=True, is_active=True).exclude(email="")
                .values_list("email", flat=True))


def sender_email() -> str:
    """FOXRAY['FROM_EMAIL'], else SERVER_EMAIL if the site set one (Django's own
    default, root@localhost, is no mailbox), else DEFAULT_FROM_EMAIL."""
    configured = conf().get("FROM_EMAIL")
    if configured:
        return configured
    server = getattr(django_settings, "SERVER_EMAIL", "")
    if server and server != "root@localhost":
        return server
    return django_settings.DEFAULT_FROM_EMAIL


def django_email(alert, project) -> str:
    from django.core.mail import send_mail
    to = recipients(project)
    if not to:
        return "no recipients: set ADMINS, FOXRAY['ALERT_EMAILS'], or give a superuser an email"
    with quiet():
        send_mail(alert.subject, alert.body, sender_email(), to, fail_silently=False)
    return f"emailed {len(to)}"


def run_after(fn) -> None:
    """Run after the response, on a short thread with its own DB connection."""
    if conf().get("SYNC_AFTER"):
        fn()
        return

    def target():
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("background work failed")
        finally:
            connections.close_all()
    threading.Thread(target=target, name="foxray-after", daemon=True).start()


class Runtime:
    def __init__(self):
        self.settings = s = core_settings()
        self.store = get_store()
        self.links = AdminLinks(s.base_url)
        webhook = s.alert_webhook

        def channels(project):
            out = ["email"]
            if (getattr(project, "alert_webhook", "") if project else "") or webhook:
                out.append("webhook")
            return out
        self.channels_for = channels
        post = webhook_sender(webhook)

        def quiet_webhook(*args, **kwargs):
            with quiet():
                return post(*args, **kwargs)
        self.outbox = Outbox(self.store, s, {"email": django_email, "webhook": quiet_webhook})
        self.maintenance = Maintenance(self.store, s, links=self.links, global_channels=channels(None))
        self.housekeeper = Housekeeper(self.store, s, maintenance=self.maintenance, outbox=self.outbox,
                                       links=self.links, channels_for=channels)
        self.ingestor = Ingestor(self.store, s, links=self.links, channels_for=channels,
                                 housekeeper=self.housekeeper)

    def default_project(self):
        name = str(conf().get("PROJECT") or "default")
        project = self.store.get_project_by_name(name)
        if project is None and name.isdigit():
            project = self.store.get_project(int(name))
        if project is None:
            project = self.store.create_project(name)
        return project

    def ingest_prefix(self) -> str:
        """Where the ingest URLs are mounted, e.g. "/foxray"."""
        try:
            url = reverse("foxray:envelope", kwargs={"project_id": 0})
        except NoReverseMatch:
            return "/foxray"
        return url[: url.index("/api/0/envelope/")]

    def dsn(self, project, request=None) -> str:
        base = self.settings.base_url.rstrip("/")
        if not base and request is not None:
            base = f"{request.scheme}://{request.get_host()}"
        return make_dsn((base or "https://your-site") + self.ingest_prefix(), project.public_key, project.id)


_runtime: Runtime | None = None
_lock = threading.Lock()


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        with _lock:
            if _runtime is None:
                _runtime = Runtime()
    return _runtime


def reset_on_setting_change(setting=None, **kwargs) -> None:
    global _runtime
    if setting in ("FOXRAY", "ADMINS", "DATABASES"):
        _runtime = None
