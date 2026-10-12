.. _scheduled-commands:

Scheduled and background commands on Cloud Run
==============================================

On Cloud Run there is no Celery worker and no beat scheduler. Management commands
run as HTTP requests to the web service:

- **Cloud Scheduler** replaces beat: one job per schedule, calling
  ``POST /_tasks/run-command`` on a cron.
- **Cloud Tasks** replaces ``.delay()``: code that wants a command run later calls
  ``pydotorg.cloudtasks.enqueue_command()``, which creates a task in a queue, and the
  queue calls the same route.

Both send an OIDC token for the invoker service account. The handler
(``pydotorg/cloudtasks.py``) checks that Google signed it, that it is made out for the
service URL and that it belongs to an invoker, and runs only the commands in
``CLOUD_TASKS_ALLOWED_COMMANDS`` (``pydotorg/settings/base.py``). The body is JSON::

    {"command": "check_sponsorship_assets_due_date", "args": ["7"], "kwargs": {"no_input": true}}

It answers when the command has finished: 200 when it is done, or when the request can
never run (a command not on the list, a body that is not a command; these are logged),
401 for a missing or wrong token, and 500 when the command failed, so the caller retries.
A redelivered task or scheduled run (same task name, or same job and schedule time) is
skipped for 24 hours.

Settings
--------

All read from the environment; none has a default that names a project.

``GOOGLE_CLOUD_PROJECT``
   The project of the queue.
``CLOUD_TASKS_LOCATION``
   The queue's region, for example ``us-east4``.
``CLOUD_TASKS_QUEUE``
   The queue's name.
``CLOUD_TASKS_AUDIENCE``
   The service's ``run.app`` URL. Tokens must be made out for it, and tasks are sent to
   it. Its host must also be in ``ALLOWED_HOSTS``.
``CLOUD_TASKS_INVOKER_EMAILS``
   Comma-separated service accounts allowed to call the route. The first one is named
   on the tasks ``enqueue_command()`` creates.
``CLOUD_TASKS_DISPATCH_DEADLINE``
   Seconds a task may run, 600 by default; keep it under the service's request timeout.

Cloud Scheduler jobs that replace beat
--------------------------------------

The beat schedule lives in the database (``django_celery_beat``), not in the code, so
read it before the switch; the tables stay after the app is removed::

    SELECT t.name, t.task, t.args, t.kwargs, t.enabled,
           c.minute, c.hour, c.day_of_month, c.month_of_year, c.day_of_week, c.timezone,
           i.every, i.period
    FROM django_celery_beat_periodictask t
    LEFT JOIN django_celery_beat_crontabschedule c ON c.id = t.crontab_id
    LEFT JOIN django_celery_beat_intervalschedule i ON i.id = t.interval_id
    ORDER BY t.name;

Each enabled row that runs ``pydotorg.celery.run_management_command`` becomes one
Cloud Scheduler job with its first argument as ``command``. The commands meant to run on
a schedule, and the jobs proposed for them:

.. list-table::
   :header-rows: 1

   * - Job
     - Schedule (UTC)
     - Body
     - Repeat-safe
   * - ``expire-jobs``
     - ``0 * * * *``
     - ``{"command": "expire_jobs"}``
     - Yes: one ``UPDATE`` of expired jobs.
   * - ``update-blogs``
     - ``*/30 * * * *``
     - ``{"command": "update_blogs"}``
     - Yes: ``update_or_create`` per entry.
   * - ``import-ics-calendars``
     - ``0 */6 * * *``
     - ``{"command": "import_ics_calendars"}``
     - Yes: re-imports the calendars.
   * - ``jobs-monthly-report``
     - ``0 9 27 * *``
     - ``{"command": "jobs_monthly_report"}``
     - No: sends one email; it sends only on the 27th.
   * - ``sponsorship-assets-due``
     - ``0 10 * * *``
     - ``{"command": "check_sponsorship_assets_due_date", "args": ["7"], "kwargs": {"no_input": true}}``
     - No: emails sponsors.
   * - ``clear-sessions``
     - ``0 3 * * *``
     - ``{"command": "clearsessions"}``
     - Yes.
   * - ``update-search-index``
     - ``0 * * * *``
     - ``{"command": "update_index", "kwargs": {"age": 2}}``
     - Yes.

Use the database's schedule where it differs from this table. For a job that is not
repeat-safe, keep Cloud Scheduler's default of no retries; the handler already skips a
second delivery of the same run. A job, once the queue, service accounts and service
exist::

    gcloud scheduler jobs create http expire-jobs \
      --location="$REGION" --schedule="0 * * * *" --time-zone=Etc/UTC \
      --uri="$SERVICE_URL/_tasks/run-command" --http-method=POST \
      --headers=Content-Type=application/json \
      --message-body='{"command": "expire_jobs"}' \
      --oidc-service-account-email="$INVOKER" --oidc-token-audience="$SERVICE_URL" \
      --attempt-deadline=600s

Running a command later from code
---------------------------------

.. code-block:: python

    import datetime as dt

    from pydotorg.cloudtasks import enqueue_command

    enqueue_command("check_sponsorship_assets_due_date", "7", no_input=True,
                    delay=dt.timedelta(hours=1), task_id=f"assets-{date.today()}")

``task_id`` names the task: the same ``task_id`` is enqueued once even if the code runs
twice. The command must be in ``CLOUD_TASKS_ALLOWED_COMMANDS``.
