"""Settings shared by both ways of running Foxray. Each mode fills this from
its own source (env/INI for standalone, settings.FOXRAY for Django)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

DEFAULT_HEALTH_CHECKS = (
    "/health", "/healthz", "/healthcheck", "/health-check", "/_health",
    "/ping", "/ready", "/readyz", "/live", "/livez", "/status",
)


@dataclass
class Settings:
    # Where the UI lives, for links in alerts and DSNs (e.g. https://foxray.example.com).
    base_url: str = ""

    # Ingest limits.
    max_body_bytes: int = 10 * 1024 * 1024          # as sent (maybe compressed)
    max_decompressed_bytes: int = 20 * 1024 * 1024  # after gzip/deflate
    max_event_bytes: int = 1024 * 1024              # one event item
    max_envelope_items: int = 100
    rate_limit_per_minute: int = 600                # events per project per minute; 0 = off
    sample_after: int = 1000                        # bodies per issue per day; after that, counts only

    # What we keep from an event.
    breadcrumbs_cap: int = 50
    user_fields: tuple = ("id",)
    request_fields: tuple = ("url", "method")
    keep_frame_vars: bool = False

    # Alerts.
    alert_cooldown_seconds: int = 3600
    alert_emails: tuple = ()
    alert_webhook: str = ""

    # Cleanup (all can be changed; some per project).
    event_retention_days: int = 30
    events_per_issue: int = 100
    issue_retention_days: int = 90
    day_count_retention_days: int = 400
    checkin_retention_days: int = 30
    checkins_per_monitor: int = 500
    sent_alert_retention_days: int = 30
    failed_alert_retention_days: int = 7
    counter_retention_days: int = 2
    max_db_mb: float = 500
    cleanup_interval_seconds: int = 600
    monitor_check_seconds: int = 60
    cleanup_batch: int = 2000
    cleanup_budget_seconds: float = 2.0
    tick_budget_seconds: float = 30.0

    health_check_names: tuple = field(default=DEFAULT_HEALTH_CHECKS)

    def project_value(self, project, name: str):
        """A per-project override if set, else the global default."""
        v = getattr(project, name, None) if project is not None else None
        if v is None or v == "":
            return getattr(self, name)
        return v

    def rate_limit_for(self, project) -> int:
        v = getattr(project, "rate_limit", None)
        return self.rate_limit_per_minute if v is None else v

    @classmethod
    def from_mapping(cls, data: dict, *, upper: bool = False) -> "Settings":
        """Build from a dict of overrides; unknown keys are ignored. Tuples
        accept lists or comma-separated strings."""
        s = cls()
        for f in fields(cls):
            key = f.name.upper() if upper else f.name
            if key not in data:
                continue
            raw = data[key]
            default = getattr(s, f.name)
            setattr(s, f.name, coerce(raw, default))
        return s


def coerce(raw, default):
    if isinstance(default, bool):
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        if isinstance(raw, str):
            return tuple(p.strip() for p in raw.split(",") if p.strip())
        return tuple(raw)
    return raw if isinstance(raw, str) else str(raw)
