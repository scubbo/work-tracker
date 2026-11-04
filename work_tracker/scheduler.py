"""Application-owned durable jobs. No OS calendar timers are used."""

from datetime import datetime, time, timedelta, timezone
import threading

import work_activity as activity
from .storage import now_iso


def daily_at(now, hour=10, minute=0):
    """Resolve each local calendar date separately, including DST transitions."""
    local = now.astimezone()
    return datetime.combine(local.date(), time(hour, minute)).astimezone()


def zone_key(now):
    local = now.astimezone()
    return f"{local.tzname()}:{local.utcoffset()}"


def next_daily(now, hour=10, minute=0):
    due = daily_at(now, hour, minute)
    if due <= now:
        due = datetime.combine(now.astimezone().date() + timedelta(days=1), time(hour, minute)).astimezone()
    return activity.iso(due)


class Scheduler:
    def __init__(self, storage, callbacks, config, checkpoints=None, clock=None):
        self.storage = storage
        self.callbacks = callbacks
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        now = self.clock()
        jobs = {job["name"]: job for job in storage.jobs()}
        today = daily_at(now, config["archive_hour"], config["archive_minute"])
        recent = [activity.parse_time(value) for value in (checkpoints or {}).values()]
        daily_due = next_daily(now, config["archive_hour"], config["archive_minute"]) if recent and max(recent) >= today else activity.iso(today)
        for name, due in (("archive", daily_due), ("refresh_prs", activity.iso(now))):
            if name not in jobs:
                storage.save_job(name, {"status": "idle", "next_run": due, "attempts": 0, "parameters": {},
                                        "zone": zone_key(now),
                                        "last_finished": activity.iso(max(recent)) if name == "archive" and recent else None})
            elif jobs[name]["status"] == "running":
                value = dict(jobs[name])
                value.pop("name", None)
                value.update(status="idle", next_run=activity.iso(now), last_error="Previous run was interrupted")
                storage.save_job(name, value)

    def queue(self, name, parameters=None):
        if name not in self.callbacks:
            raise ValueError("Unknown job")
        with self.lock:
            value = next(job for job in self.storage.jobs() if job["name"] == name)
            value.pop("name")
            if value["status"] == "running":
                if parameters:
                    raise ValueError("A job is already running; retry this explicit collection window afterward")
                return {"name": name, "status": "running", "wait_for": value.get("run_seq", 1)}
            if value["status"] == "queued" and value.get("parameters", {}) != (parameters or {}):
                raise ValueError("A different job request is already queued")
            value.update(status="queued", next_run=activity.iso(self.clock()), parameters=parameters or {})
            self.storage.save_job(name, value)
            self.wake.set()
            return {"name": name, "status": "queued", "wait_for": value.get("run_seq", 0) + 1}

    def step(self):
        now = self.clock()
        for job in self.storage.jobs():
            if self.stopping.is_set():
                return
            with self.lock:
                job = next(value for value in self.storage.jobs() if value["name"] == job["name"])
                name = job.pop("name")
                if name == "archive" and job["status"] == "idle" and not job.get("last_error") and job.get("zone") != zone_key(now):
                    today = daily_at(now, self.config["archive_hour"], self.config["archive_minute"])
                    completed = activity.parse_time(job["last_finished"]) if job.get("last_finished") else None
                    job["next_run"] = next_daily(now, self.config["archive_hour"], self.config["archive_minute"]) if completed and completed >= today else activity.iso(today)
                    job["zone"] = zone_key(now)
                    self.storage.save_job(name, job)
                if job["status"] == "running" or activity.parse_time(job["next_run"]) > now:
                    continue
                job.update(status="running", last_started=activity.iso(now), run_seq=job.get("run_seq", 0) + 1)
                self.storage.save_job(name, job)
            error = None
            result = None
            try:
                result = self.callbacks[name](job.get("parameters") or {})
                if isinstance(result, dict) and result.get("ok") is False:
                    error = result.get("error") or "Job completed with partial failures"
            except Exception as exc:
                # Do not log arbitrary CLI output, payloads, or private response bodies.
                error = f"Job failed: {type(exc).__name__}; inspect source authentication and job status"
            finished = self.clock()
            with self.lock:
                attempts = job.get("attempts", 0) + 1 if error else 0
                if error:
                    due = activity.iso(finished + timedelta(seconds=min(60 * 2 ** min(attempts - 1, 6), 3600)))
                elif name == "archive":
                    due = next_daily(finished, self.config["archive_hour"], self.config["archive_minute"])
                else:
                    due = activity.iso(finished + timedelta(seconds=self.config["refresh_seconds"]))
                job.update(status="idle", next_run=due, attempts=attempts,
                           last_finished=activity.iso(finished), completed_seq=job["run_seq"],
                           last_error=error, last_result=result, parameters={})
                self.storage.save_job(name, job)

    def start(self):
        if self.thread:
            raise RuntimeError("Scheduler already started")
        self.thread = threading.Thread(target=self._loop, name="tracker-jobs", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            self.wake.clear()
            self.step()
            self.wake.wait(2)

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
