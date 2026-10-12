"""Middleware for health checks, cache control and surrogate keys."""

import contextlib

from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpResponse

HEALTHZ_PATH = "/healthz"


class HealthCheck:
    """Answer GET /healthz with 200 once PostgreSQL answers, 503 when it does not.

    Cloud Run's startup and liveness probes call it from inside Google's network with
    a Host header that is not in ALLOWED_HOSTS, so it must run before any middleware
    that validates the host or redirects to HTTPS.
    """

    def __init__(self, get_response):
        """Store the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Check the database for /healthz; pass every other request on."""
        if request.path != HEALTHZ_PATH:
            return self.get_response(request)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError:
            # Drop the broken connection so the next probe opens a fresh one.
            with contextlib.suppress(DatabaseError):
                connection.close()
            return HttpResponse("database unavailable", status=503, content_type="text/plain")
        response = HttpResponse("ok", content_type="text/plain")
        response["Cache-Control"] = "no-store"
        return response


class AdminNoCaching:
    """Middleware to ensure the admin is not cached by Fastly or other caches."""

    def __init__(self, get_response):
        """Store the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Set Cache-Control to private for admin requests."""
        response = self.get_response(request)
        if request.path.startswith("/admin"):
            response["Cache-Control"] = "private"
        return response


class GlobalSurrogateKey:
    """Middleware to insert a Surrogate-Key for purging in Fastly or other caches.

    Adds both a global key (for full site purges) and section-based keys
    derived from the URL path (for targeted purges like /downloads/).
    """

    def __init__(self, get_response):
        """Store the get_response callable."""
        self.get_response = get_response

    def get_section_key(self, path):
        """Extract section surrogate key from URL path.

        Examples:
            /downloads/ -> downloads
            /downloads/release/python-3141/ -> downloads
            /events/python-events/ -> events
            / -> None

        """
        parts = path.strip("/").split("/")
        if parts and parts[0]:
            return parts[0]
        return None

    def __call__(self, request):
        """Append the global and section surrogate keys to the response header."""
        response = self.get_response(request)
        keys = []
        if hasattr(settings, "GLOBAL_SURROGATE_KEY"):
            keys.append(settings.GLOBAL_SURROGATE_KEY)

        section_key = self.get_section_key(request.path)
        if section_key:
            keys.append(section_key)

        existing = response.get("Surrogate-Key")
        if existing:
            keys.append(existing)

        if keys:
            response["Surrogate-Key"] = " ".join(keys)

        return response
