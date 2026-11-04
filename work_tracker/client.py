"""Small local RPC client. No client opens the datastore or starts scheduled jobs."""

import json
import os
from pathlib import Path
import socket
import time


MAX_MESSAGE = 16 * 1024 * 1024


def default_state():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state"))) / "work-activity"


def request(root, method, params=None):
    path = Path(root).expanduser().resolve() / "app.sock"
    message = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode() + b"\n"
    if len(message) > MAX_MESSAGE:
        raise ValueError("Request is too large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(path))
            connection.sendall(message)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_MESSAGE + 1)
        if not line.endswith(b"\n") or len(line) > MAX_MESSAGE:
            raise ValueError("Invalid or oversized application response")
        result = json.loads(line)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Local application unavailable at {path}; start work-tracker serve or load its service") from exc
    if not result.get("ok"):
        raise ValueError(result.get("error", "Application rejected the request"))
    return result["data"]


def wait_for_job(root, queued, timeout=900):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = next(value for value in request(root, "jobs") if value["name"] == queued["name"])
        if job.get("completed_seq", 0) >= queued["wait_for"]:
            if job.get("last_error"):
                raise RuntimeError(job["last_error"])
            return job.get("last_result")
        time.sleep(0.2)
    raise RuntimeError("Job is still running; inspect work-tracker jobs instead of resubmitting it")
