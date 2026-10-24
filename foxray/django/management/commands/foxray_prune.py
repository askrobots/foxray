import json

from django.core.management.base import BaseCommand

from foxray.django.runtime import runtime


class Command(BaseCommand):
    help = "Run a full cleanup now (it also runs on its own after ingest)."

    def add_arguments(self, parser):
        parser.add_argument("--budget", type=float, help="seconds to spend")

    def handle(self, *args, **options):
        rt = runtime()
        result = rt.maintenance.run(force=True, budget=options["budget"] or rt.settings.tick_budget_seconds)
        if result is None:
            self.stdout.write("Another cleanup is running; nothing done.")
        else:
            self.stdout.write(json.dumps(result, indent=2, default=str))
