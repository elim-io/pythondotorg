import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from google.auth.credentials import AnonymousCredentials
from google.cloud.storage import Blob, Bucket
from storages.backends.gcloud import GoogleCloudStorage

from custom_storages.gcloud import GoogleCloudPipelineManifestStorage
from pydotorg.tests.test_cloudrun import BASE, CLOUD_RUN_ENV

BUCKET = "pdo-files"
PUBLIC = f"https://storage.googleapis.com/{BUCKET}"
CONNECTION = {"bucket_name": BUCKET, "project_id": "test", "credentials": AnonymousCredentials()}
MEDIA_OPTIONS = {
    "location": "media",
    "file_overwrite": False,
    "object_parameters": {"cache_control": "public, max-age=86400"},
}
STATIC_OPTIONS = {"location": "static", "object_parameters": {"cache_control": "public, max-age=604800"}}


class FakeBucket:
    """Keeps the objects in a dict and stands in for the Cloud Storage API calls."""

    def __init__(self):
        self.objects = {}
        patches = {
            (Bucket, "get_blob"): self.get_blob,
            (Bucket, "delete_blob"): self.delete_blob,
            (Blob, "upload_from_file"): self.upload_from_file,
            (Blob, "download_to_file"): self.download_to_file,
        }
        self.patchers = [mock.patch.object(owner, name, self.as_method(new)) for (owner, name), new in patches.items()]

    @staticmethod
    def as_method(handler):
        """Wrap a handler in a function, so the patched class passes its instance first."""

        def method(owner, *args, **kwargs):
            return handler(owner, *args, **kwargs)

        return method

    def __enter__(self):
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self.patchers:
            patcher.stop()

    def get_blob(self, bucket, name, *args, **kwargs):
        if name not in self.objects:
            return None
        return Blob(name, bucket)

    def delete_blob(self, bucket, name, *args, **kwargs):
        self.objects.pop(name, None)

    def upload_from_file(self, blob, file, rewind=False, content_type=None, predefined_acl=None, **kwargs):
        if rewind:
            file.seek(0)
        data = file.read()
        self.objects[blob.name] = {
            "data": data if isinstance(data, bytes) else data.encode(),
            "content_type": content_type,
            "cache_control": blob.cache_control,
            "acl": predefined_acl,
        }

    def download_to_file(self, blob, file, *args, **kwargs):
        file.write(self.objects[blob.name]["data"])


