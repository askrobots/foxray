"""``path("foxray/", include("foxray.django.urls"))``. The DSN for project
N is then ``https://<key>@<host>/foxray/N``."""

from django.urls import path

from . import views

app_name = "foxray"

urlpatterns = [
    path("api/<int:project_id>/envelope/", views.envelope, name="envelope"),
    path("api/<int:project_id>/store/", views.store, name="store"),
    path("", views.home, name="home"),
]
