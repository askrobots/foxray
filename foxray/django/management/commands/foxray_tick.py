import json

from django.core.management.base import BaseCommand

from foxray.django.runtime import runtime


class Command(BaseCommand):
    help = "Check monitors, clean up if due, send pending alerts. For cron or the app's scheduler (optional)."

    def add_arguments(self, parser):
        parser.add_argument("--force-cleanup", action="store_true", help="clean up even if not due")

    def handle(self, *args, **options):
        result = runtime().housekeeper.tick(force_cleanup=options["force_cleanup"])
        self.stdout.write(json.dumps(result, default=str))