def load_cloud_run_settings(test, **env):
    """Load pydotorg.settings.cloudrun in a new process and return the storage settings."""
    code = (
        "import json, django; django.setup(); from django.conf import settings; "
        "print(json.dumps({'storages': settings.STORAGES, 'static': settings.STATIC_URL, "
        "'media': settings.MEDIA_URL, 'migration': settings.MEDIA_MIGRATION_BASE_URL}))"
    )
    result = subprocess.run(  # noqa: S603 - code from these tests
        [sys.executable, "-c", code],
        cwd=BASE,
        env={**os.environ, **CLOUD_RUN_ENV, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    test.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)


class MediaStorageTests(SimpleTestCase):
    def test_uploads_go_to_the_media_folder_of_the_bucket(self):
        storage = GoogleCloudStorage(querystring_auth=False, default_acl=None, **CONNECTION, **MEDIA_OPTIONS)
        with FakeBucket() as bucket:
            name = storage.save("sponsors/logo.png", ContentFile(b"png", name="logo.png"))
            again = storage.save("sponsors/logo.png", ContentFile(b"other", name="logo.png"))
            with storage.open(name) as stored:
                content = stored.read()

        self.assertEqual(name, "sponsors/logo.png")
        self.assertNotEqual(again, name, "an upload must not replace another one")
        self.assertEqual(content, b"png")
        stored = bucket.objects["media/sponsors/logo.png"]
        self.assertEqual(stored["cache_control"], "public, max-age=86400")
        self.assertEqual(stored["content_type"], "image/png")
        self.assertIsNone(stored["acl"], "the bucket's uniform access decides who reads")
        self.assertEqual(storage.url(name), f"{PUBLIC}/media/sponsors/logo.png")


class StaticStorageTests(SimpleTestCase):
    def collectstatic(self, source):
        storages = {
            **settings.STORAGES,
            "staticfiles": {
                "BACKEND": "custom_storages.gcloud.GoogleCloudPipelineManifestStorage",
                "OPTIONS": {"querystring_auth": False, "default_acl": None, **CONNECTION, **STATIC_OPTIONS},
            },
        }
        pipeline = {"PIPELINE_ENABLED": True, "STYLESHEETS": {}, "JAVASCRIPT": {}}
        with override_settings(
            STORAGES=storages,
            STATICFILES_DIRS=[source],
            STATICFILES_FINDERS=["django.contrib.staticfiles.finders.FileSystemFinder"],
            PIPELINE=pipeline,
        ):
            call_command("collectstatic", interactive=False, verbosity=0)
            return staticfiles_storage.url("css/site.css")

    def test_collectstatic_writes_hashed_files_and_the_manifest_to_the_bucket(self):
        with tempfile.TemporaryDirectory() as source, FakeBucket() as bucket:
            Path(source, "css").mkdir()
            Path(source, "img").mkdir()
            Path(source, "img", "logo.png").write_bytes(b"png")
            Path(source, "css", "site.css").write_text(
                '/* url("missing.png") */ body { background: url("../img/logo.png"); }'
            )
            url = self.collectstatic(source)

        manifest = json.loads(bucket.objects["static/staticfiles.json"]["data"])["paths"]
        hashed_css, hashed_logo = manifest["css/site.css"], manifest["img/logo.png"]
        self.assertNotEqual(hashed_css, "css/site.css")
        self.assertEqual(url, f"{PUBLIC}/static/{hashed_css}")
        css = bucket.objects[f"static/{hashed_css}"]
        self.assertIn(Path(hashed_logo).name, css["data"].decode())
        self.assertIn('url("missing.png")', css["data"].decode(), "urls in comments stay as they are")
        self.assertEqual(css["cache_control"], "public, max-age=604800")
        self.assertIn("static/img/logo.png", bucket.objects, "the unhashed name stays for the legacy paths")

    def test_reads_the_manifest_back_from_the_bucket(self):
        with FakeBucket() as bucket:
            manifest = {"paths": {"css/site.css": "css/site.abc123.css"}, "version": "1.1", "hash": "x"}
            bucket.objects["static/staticfiles.json"] = {"data": json.dumps(manifest).encode()}
            storage = GoogleCloudPipelineManifestStorage(
                querystring_auth=False, default_acl=None, **CONNECTION, **STATIC_OPTIONS
            )
            self.assertEqual(storage.url("css/site.css"), f"{PUBLIC}/static/css/site.abc123.css")


class StorageSettingsTests(SimpleTestCase):
    def test_local_development_and_tests_keep_the_disk(self):
        self.assertEqual(settings.STORAGES["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")

    def test_cloud_run_keeps_media_and_static_files_in_the_bucket(self):
        loaded = load_cloud_run_settings(self)
        self.assertEqual(loaded["storages"]["default"]["BACKEND"], "storages.backends.gcloud.GoogleCloudStorage")
        self.assertEqual(loaded["storages"]["default"]["OPTIONS"]["location"], "media")
        self.assertFalse(loaded["storages"]["default"]["OPTIONS"]["file_overwrite"])
        self.assertEqual(
            loaded["storages"]["staticfiles"]["BACKEND"], "custom_storages.gcloud.GoogleCloudPipelineManifestStorage"
        )
        self.assertEqual(loaded["static"], f"{PUBLIC}/static/")
        self.assertEqual(loaded["media"], f"{PUBLIC}/media/")
        self.assertEqual(loaded["migration"], PUBLIC)

    def test_a_cdn_in_front_of_the_bucket(self):
        loaded = load_cloud_run_settings(self, GS_CUSTOM_ENDPOINT="https://files.example.org/")
        self.assertEqual(loaded["static"], "https://files.example.org/static/")
        self.assertEqual(loaded["media"], "https://files.example.org/media/")

    def test_cloud_run_needs_no_aws_settings(self):
        env = {name: "" for name in os.environ if name.startswith("AWS_")}
        load_cloud_run_settings(self, **env)


class LegacyStaticPathTests(SimpleTestCase):
    def render(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in {"STATIC_URL", "GS_BUCKET_NAME", "GS_CUSTOM_ENDPOINT"}}
        return subprocess.run(  # noqa: S603 - our own script
            [BASE / "bin" / "render-nginx-conf"],
            env={**clean, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_legacy_static_paths_redirect_to_the_bucket(self):
        result = self.render(GS_BUCKET_NAME=BUCKET)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"rewrite ^/static/(.*)$ {PUBLIC}/static/$1 permanent;", result.stdout)
        self.assertIn(f"rewrite ^/images/(.*)$ {PUBLIC}/static/images/$1 permanent;", result.stdout)
        self.assertIn(f"return 301 {PUBLIC}/static/favicon.ico;", result.stdout)
        self.assertNotIn("static-root", result.stdout)

    def test_the_same_static_url_as_the_settings(self):
        loaded = load_cloud_run_settings(self, GS_CUSTOM_ENDPOINT="https://files.example.org")
        result = self.render(GS_BUCKET_NAME=BUCKET, GS_CUSTOM_ENDPOINT="https://files.example.org")
        self.assertIn(f"rewrite ^/static/(.*)$ {loaded['static']}$1 permanent;", result.stdout)

    def test_refuses_a_static_url_that_could_break_the_config(self):
        result = self.render(STATIC_URL="https://files.example.org/static/; return 200")
        self.assertNotEqual(result.returncode, 0)


@override_settings(MEDIA_MIGRATION_BASE_URL=PUBLIC)
class MediaMigrationViewTests(SimpleTestCase):
    def test_legacy_media_urls_redirect_to_the_bucket(self):
        response = self.client.get("/m/sponsors/logo.png/")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], f"{PUBLIC}/media/sponsors/logo.png")
