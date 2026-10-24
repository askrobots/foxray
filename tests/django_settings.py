import os
import tempfile
from urllib.parse import urlsplit

SECRET_KEY = "foxray-tests"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
TIME_ZONE = "UTC"
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "foxray.django",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "tests.django_urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                                       "django.contrib.auth.context_processors.auth",
                                       "django.contrib.messages.context_processors.messages"]},
}]
_pg = os.environ.get("FOXRAY_TEST_DJANGO_PG", "")
if _pg:
    _u = urlsplit(_pg)
    DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "foxray_django",
                             "USER": _u.username or "", "PASSWORD": _u.password or "", "HOST": _u.hostname or "",
                             "PORT": str(_u.port or "")}}
else:
    # A file, not memory: the live-server and threaded tests share it across connections.
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
                             "OPTIONS": {"timeout": 30},
                             "TEST": {"NAME": os.path.join(tempfile.gettempdir(), f"foxray-django-{os.getpid()}.sqlite3")}}}
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
ADMINS = [("Ops", "ops@example.com")]
SERVER_EMAIL = "foxray@example.com"
FOXRAY = {"AUTO_INIT": False, "SYNC_AFTER": True, "BASE_URL": "http://testserver"}
