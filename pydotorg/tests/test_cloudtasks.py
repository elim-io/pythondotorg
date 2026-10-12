import datetime as dt
import json
import logging
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from pydotorg import cloudtasks

AUDIENCE = "https://pythondotorg-abc123-ue.a.run.app"
INVOKER = "tasks-invoker@example-project.iam.gserviceaccount.com"
VERIFY = "google.oauth2.id_token.verify_oauth2_token"
TOKEN = "a-google-signed-id-token"  # verify_oauth2_token is mocked in every test

TASKS_SETTINGS = {
    "GOOGLE_CLOUD_PROJECT": "example-project",
    "CLOUD_TASKS_LOCATION": "us-east4",
    "CLOUD_TASKS_QUEUE": "commands",
    "CLOUD_TASKS_AUDIENCE": AUDIENCE,
    "CLOUD_TASKS_INVOKER_EMAILS": [INVOKER],
    "CLOUD_TASKS_ALLOWED_COMMANDS": ["expire_jobs", "check_sponsorship_assets_due_date"],
}


def claims(email=INVOKER, verified=True):
    return {"email": email, "email_verified": verified, "aud": AUDIENCE}


@override_settings(**TASKS_SETTINGS)
class RunCommandTests(TestCase):
    url = cloudtasks.RUN_COMMAND_PATH

    def setUp(self):
        cache.clear()
        # apps/boxes/tests.py turns logging off for the whole run; these tests check the logs.
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.NOTSET)

    def post(self, body, token=TOKEN, **headers):
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(self.url, json.dumps(body), content_type="application/json", **headers)

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_runs_an_allowed_command(self, call_command, verify):
        body = {"command": "check_sponsorship_assets_due_date", "args": [7], "kwargs": {"no_input": True}}
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"done")
        call_command.assert_called_once_with("check_sponsorship_assets_due_date", "7", no_input=True)
        self.assertEqual(verify.call_args.kwargs["audience"], AUDIENCE)

    @mock.patch(VERIFY)
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_refuses_a_request_without_a_token(self, call_command, verify):
        response = self.post({"command": "expire_jobs"}, token=None)
        self.assertEqual(response.status_code, 401)
        verify.assert_not_called()
        call_command.assert_not_called()

    @mock.patch(VERIFY, side_effect=ValueError("Token has wrong audience"))
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_refuses_a_token_that_does_not_verify(self, call_command, verify):
        response = self.post({"command": "expire_jobs"})
        self.assertEqual(response.status_code, 401)
        call_command.assert_not_called()

    @mock.patch(VERIFY, return_value=claims(email="someone-else@example-project.iam.gserviceaccount.com"))
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_refuses_a_token_of_another_account(self, call_command, verify):
        response = self.post({"command": "expire_jobs"})
        self.assertEqual(response.status_code, 401)
        call_command.assert_not_called()

    @mock.patch(VERIFY, return_value=claims(verified=False))
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_refuses_an_unverified_email(self, call_command, verify):
        response = self.post({"command": "expire_jobs"})
        self.assertEqual(response.status_code, 401)
        call_command.assert_not_called()

    @override_settings(CLOUD_TASKS_AUDIENCE="", CLOUD_TASKS_INVOKER_EMAILS=[])
    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_refuses_everything_when_not_configured(self, call_command, verify):
        response = self.post({"command": "expire_jobs"})
        self.assertEqual(response.status_code, 401)
        call_command.assert_not_called()

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_drops_a_command_not_on_the_allow_list(self, call_command, verify):
        with self.assertLogs("pydotorg.cloudtasks", "ERROR"):
            response = self.post({"command": "flush", "kwargs": {"interactive": False}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"dropped"))
        call_command.assert_not_called()

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_drops_a_body_that_is_not_a_command(self, call_command, verify):
        for body in (
            ["expire_jobs"],
            {"command": "expire_jobs", "args": "x"},
            {"command": "expire_jobs", "kwargs": []},
        ):
            with self.subTest(body=body), self.assertLogs("pydotorg.cloudtasks", "ERROR"):
                response = self.post(body)
                self.assertEqual(response.status_code, 200)
        call_command.assert_not_called()

    @mock.patch(VERIFY, return_value=claims())
    def test_only_post(self, verify):
        response = self.client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, 405)

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_skips_a_duplicate_delivery_of_a_task(self, call_command, verify):
        headers = {"HTTP_X_CLOUDTASKS_QUEUENAME": "commands", "HTTP_X_CLOUDTASKS_TASKNAME": "abc"}
        first = self.post({"command": "expire_jobs"}, **headers)
        second = self.post({"command": "expire_jobs"}, **headers)
        self.assertEqual(first.content, b"done")
        self.assertEqual(second.content, b"duplicate")
        call_command.assert_called_once()

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command")
    def test_runs_each_scheduled_run_once(self, call_command, verify):
        job = {"HTTP_X_CLOUDSCHEDULER_JOBNAME": "expire-jobs"}
        self.post({"command": "expire_jobs"}, HTTP_X_CLOUDSCHEDULER_SCHEDULETIME="2026-10-12T00:00:00Z", **job)
        self.post({"command": "expire_jobs"}, HTTP_X_CLOUDSCHEDULER_SCHEDULETIME="2026-10-12T00:00:00Z", **job)
        self.post({"command": "expire_jobs"}, HTTP_X_CLOUDSCHEDULER_SCHEDULETIME="2026-10-13T00:00:00Z", **job)
        self.assertEqual(call_command.call_count, 2)

    @mock.patch(VERIFY, return_value=claims())
    @mock.patch("pydotorg.cloudtasks.management.call_command", side_effect=[RuntimeError("SMTP down"), None])
    def test_a_failed_command_asks_for_a_retry_and_can_run_again(self, call_command, verify):
        headers = {"HTTP_X_CLOUDTASKS_QUEUENAME": "commands", "HTTP_X_CLOUDTASKS_TASKNAME": "abc"}
        with self.assertLogs("pydotorg.cloudtasks", "ERROR"):
            failed = self.post({"command": "expire_jobs"}, **headers)
        retried = self.post({"command": "expire_jobs"}, **headers)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(retried.content, b"done")
        self.assertEqual(call_command.call_count, 2)

    @mock.patch(VERIFY, return_value=claims())
    def test_runs_the_real_command(self, verify):
        response = self.post({"command": "expire_jobs"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"done")


@override_settings(**TASKS_SETTINGS)
class EnqueueCommandTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("google.cloud.tasks_v2.CloudTasksClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.client_class.return_value
        self.client.queue_path.return_value = "projects/example-project/locations/us-east4/queues/commands"
        self.client.create_task.return_value.name = "the-task"

    def created_task(self):
        return self.client.create_task.call_args.kwargs["task"]

    def test_creates_an_http_task_with_an_oidc_token(self):
        name = cloudtasks.enqueue_command("check_sponsorship_assets_due_date", 7, no_input=True)
        self.assertEqual(name, "the-task")
        self.client.queue_path.assert_called_once_with("example-project", "us-east4", "commands")
        request = self.created_task()["http_request"]
        self.assertEqual(request["url"], f"{AUDIENCE}/_tasks/run-command")
        self.assertEqual(request["oidc_token"], {"service_account_email": INVOKER, "audience": AUDIENCE})
        self.assertEqual(
            json.loads(request["body"]),
            {"command": "check_sponsorship_assets_due_date", "args": ["7"], "kwargs": {"no_input": True}},
        )
        self.assertNotIn("name", self.created_task())

    def test_names_the_task_so_it_is_enqueued_once(self):
        cloudtasks.enqueue_command("expire_jobs", task_id="2026-10-12")
        first = self.created_task()["name"]
        cloudtasks.enqueue_command("expire_jobs", task_id="2026-10-12")
        self.assertEqual(self.created_task()["name"], first)
        self.assertTrue(first.startswith("projects/example-project/locations/us-east4/queues/commands/tasks/"))

    def test_an_existing_task_is_not_an_error(self):
        from google.api_core.exceptions import AlreadyExists

        self.client.create_task.side_effect = AlreadyExists("exists")
        name = cloudtasks.enqueue_command("expire_jobs", task_id="2026-10-12")
        self.assertIn("/tasks/", name)

    def test_delays_the_task(self):
        before = dt.datetime.now(tz=dt.UTC)
        cloudtasks.enqueue_command("expire_jobs", delay=dt.timedelta(hours=1))
        when = self.created_task()["schedule_time"].ToDatetime(tzinfo=dt.UTC)
        self.assertGreaterEqual(when, before + dt.timedelta(hours=1))
        self.assertLess(when, before + dt.timedelta(hours=1, minutes=1))

    def test_refuses_a_command_not_on_the_allow_list(self):
        with self.assertRaises(ValueError):
            cloudtasks.enqueue_command("flush")
        self.client.create_task.assert_not_called()

    @override_settings(CLOUD_TASKS_QUEUE="")
    def test_refuses_to_enqueue_when_not_configured(self):
        with self.assertRaises(ValueError):
            cloudtasks.enqueue_command("expire_jobs")
        self.client.create_task.assert_not_called()
