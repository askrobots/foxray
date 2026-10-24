from django.contrib import admin
from django.urls import include, path


def boom(request):
    def compute_total():
        return 1 / 0
    return compute_total()


urlpatterns = [
    path("admin/", admin.site.urls),
    path("foxray/", include("foxray.django.urls")),
    path("boom/", boom),
]
