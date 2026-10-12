import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

BASE = Path(settings.BASE)


class HealthzTests(TestCase):
    def test_ok_when_database_answers(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_unavailable_when_database_does_not_answer(self):
        with mock.patch("pydotorg.middleware.connection.cursor", side_effect=OperationalError("down")):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)

    @override_settings(ALLOWED_HOSTS=["www.python.org"], SECURE_SSL_REDIRECT=True)
    def test_answers_probes_from_any_host_over_plain_http(self):
        """Cloud Run's probes use the container's address and plain HTTP."""
        response = self.client.get("/healthz", HTTP_HOST="169.254.1.1:8080")
        self.assertEqual(response.status_code, 200)

    def test_other_paths_are_not_affected(self):
        response = self.client.get("/healthz/extra")
        self.assertNotEqual(response.content, b"ok")


class CloudRunImageTests(SimpleTestCase):
    def render_nginx_conf(self, port):
        return subprocess.run(  # noqa: S603 - our own script
            [BASE / "bin" / "render-nginx-conf"],
            env={**os.environ, "PORT": port},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_nginx_listens_on_port(self):
        result = self.render_nginx_conf("9123")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\n\s*listen 9123;")
        self.assertNotIn("cabotage", result.stdout)

    def test_nginx_refuses_a_port_that_is_not_a_number(self):
        result = self.render_nginx_conf("80; evil")
        self.assertNotEqual(result.returncode, 0)

    def test_nginx_proxies_to_gunicorn_socket(self):
        conf = runpy.run_path(str(BASE / "gunicorn.cloudrun.conf.py"))
        upstream = re.search(r"server (unix:\S+) ", self.render_nginx_conf("8080").stdout)
        self.assertIsNotNone(upstream)
        self.assertEqual(conf["bind"], upstream.group(1))

    def test_nginx_accepts_the_config(self):
        nginx = shutil.which("nginx")
        if nginx is None:
            self.skipTest("nginx is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "nginx.conf").write_text(self.render_nginx_conf("8080").stdout)
            shutil.copy(BASE / "config" / "mime.types", tmp)
            Path(tmp, "logs", "nginx").mkdir(parents=True)
            result = subprocess.run(  # noqa: S603 - the nginx found on PATH, checking our config
                [nginx, "-t", "-p", tmp, "-c", f"{tmp}/nginx.conf"], capture_output=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_image_starts_on_cloud_run_settings(self):
        dockerfile = (BASE / "docker" / "Dockerfile.cloudrun").read_text()
        self.assertIn("ENV DJANGO_SETTINGS_MODULE=pydotorg.settings.cloudrun", dockerfile)
        self.assertIn('CMD ["bin/start-cloudrun"]', dockerfile)


CLOUD_RUN_ENV = {
    "DJANGO_SETTINGS_MODULE": "pydotorg.settings.cloudrun",
    "SECRET_KEY": "test",
    "ALLOWED_HOSTS": "www.python.org",
    "SEARCHBOX_SSL_URL": "https://search.invalid",
    "EMAIL_HOST": "smtp.invalid",
    "EMAIL_HOST_USER": "user",
    "EMAIL_HOST_PASSWORD": "password",
    "EMAIL_PORT": "587",
    "DEFAULT_FROM_EMAIL": "noreply@python.org",
    "FASTLY_API_KEY": "test",
    "SENTRY_DSN": "",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_STORAGE_BUCKET_NAME": "test",
}


class CloudRunSettingsTests(SimpleTestCase):
    def run_with_cloud_run_settings(self, code, **env):
        return subprocess.run(  # noqa: S603 - code from these tests
            [sys.executable, "-c", f"import django; django.setup(); {code}"],
            cwd=BASE,
            env={**os.environ, **CLOUD_RUN_ENV, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_settings_load_from_the_environment(self):
        result = self.run_with_cloud_run_settings(
            "from django.conf import settings; print(settings.ALLOWED_HOSTS, settings.SECURE_PROXY_SSL_HEADER)"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("['www.python.org'] ('HTTP_X_FORWARDED_PROTO', 'https')", result.stdout)

    def test_health_check_runs_first(self):
        result = self.run_with_cloud_run_settings(
            "import json; from django.conf import settings; print(json.dumps(settings.MIDDLEWARE))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        middleware = json.loads(result.stdout)
        self.assertLess(
            middleware.index("pydotorg.middleware.HealthCheck"),
            middleware.index("django.middleware.common.CommonMiddleware"),
        )
