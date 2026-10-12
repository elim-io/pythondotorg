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

MEDIAFILES_LOCATION = "media"
STORAGES = {
    "default": {
        "BACKEND": "custom_storages.storages.MediaStorage",
    },
    "staticfiles": {
        "BACKEND": "custom_storages.storages.PipelineManifestStorage",
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

AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
AWS_DEFAULT_ACL = config("AWS_DEFAULT_ACL", default="public-read")
AWS_AUTO_CREATE_BUCKET = False
AWS_S3_OBJECT_PARAMETERS = {
    "CacheControl": "max-age=86400",
}
AWS_QUERYSTRING_AUTH = False
AWS_S3_FILE_OVERWRITE = False
AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="us-east-1")
AWS_S3_USE_SSL = True
AWS_S3_ENDPOINT_URL = config("AWS_S3_ENDPOINT_URL", default="https://s3.amazonaws.com")
