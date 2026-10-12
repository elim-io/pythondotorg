"""Gunicorn settings for Google Cloud Run.

nginx listens on $PORT (see bin/start-cloudrun) and proxies to this socket, as on Cabotage.
"""

import os
from pathlib import Path

SOCKET = "/tmp/gunicorn.sock"  # noqa: S108 - private to the container; nginx's upstream

bind = f"unix:{SOCKET}"
backlog = 1024
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
preload_app = True
max_requests = 2048
max_requests_jitter = 128

worker_connections = 1000
timeout = 60
keepalive = 2

errorlog = "-"
loglevel = "info"
accesslog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

control_socket_disable = True


def when_ready(server):
    """Tell bin/start-nginx that the app is up, so nginx starts taking traffic."""
    Path("/tmp/app-initialized").touch()  # noqa: S108 - the flag bin/start-nginx waits for
