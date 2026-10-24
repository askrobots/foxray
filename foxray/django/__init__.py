"""Foxray as a Django app: add ``foxray.django`` to INSTALLED_APPS, add
``path("foxray/", include("foxray.django.urls"))``, run migrate. Errors from
the app itself are stored in-process (no DSN); other apps send to the
ingest URLs. The UI is the Django admin."""
