"""Ingest endpoints for other apps (the app's own errors need no HTTP).

CSRF-exempt and independent of sessions and auth: the key in the request
is the only credential. The body is read straight from the stream, so
DATA_UPLOAD_MAX_MEMORY_SIZE does not apply; Foxray's own limit does."""

import logging

from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt

from .runtime import run_after, runtime

log = logging.getLogger("foxray.django")


def _response(r):
    resp = HttpResponse(r.body, status=r.status, content_type=r.headers.pop("Content-Type", "application/json"))
    for k, v in r.headers.items():
        resp[k] = v
    return resp


def _ingest(request, project_id, kind):
    rt = runtime()
    if request.method == "OPTIONS":
        return _response(rt.ingestor.preflight())
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST", "OPTIONS"])
    limit = rt.settings.max_body_bytes
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > limit:
        return JsonResponse({"detail": "body too large"}, status=413)
    body = request.read(limit + 1)
    if len(body) > limit:
        return JsonResponse({"detail": "body too large"}, status=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    query = {k: request.GET.get(k) for k in request.GET}
    try:
        r = rt.ingestor.handle(project_id, kind, headers, query, body)
    except Exception:  # noqa: BLE001 - logged, never re-raised (no capture loop)
        log.exception("ingest failed")
        return JsonResponse({"detail": "internal error"}, status=500)
    resp = _response(r)
    if r.after is not None:
        run_after(r.after)
    return resp


@csrf_exempt
def envelope(request, project_id):
    return _ingest(request, project_id, "envelope")


@csrf_exempt
def store(request, project_id):
    return _ingest(request, project_id, "store")


def home(request):
    try:
        return redirect(reverse("admin:app_list", kwargs={"app_label": "foxray"}))
    except NoReverseMatch:
        return HttpResponse("Foxray is running. Add django.contrib.admin to see issues.", content_type="text/plain")
