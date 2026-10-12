"""Run management commands from Google Cloud Tasks and Cloud Scheduler.

Both call ``POST /_tasks/run-command`` with a JSON body ``{"command": ..., "args": [...],
"kwargs": {...}}`` and an OIDC token minted for the invoker service account. The handler
checks the token, runs only the commands in ``settings.CLOUD_TASKS_ALLOWED_COMMANDS`` and
answers when the command has finished, as Cloud Run takes the CPU away once it answers.

Status codes tell the caller what to do: 2xx is done, anything else is tried again. A
request that a retry cannot fix (a command not on the allow list, a body that is not a
command) is logged and answered with 200, so the queue drops it instead of retrying it.
"""

import datetime as dt
import hashlib
import json
import logging

from django.conf import settings
from django.core import management
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

RUN_COMMAND_PATH = "/_tasks/run-command"

# How long a delivered task or scheduled run is remembered, to skip duplicate deliveries.
# Cloud Tasks keeps a task's name for up to 24 hours after it ran.
DEDUPLICATION_SECONDS = 24 * 60 * 60


class InvalidRequestError(ValueError):
    """The request can never run, however often it is retried."""


def verify_caller(request):
    """Return the verified service account email of the caller, or None.

    The token must be signed by Google, made out for ``CLOUD_TASKS_AUDIENCE`` (the service
    URL), and belong to one of ``CLOUD_TASKS_INVOKER_EMAILS`` with a verified email.
    """
    audience = settings.CLOUD_TASKS_AUDIENCE
    invokers = settings.CLOUD_TASKS_INVOKER_EMAILS
    if not audience or not invokers:
        return None
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not token:
        return None

    # The Google libraries come with the prod extra, so they are imported only when used.
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)
    except ValueError:
        logger.warning("Refused a task: the token did not verify")
        return None
    email = claims.get("email")
    if email not in invokers or claims.get("email_verified") is not True:
        logger.warning("Refused a task from %s: not an invoker", email)
        return None
    return email


def parse_command(body):
    """Return (command, args, kwargs) from a request body, or raise InvalidRequestError."""
    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        msg = "the body is not JSON"
        raise InvalidRequestError(msg) from exc
    if not isinstance(payload, dict):
        msg = "the body is not a JSON object"
        raise InvalidRequestError(msg)
    command = payload.get("command")
    args = payload.get("args", [])
    kwargs = payload.get("kwargs", {})
    if command not in settings.CLOUD_TASKS_ALLOWED_COMMANDS:
        msg = f"{command!r} is not an allowed command"
        raise InvalidRequestError(msg)
    if not isinstance(args, list) or not all(isinstance(a, str | int | float) for a in args):
        msg = "args must be a list of strings and numbers"
        raise InvalidRequestError(msg)
    if not isinstance(kwargs, dict):
        msg = "kwargs must be an object"
        raise InvalidRequestError(msg)
    return command, [str(a) for a in args], kwargs


def delivery_key(request):
    """Return a key naming this task or scheduled run, the same on each redelivery."""
    task = request.headers.get("X-CloudTasks-TaskName")
    if task:
        name = f"{request.headers.get('X-CloudTasks-QueueName', '')}/{task}"
    else:
        job = request.headers.get("X-CloudScheduler-JobName")
        run = request.headers.get("X-CloudScheduler-ScheduleTime")
        if not (job and run):
            return None
        name = f"scheduler/{job}/{run}"
    return "cloudtasks:" + hashlib.sha256(name.encode()).hexdigest()


@csrf_exempt
def run_command(request):
    """Run one allowed management command for Cloud Tasks or Cloud Scheduler."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    caller = verify_caller(request)
    if caller is None:
        return HttpResponse("unauthorized", status=401, content_type="text/plain")

    try:
        command, args, kwargs = parse_command(request.body)
    except InvalidRequestError as exc:
        logger.error("Dropped a task from %s: %s", caller, exc)  # noqa: TRY400 - not an exception to trace
        return HttpResponse(f"dropped: {exc}", content_type="text/plain")

    key = delivery_key(request)
    if key and not cache.add(key, "running", DEDUPLICATION_SECONDS):
        logger.info("Skipped a duplicate delivery of %s", command)
        return HttpResponse("duplicate", content_type="text/plain")

    try:
        management.call_command(command, *args, **kwargs)
    except Exception:
        logger.exception("Command %s failed; the caller will retry it", command)
        if key:
            cache.delete(key)
        return HttpResponse("failed", status=500, content_type="text/plain")
    return HttpResponse("done", content_type="text/plain")


def enqueue_command(command_name, *args, delay=None, task_id=None, **kwargs):
    """Ask Cloud Tasks to run a management command later through ``run_command``.

    ``delay`` is a ``timedelta`` (up to 30 days). ``task_id`` names the task, so that the
    same ``task_id`` is enqueued once even if this is called twice. Returns the task name.
    """
    if command_name not in settings.CLOUD_TASKS_ALLOWED_COMMANDS:
        msg = f"{command_name!r} is not in CLOUD_TASKS_ALLOWED_COMMANDS"
        raise ValueError(msg)
    required = ("GOOGLE_CLOUD_PROJECT", "CLOUD_TASKS_LOCATION", "CLOUD_TASKS_QUEUE", "CLOUD_TASKS_AUDIENCE")
    missing = [name for name in required if not getattr(settings, name)]
    if missing or not settings.CLOUD_TASKS_INVOKER_EMAILS:
        msg = f"Cloud Tasks is not configured: set {', '.join(missing) or 'CLOUD_TASKS_INVOKER_EMAILS'}"
        raise ValueError(msg)

    from google.api_core.exceptions import AlreadyExists
    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(settings.GOOGLE_CLOUD_PROJECT, settings.CLOUD_TASKS_LOCATION, settings.CLOUD_TASKS_QUEUE)
    body = {"command": command_name, "args": [str(a) for a in args], "kwargs": kwargs}
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": settings.CLOUD_TASKS_AUDIENCE.rstrip("/") + RUN_COMMAND_PATH,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body).encode(),
            "oidc_token": {
                "service_account_email": settings.CLOUD_TASKS_INVOKER_EMAILS[0],
                "audience": settings.CLOUD_TASKS_AUDIENCE,
            },
        },
        "dispatch_deadline": {"seconds": settings.CLOUD_TASKS_DISPATCH_DEADLINE},
    }
    if task_id:
        digest = hashlib.sha256(f"{command_name}:{task_id}".encode()).hexdigest()[:32]
        task["name"] = f"{parent}/tasks/{digest}"
    if delay:
        when = timestamp_pb2.Timestamp()
        when.FromDatetime(dt.datetime.now(tz=dt.UTC) + delay)
        task["schedule_time"] = when

    try:
        return client.create_task(parent=parent, task=task).name
    except AlreadyExists:
        logger.info("Task %s was enqueued already", task["name"])
        return task["name"]
