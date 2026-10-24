from django.core.management.base import BaseCommand

from foxray.django.runtime import runtime


class Command(BaseCommand):
    help = "Create a project for another app to send to, and print its DSN."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--base-url", help="e.g. https://askrobots.com (default: FOXRAY['BASE_URL'])")

    def handle(self, *args, **options):
        rt = runtime()
        project = rt.store.get_project_by_name(options["name"]) or rt.store.create_project(options["name"])
        if options["base_url"]:
            rt.settings.base_url = options["base_url"]
        self.stdout.write(rt.dsn(project))
