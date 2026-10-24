"""Create the "default" project, where the app's own errors go."""

import secrets

from django.db import migrations
from django.utils import timezone


def create_default(apps, schema_editor):
    Project = apps.get_model("foxray", "Project")
    if not Project.objects.using(schema_editor.connection.alias).exists():
        Project.objects.using(schema_editor.connection.alias).create(
            name="default", public_key=secrets.token_hex(16), created_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("foxray", "0001_initial")]
    operations = [migrations.RunPython(create_default, migrations.RunPython.noop)]
