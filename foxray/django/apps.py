from django.apps import AppConfig


class FoxrayConfig(AppConfig):
    name = "foxray.django"
    label = "foxray"
    verbose_name = "Foxray"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        try:  # Foxray's own log lines must never become events (no loops).
            from sentry_sdk.integrations.logging import ignore_logger
            ignore_logger("foxray")
            ignore_logger("foxray.*")
        except ImportError:
            pass
        from django.core.signals import setting_changed

        from . import runtime, sdk
        setting_changed.connect(runtime.reset_on_setting_change)
        sdk.auto_init()
