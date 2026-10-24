"""The tables Foxray's SQL store reads and writes. The admin uses these
models; ingest goes through ``foxray.core.sql.SqlStore`` on Django's own
database connection, so the standalone server and the Django app share one
implementation (and one storage contract test suite)."""

import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from foxray.core.filters import RULE_KINDS, PatternError, validate_pattern
from foxray.core.models import LEVELS


def new_key() -> str:
    return secrets.token_hex(16)


LEVEL_CHOICES = [(lv, lv) for lv in LEVELS]
STATUS_CHOICES = [("unresolved", "unresolved"), ("resolved", "resolved"), ("ignored", "ignored")]


class Project(models.Model):
    name = models.CharField(max_length=100)
    public_key = models.CharField(max_length=64, unique=True, default=new_key,
                                  help_text="The key in the DSN. Change it to cut off old senders.")
    created_at = models.DateTimeField(default=timezone.now)
    rate_limit = models.IntegerField(null=True, blank=True, help_text="Events a minute; empty = default, 0 = off.")
    sample_after = models.IntegerField(null=True, blank=True,
                                       help_text="Event bodies kept per issue a day; after that only counts.")
    event_retention_days = models.IntegerField(null=True, blank=True)
    events_per_issue = models.IntegerField(null=True, blank=True, help_text="Newest event bodies kept per issue.")
    issue_retention_days = models.IntegerField(null=True, blank=True,
                                               help_text="Resolved/ignored issues not seen this long are deleted.")
    alert_new = models.BooleanField(default=True, help_text="Alert on new issues.")
    alert_regression = models.BooleanField(default=True, help_text="Alert when a resolved issue comes back.")
    alert_min_level = models.CharField(max_length=10, blank=True, choices=LEVEL_CHOICES)
    alert_environments = models.CharField(max_length=500, blank=True, help_text="Comma-separated; empty = all.")
    alert_emails = models.CharField(max_length=1000, blank=True,
                                    help_text="Comma-separated; empty = FOXRAY['ALERT_EMAILS'], ADMINS, or superusers.")
    alert_webhook = models.CharField(max_length=500, blank=True, help_text="POST alerts here as JSON.")
    filter_crawlers = models.BooleanField(default=True, help_text="Drop events from crawlers and bots.")
    filter_js_noise = models.BooleanField(default=False,
                                          help_text="Drop JavaScript errors from browser extensions and localhost.")
    filter_health_checks = models.BooleanField(default=True, help_text="Drop errors in health-check requests.")
    health_check_names = models.CharField(max_length=500, blank=True, help_text="More health-check paths.")

    class Meta:
        db_table = "foxray_project"

    def __str__(self):
        return self.name


class Issue(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    fingerprint = models.TextField()
    title = models.TextField()
    culprit = models.TextField(blank=True)
    level = models.CharField(max_length=16, choices=LEVEL_CHOICES)
    platform = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="unresolved")
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    times_seen = models.BigIntegerField("count")
    users_seen = models.BigIntegerField("users")
    last_environment = models.TextField(blank=True)
    last_release = models.TextField(blank=True)
    last_alerted_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    reopened_at = models.DateTimeField(null=True, blank=True)
    ignore_until = models.DateTimeField(null=True, blank=True)
    ignore_until_count = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "foxray_issue"
        constraints = [models.UniqueConstraint(fields=["project", "fingerprint"], name="foxray_issue_fp")]
        indexes = [models.Index(fields=["project", "status", "last_seen"], name="foxray_issue_list"),
                   models.Index(fields=["last_seen"], name="foxray_issue_last_seen")]
        permissions = []

    def __str__(self):
        return self.title


