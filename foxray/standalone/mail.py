"""Email alerts through smtplib."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from foxray.core.alerts import webhook_sender


def recipients(project, settings) -> list[str]:
    own = [e.strip() for e in (getattr(project, "alert_emails", "") or "").split(",") if e.strip()]
    return own or list(settings.alert_emails)


def smtp_sender(cfg, settings):
    def send(alert, project) -> str:
        to = recipients(project, settings)
        if not to:
            return "no recipients"
        msg = EmailMessage()
        msg["From"] = cfg.smtp_from
        msg["To"] = ", ".join(to)
        msg["Subject"] = alert.subject
        msg.set_content(alert.body)
        if cfg.smtp_ssl:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
        with server:
            if cfg.smtp_starttls and not cfg.smtp_ssl:
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
        return f"emailed {len(to)}"
    return send


def senders(cfg, settings) -> dict:
    out = {"webhook": webhook_sender(settings.alert_webhook)}
    if cfg.smtp_host:
        out["email"] = smtp_sender(cfg, settings)
    return out


def channels_for(cfg, settings):
    def channels(project) -> list[str]:
        out = []
        if cfg.smtp_host and recipients(project, settings):
            out.append("email")
        if (getattr(project, "alert_webhook", "") if project else "") or settings.alert_webhook:
            out.append("webhook")
        return out or ["log"]
    return channels
