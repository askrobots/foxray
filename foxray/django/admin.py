"""Foxray in the Django admin: Issues, Events, Monitors, Check-ins, Filters,
Projects, Alerts and Storage under a "Foxray" section. Access is Django's
own: superusers see everything; staff need the model permissions
(``foxray.view_issue`` to look, ``foxray.change_issue`` to resolve or
ignore), granted through groups as usual."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone as dt_timezone

from django.contrib import admin, messages
from django.contrib.admin import DateFieldListFilter, SimpleListFilter
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from foxray.core.maintenance import storage_report
from foxray.core.present import (day_bars, exceptions_for_display, kv_rows, reason_label, schedule_text,
                                 size_text, when)
from foxray.core.util import days_ago_str

from .models import Alert, CheckIn, Event, FilterRule, Issue, IssueTag, Monitor, Project, Storage
from .runtime import runtime

_local = threading.local()


def _counts_only_warning(request) -> None:
    if runtime().store.get_state_value("mode", "normal") == "counts_only":
        url = reverse("admin:foxray_storage_changelist")
        messages.warning(request, format_html(
            'Storage is full: Foxray is counting and alerting but not keeping new event bodies. '
            '<a href="{}">Storage</a>', url))


# -- filters for the issue list ------------------------------------------------

class StatusFilter(SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [("unresolved", "Unresolved"), ("resolved", "Resolved"), ("ignored", "Ignored"), ("all", "All")]

    def choices(self, changelist):
        current = self.value() or "unresolved"
        for value, label in self.lookup_choices:
            yield {"selected": current == value, "query_string": changelist.get_query_string({self.parameter_name: value}),
                   "display": label}

    def queryset(self, request, queryset):
        value = self.value() or "unresolved"
        return queryset if value == "all" else queryset.filter(status=value)


class _TagFilter(SimpleListFilter):
    key = ""

    def lookups(self, request, model_admin):
        values = (IssueTag.objects.filter(key=self.key).values_list("value", flat=True).distinct()
                  .order_by("value")[:100])
        return [(v, v) for v in values]

    def queryset(self, request, queryset):
        if self.value():
            ids = IssueTag.objects.filter(key=self.key, value=self.value()).values("issue_id")
            return queryset.filter(id__in=ids)
        return queryset


class EnvironmentFilter(_TagFilter):
    title = "environment"
    parameter_name = "environment"
    key = "environment"


class ReleaseFilter(_TagFilter):
    title = "release"
    parameter_name = "release"
    key = "release"


# -- issues -------------------------------------------------------------------

@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("title_link", "level", "status", "times_seen", "users_seen", "first_seen", "last_seen", "project")
    list_display_links = None
    list_filter = (StatusFilter, "level", EnvironmentFilter, ReleaseFilter, ("first_seen", DateFieldListFilter),
                   ("last_seen", DateFieldListFilter), "project")
    search_fields = ("title", "culprit")
    date_hierarchy = "last_seen"
    ordering = ("-last_seen",)
    list_per_page = 50
    actions = ("resolve", "ignore", "reopen")

    def has_add_permission(self, request):
        return False

    @admin.display(description="issue", ordering="title")
    def title_link(self, obj):
        url = reverse("admin:foxray_issue_change", args=[obj.id])
        return format_html('<a href="{}"><strong>{}</strong></a><br><span class="quiet">{}</span>', url, obj.title,
                           obj.culprit)

    def changelist_view(self, request, extra_context=None):
        _counts_only_warning(request)
        return super().changelist_view(request, extra_context)

    def _set(self, request, queryset, status, label):
        store, now = runtime().store, timezone.now()
        n = 0
        for issue_id in queryset.values_list("id", flat=True):
            store.set_issue_status(issue_id, status, now=now)
            n += 1
        self.message_user(request, f"{label} {n} issue(s).", messages.SUCCESS)

    @admin.action(description="Resolve selected issues", permissions=["change"])
    def resolve(self, request, queryset):
        self._set(request, queryset, "resolved", "Resolved")

    @admin.action(description="Ignore selected issues", permissions=["change"])
    def ignore(self, request, queryset):
        self._set(request, queryset, "ignored", "Ignored")

    @admin.action(description="Reopen selected issues", permissions=["change"])
    def reopen(self, request, queryset):
        self._set(request, queryset, "unresolved", "Reopened")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """The issue page: the latest event with its stack trace."""
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        rt = runtime()
        store = rt.store
        issue = store.get_issue(object_id)
        if issue is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)
        here = reverse("admin:foxray_issue_change", args=[issue.id])
        if request.method == "POST":
            if not self.has_change_permission(request):
                raise PermissionDenied
            now = timezone.now()
            action = request.POST.get("action")
            if action == "resolve":
                store.set_issue_status(issue.id, "resolved", now=now)
            elif action == "reopen":
                store.set_issue_status(issue.id, "unresolved", now=now)
            elif action == "ignore":
                store.set_issue_status(issue.id, "ignored", now=now)
            elif action == "ignore_count" and request.POST.get("count", "").isdigit():
                store.set_issue_status(issue.id, "ignored", now=now, ignore_more=max(1, int(request.POST["count"])))
            elif action == "ignore_until":
                try:
                    until = datetime.strptime(request.POST.get("until", ""), "%Y-%m-%d").replace(tzinfo=dt_timezone.utc)
                    store.set_issue_status(issue.id, "ignored", now=now, ignore_until=until)
                except ValueError:
                    messages.error(request, "Pick a date to ignore until.")
            return HttpResponseRedirect(here)
        now = timezone.now()
        event = None
        if request.GET.get("event", "").isdigit():
            event = store.get_event(request.GET["event"])
            if event is not None and event.issue_id != issue.id:
                event = None
        event = event or store.latest_event(issue.id)
        payload = event.payload if event else {}
        sections = []
        for title, value in (("Request", payload.get("request")), ("User", payload.get("user")),
                             ("Tags", payload.get("tags")), ("Context", payload.get("contexts")),
                             ("Extra", payload.get("extra"))):
            rows = kv_rows(value) if isinstance(value, dict) else []
            if rows:
                sections.append((title, rows))
        project = store.get_project(issue.project_id)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts, "title": issue.title, "issue": issue, "project": project,
            "event": event, "payload": payload, "exceptions": exceptions_for_display(payload),
            "breadcrumbs": list(reversed(payload.get("breadcrumbs") or [])),
            "sections": sections, "tags": store.issue_tags(issue.id),
            "bars": day_bars(store.issue_days(issue.id, days_ago_str(now, 30)), now),
            "events": store.list_events(issue.id, 15), "can_change": self.has_change_permission(request),
            "first_seen": when(issue.first_seen), "last_seen": when(issue.last_seen),
            "ignore_note": (f"until {issue.ignore_until_count} events" if issue.ignore_until_count else
                            f"until {when(issue.ignore_until)}" if issue.ignore_until else ""),
            "event_when": when(event.received_at) if event else "",
            "has_view_permission": True,
        }
        return TemplateResponse(request, "admin/foxray/issue_detail.html", context)


# -- events -------------------------------------------------------------------

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "issue_link", "level", "environment", "release", "received_at")
    list_filter = ("level", ("received_at", DateFieldListFilter), "project")
    search_fields = ("event_id", "title", "release", "environment")
    date_hierarchy = "received_at"
    ordering = ("-id",)
    list_select_related = ("issue",)
    fields = ("event_id", "issue_link", "project", "level", "title", "environment", "release", "received_at",
              "occurred_at", "data_pretty")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="issue")
    def issue_link(self, obj):
        url = reverse("admin:foxray_issue_change", args=[obj.issue_id]) + f"?event={obj.id}"
        return format_html('<a href="{}">{}</a>', url, obj.issue.title if obj.issue_id else "")

    @admin.display(description="data")
    def data_pretty(self, obj):
        try:
            text = json.dumps(json.loads(obj.data), indent=2)
        except ValueError:
            text = obj.data
        return format_html("<pre style='white-space:pre-wrap;max-width:100%'>{}</pre>", text)


# -- monitors -----------------------------------------------------------------

@admin.register(Monitor)
class MonitorAdmin(admin.ModelAdmin):
    list_display = ("slug", "project", "status", "schedule", "last_checkin_at", "next_due_at", "active")
    list_filter = ("status", "active", "project")
    search_fields = ("slug", "name")
    actions = ("pause", "resume")
    fields = ("slug", "name", "project", "status", "active", "schedule", "timezone", "checkin_margin", "max_runtime",
              "last_checkin_at", "next_due_at", "last_alerted_at", "recent_checkins")
    readonly_fields = ("slug", "project", "status", "schedule", "timezone", "last_checkin_at", "next_due_at",
                       "last_alerted_at", "recent_checkins")

    def has_add_permission(self, request):
        return False

    @admin.display(description="schedule")
    def schedule(self, obj):
        return schedule_text(obj)

    @admin.display(description="recent check-ins")
    def recent_checkins(self, obj):
        rows = CheckIn.objects.filter(monitor=obj).order_by("-id")[:20]
        if not rows:
            return "none"
        return format_html("<table><tr><th>status</th><th>received</th><th>duration</th><th>environment</th></tr>{}</table>",
                           format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                                            ((c.status, when(c.received_at),
                                              "" if c.duration is None else f"{c.duration:.1f}s", c.environment)
                                             for c in rows)))

    @admin.action(description="Pause alerts for selected monitors", permissions=["change"])
    def pause(self, request, queryset):
        queryset.update(active=False)

    @admin.action(description="Resume alerts for selected monitors", permissions=["change"])
    def resume(self, request, queryset):
        queryset.update(active=True)


@admin.register(CheckIn)
class CheckInAdmin(admin.ModelAdmin):
    list_display = ("monitor", "status", "received_at", "duration", "environment")
    list_filter = ("status", ("received_at", DateFieldListFilter), "monitor")
    date_hierarchy = "received_at"
    ordering = ("-id",)
    list_select_related = ("monitor",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# -- filters and projects -------------------------------------------------------

@admin.register(FilterRule)
class FilterRuleAdmin(admin.ModelAdmin):
    list_display = ("kind", "pattern", "is_regex", "environment", "release", "enabled", "project", "note")
    list_editable = ("enabled",)
    list_filter = ("kind", "enabled", "project")
    search_fields = ("pattern", "note")


class FilterRuleInline(admin.TabularInline):
    model = FilterRule
    extra = 0
    fields = ("kind", "pattern", "is_regex", "environment", "release", "enabled", "note")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "dsn")
    inlines = (FilterRuleInline,)
    readonly_fields = ("dsn", "created_at", "dropped")
    fieldsets = (
        (None, {"fields": ("name", "dsn", "public_key", "created_at")}),
        ("Alerts", {"fields": ("alert_new", "alert_regression", "alert_min_level", "alert_environments",
                               "alert_emails", "alert_webhook")}),
        ("Noise", {"fields": ("filter_crawlers", "filter_health_checks", "health_check_names", "filter_js_noise")}),
        ("Limits (empty means the default)", {"fields": ("rate_limit", "sample_after", "events_per_issue",
                                                         "event_retention_days", "issue_retention_days")}),
        ("Dropped in the last 2 days", {"fields": ("dropped",)}),
    )

    def changelist_view(self, request, extra_context=None):
        _local.request = request
        return super().changelist_view(request, extra_context)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        _local.request = request
        return super().changeform_view(request, object_id, form_url, extra_context)

    @admin.display(description="DSN")
    def dsn(self, obj):
        if not obj or not obj.pk:
            return "shown after saving"
        value = runtime().dsn(obj, getattr(_local, "request", None))
        return format_html('<input readonly value="{}" size="70" onclick="this.select()" style="font-family:monospace">',
                           value)

    @admin.display(description="dropped")
    def dropped(self, obj):
        if not obj or not obj.pk:
            return "-"
        store = runtime().store
        rules = {r.id: r for r in store.list_filters(obj.pk)}
        rows = store.counters(obj.pk, days_ago_str(timezone.now(), 2))
        if not rows:
            return "nothing"
        return format_html("<table>{}</table>", format_html_join(
            "", "<tr><td>{}</td><td>{}</td><td style='text-align:right'>{}</td></tr>",
            ((day, reason_label(reason, rules), n) for _, day, reason, n in rows)))


# -- alerts -------------------------------------------------------------------

@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("subject", "kind", "channel", "status", "attempts", "created_at", "sent_at", "last_error")
    list_filter = ("status", "kind", "channel", ("created_at", DateFieldListFilter))
    date_hierarchy = "created_at"
    ordering = ("-id",)
    actions = ("retry",)

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    @admin.action(description="Send selected alerts again now", permissions=["change"])
    def retry(self, request, queryset):
        n = queryset.exclude(status="sent").update(status="pending", next_attempt_at=timezone.now())
        runtime().outbox.flush()
        self.message_user(request, f"Retried {n} alert(s).")


# -- storage --------------------------------------------------------------------

@admin.register(Storage)
class StorageAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        rt = runtime()
        if request.method == "POST":
            if not self.has_change_permission(request):
                raise PermissionDenied
            result = rt.maintenance.run(force=True, budget=rt.settings.tick_budget_seconds)
            if result is None:
                messages.info(request, "A cleanup is already running.")
            else:
                n = sum((result.get("removed") or {}).values())
                messages.success(request, f"Cleanup removed {n} row(s).")
            return HttpResponseRedirect(request.path)
        report = storage_report(rt.store, rt.settings, rt.maintenance)
        last = report["last_cleanup"] or {}
        context = {
            **self.admin_site.each_context(request), "opts": self.opts, "title": "Storage",
            "report": report, "used": size_text(report["used_bytes"]), "cap": size_text(report["cap_bytes"]),
            "file": size_text(report["file_bytes"]), "percent": report["percent"] or 0,
            "last_at": when(report["last_cleanup_at"]), "removed": sorted((last.get("removed") or {}).items()),
            "tables": sorted(report["tables"].items()), "can_change": self.has_change_permission(request),
            "interval_minutes": rt.settings.cleanup_interval_seconds // 60,
            "has_view_permission": True,
        }
        return TemplateResponse(request, "admin/foxray/storage.html", context)