class Event(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    event_id = models.CharField(max_length=32)
    received_at = models.DateTimeField()
    occurred_at = models.DateTimeField(null=True, blank=True)
    level = models.CharField(max_length=16)
    title = models.TextField()
    environment = models.CharField(max_length=64, blank=True)
    release = models.TextField(blank=True)
    user_key = models.TextField(blank=True)
    data = models.TextField()

    class Meta:
        db_table = "foxray_event"
        constraints = [models.UniqueConstraint(fields=["project", "event_id"], name="foxray_event_id")]
        indexes = [models.Index(fields=["issue", "id"], name="foxray_event_issue"),
                   models.Index(fields=["received_at"], name="foxray_event_received")]

    def __str__(self):
        return self.event_id


class IssueDay(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    project_id = models.BigIntegerField()
    day = models.CharField(max_length=10)
    n = models.BigIntegerField()

    class Meta:
        db_table = "foxray_issue_day"
        constraints = [models.UniqueConstraint(fields=["issue", "day"], name="foxray_issue_day_u")]
        indexes = [models.Index(fields=["day"], name="foxray_issue_day_day")]


class IssueUser(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    user_key = models.TextField()

    class Meta:
        db_table = "foxray_issue_user"
        constraints = [models.UniqueConstraint(fields=["issue", "user_key"], name="foxray_issue_user_u")]


class IssueTag(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE)
    key = models.CharField(max_length=32)
    value = models.TextField()
    n = models.BigIntegerField()
    last_seen = models.DateTimeField()

    class Meta:
        db_table = "foxray_issue_tag"
        constraints = [models.UniqueConstraint(fields=["issue", "key", "value"], name="foxray_issue_tag_u")]
        indexes = [models.Index(fields=["key", "value"], name="foxray_issue_tag_kv")]


class Monitor(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    slug = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    schedule_type = models.CharField(max_length=16, blank=True)
    schedule_value = models.CharField(max_length=200, blank=True)
    schedule_unit = models.CharField(max_length=16, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    checkin_margin = models.IntegerField(default=1, help_text="Minutes late before a run counts as missed.")
    max_runtime = models.IntegerField(default=30, help_text="Minutes a run may take before it times out.")
    status = models.CharField(max_length=16, default="new")
    active = models.BooleanField(default=True, help_text="Off: keep recording, never alert.")
    last_checkin_at = models.DateTimeField(null=True, blank=True)
    next_due_at = models.DateTimeField(null=True, blank=True)
    last_alerted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "foxray_monitor"
        constraints = [models.UniqueConstraint(fields=["project", "slug"], name="foxray_monitor_slug")]

    def __str__(self):
        return self.slug


class CheckIn(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    check_in_id = models.CharField(max_length=32)
    status = models.CharField(max_length=16)
    duration = models.FloatField(null=True, blank=True)
    environment = models.CharField(max_length=64, blank=True)
    release = models.TextField(blank=True)
    received_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        db_table = "foxray_checkin"
        verbose_name = "check-in"
        constraints = [models.UniqueConstraint(fields=["monitor", "check_in_id"], name="foxray_checkin_id")]
        indexes = [models.Index(fields=["monitor", "id"], name="foxray_checkin_monitor"),
                   models.Index(fields=["status"], name="foxray_checkin_status")]


class FilterRule(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    kind = models.CharField("match", max_length=16, choices=[(k, k) for k in RULE_KINDS])
    pattern = models.CharField(max_length=200)
    is_regex = models.BooleanField("regex", default=False)
    environment = models.CharField(max_length=64, blank=True, help_text="Only in this environment (empty = any).")
    release = models.CharField(max_length=200, blank=True, help_text="Only in this release (empty = any).")
    enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "foxray_filter"
        verbose_name = "filter"

    def clean(self):
        try:
            validate_pattern(self.pattern, self.is_regex)
        except PatternError as exc:
            raise ValidationError({"pattern": str(exc)}) from None

    def __str__(self):
        return f"{self.kind}: {self.pattern}"


class Counter(models.Model):
    project_id = models.BigIntegerField()
    day = models.CharField(max_length=10)
    reason = models.CharField(max_length=100)
    n = models.BigIntegerField()

    class Meta:
        db_table = "foxray_counter"
        constraints = [models.UniqueConstraint(fields=["project_id", "day", "reason"], name="foxray_counter_u")]


class RateWindow(models.Model):
    project_id = models.BigIntegerField()
    window_start = models.BigIntegerField()
    n = models.BigIntegerField()

    class Meta:
        db_table = "foxray_rate"
        constraints = [models.UniqueConstraint(fields=["project_id", "window_start"], name="foxray_rate_u")]


class Alert(models.Model):
    project_id = models.BigIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=32)
    channel = models.CharField(max_length=16)
    issue_id = models.BigIntegerField(null=True, blank=True)
    monitor_id = models.BigIntegerField(null=True, blank=True)
    subject = models.TextField()
    body = models.TextField()
    payload = models.TextField()
    status = models.CharField(max_length=16)
    attempts = models.IntegerField()
    next_attempt_at = models.DateTimeField()
    created_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "foxray_alert"
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="foxray_alert_due")]

    def __str__(self):
        return self.subject


class State(models.Model):
    name = models.CharField(max_length=64, unique=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    value = models.TextField(blank=True)

    class Meta:
        db_table = "foxray_state"


class Storage(models.Model):
    """No table: the admin's Storage page."""

    class Meta:
        managed = False
        db_table = "foxray_storage_page"
        verbose_name = "storage"
        verbose_name_plural = "storage"
