"""Django settings for the Google Cloud Run deployment.

Every value that names the deployment (hosts, services, buckets, keys) comes from the
environment, so the same image runs in staging and production.
"""

import sentry_sdk
from decouple import Csv
from sentry_sdk.integrations.django import DjangoIntegration

from pydotorg.settings.base import *

DEBUG = TEMPLATE_DEBUG = False

DATABASE_CONN_MAX_AGE = config("DATABASE_CONN_MAX_AGE", default=600, cast=int)
DATABASES["default"]["CONN_MAX_AGE"] = DATABASE_CONN_MAX_AGE
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

## Django Caching

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache_table",
    }
}

HAYSTACK_SEARCHBOX_SSL_URL = config("SEARCHBOX_SSL_URL")

HAYSTACK_CONNECTIONS = {
    "default": {
        "ENGINE": "haystack.backends.elasticsearch7_backend.Elasticsearch7SearchEngine",
        "URL": HAYSTACK_SEARCHBOX_SSL_URL,
        "INDEX_NAME": config("HAYSTACK_INDEX", default="haystack-prod"),
    },
}

SECRET_KEY = config("SECRET_KEY")

ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

MIDDLEWARE = ["whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE]

## Media and static files in a Cloud Storage bucket
# The bucket uses uniform bucket-level access and is readable by allUsers, so objects get
# no ACL and URLs are not signed. The service account writes with its own credentials.

GS_BUCKET_NAME = config("GS_BUCKET_NAME")
GS_PROJECT_ID = GOOGLE_CLOUD_PROJECT or None
GS_DEFAULT_ACL = None
GS_QUERYSTRING_AUTH = False
# A CDN in front of the bucket, such as Fastly, when there is one; bin/render-nginx-conf
# reads the same variables to redirect the legacy static paths.
GS_CUSTOM_ENDPOINT = config("GS_CUSTOM_ENDPOINT", default="").rstrip("/") or None
GS_PUBLIC_BASE_URL = GS_CUSTOM_ENDPOINT or f"https://storage.googleapis.com/{GS_BUCKET_NAME}"

MEDIAFILES_LOCATION = "media"
MEDIA_URL = f"{GS_PUBLIC_BASE_URL}/{MEDIAFILES_LOCATION}/"
STATIC_URL = config("STATIC_URL", default=f"{GS_PUBLIC_BASE_URL}/static/")
# Where pydotorg.views.MediaMigrationView sends the legacy /m/ URLs.
MEDIA_MIGRATION_BASE_URL = GS_PUBLIC_BASE_URL

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "location": MEDIAFILES_LOCATION,
            "file_overwrite": False,
            "object_parameters": {"cache_control": "public, max-age=86400"},
        },
    },
    # Written by `manage.py collectstatic` at deploy time, before the new revision takes
    # traffic; the image has no static files of its own.
    "staticfiles": {
        "BACKEND": "custom_storages.gcloud.GoogleCloudPipelineManifestStorage",
        "OPTIONS": {
            "location": "static",
            "object_parameters": {"cache_control": "public, max-age=604800"},
        },
    },
}

EMAIL_HOST = config("EMAIL_HOST")
EMAIL_HOST_USER = config("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD")
EMAIL_PORT = int(config("EMAIL_PORT"))
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")

# Fastly API Key
FASTLY_API_KEY = config("FASTLY_API_KEY")

# Google's front end terminates TLS for Cloud Run, for requests from Fastly and to the
# run.app URL alike, and always sets X-Forwarded-Proto.
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "Fastly-Client-IP"

sentry_sdk.init(
    dsn=config("SENTRY_DSN"),
    integrations=[DjangoIntegration()],
    # Cloud Run sets K_REVISION on every revision; SOURCE_COMMIT wins when the deploy sets it.
    release=config("SOURCE_COMMIT", default=config("K_REVISION", default="")) or None,
    send_default_pii=True,
    traces_sample_rate=0.1,
    profiles_sample_rate=0.1,
)
