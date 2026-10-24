"""The promise: add the app, add one URL line, migrate. An error in a view
shows up in /admin/ for the superuser, with no other configuration (no DSN,
no FOXRAY settings, no Foxray users)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SETTINGS = '''
SECRET_KEY = "zero-config"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "foxray.django",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "site_urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                                                 "django.contrib.auth.context_processors.auth",
                                                 "django.contrib.messages.context_processors.messages"]}}]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "site.sqlite3"}}
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
'''

URLS = '''
from django.contrib import admin
from django.urls import include, path


def compute_total(cart):
    return sum(cart) / len(cart)


def checkout(request):
    return compute_total([])


urlpatterns = [
    path("admin/", admin.site.urls),
    path("foxray/", include("foxray.django.urls")),   # the one line
    path("checkout/", checkout),
]
'''

FLOW = '''
import os, re, sys
os.environ["DJANGO_SETTINGS_MODULE"] = "site_settings"
import django
django.setup()
from django.core.management import call_command
call_command("migrate", verbosity=0)

from django.contrib.auth.models import User
from django.test import Client
import sentry_sdk

assert sentry_sdk.get_client().is_active(), "Foxray did not start the SDK in-process"
client = Client(raise_request_exception=False)
response = client.get("/checkout/")
assert response.status_code == 500, response.status_code
sentry_sdk.flush(timeout=10)

admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
client.force_login(admin)
index = client.get("/admin/").content.decode()
assert "Foxray" in index and "Issues" in index and "Storage" in index, "no Foxray section in the admin"
listing = client.get("/admin/foxray/issue/").content.decode()
assert "ZeroDivisionError" in listing, listing[:2000]
issue_id = re.search(r"/admin/foxray/issue/(\\d+)/change/", listing).group(1)
detail = client.get(f"/admin/foxray/issue/{issue_id}/change/").content.decode()
assert "compute_total" in detail and "sum(cart) / len(cart)" in detail, "no stack trace"
assert "/checkout/" in detail
projects = client.get("/admin/foxray/project/").content.decode()
assert "default" in projects and "/foxray/1" in projects, "no default project with a DSN"
print("ZERO-CONFIG OK")
'''


@pytest.mark.slow
def test_add_app_migrate_raise_see_it_in_admin(tmp_path):
    (tmp_path / "site_settings.py").write_text(textwrap.dedent(SETTINGS))
    (tmp_path / "site_urls.py").write_text(textwrap.dedent(URLS))
    (tmp_path / "flow.py").write_text(textwrap.dedent(FLOW))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "FOXRAY_", "SENTRY_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(ROOT)])
    result = subprocess.run([sys.executable, "flow.py"], cwd=tmp_path, env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ZERO-CONFIG OK" in result.stdout
