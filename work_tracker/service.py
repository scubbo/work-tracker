"""One local application owns storage, collectors, and scheduled jobs."""

from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import socketserver
import threading
import uuid

import work_activity as activity
from .client import MAX_MESSAGE
from .github import fetch_batch, open_inventory, review_status
from .pr_closing import PRCloser
from .scheduler import Scheduler, next_daily
from .storage import SQLiteStorage, now_iso


DEFAULT_CONFIG = {"archive_hour": 10, "archive_minute": 0, "refresh_seconds": 300, "automated_reviewers": []}


class Application:
    def __init__(self, root, storage=None, run=None, clock=None):
        self.root = Path(root).expanduser().resolve()
        activity.private_directory(self.root)
        self.root.chmod(0o700)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run = run or activity.run_json
        self.mutation_lock = threading.RLock()
        self.stopping = threading.Event()
        marker = activity.read_json(self.root/"application.json", {"version": 1})
        if marker != {"version": 1}:
            raise ValueError("Unsupported application marker; refusing legacy fallback")
        saved_config = activity.read_json(self.root/"app-config.json", {})
        if not isinstance(saved_config, dict):
            raise ValueError("Application configuration must be an object")
        self.config = {**DEFAULT_CONFIG, **saved_config}
        self._validate_config(self.config)
        self.storage = storage or SQLiteStorage(self.root/"tracker.sqlite3")
        self.storage.import_legacy(self.root/"prs.json")
        self.storage.recover_closes()
        self.closer = PRCloser(self.storage, self.run, self.mutation_lock)
        activity.write_json(self.root/"application.json", {"version": 1})
        self.scheduler = Scheduler(self.storage, {"archive": self.archive, "refresh_prs": self.refresh_prs},
                                   self.config, activity.read_json(self.root/"checkpoints.json", {}), self.clock)

    @staticmethod
    def _validate_config(config):
        if set(config) != set(DEFAULT_CONFIG):
            raise ValueError("Unknown application schedule configuration")
        for name, low, high in (("archive_hour", 0, 23), ("archive_minute", 0, 59), ("refresh_seconds", 60, 86400)):
            value = config[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        if not isinstance(config["automated_reviewers"], list) or any(
                not isinstance(value, str) for value in config["automated_reviewers"]):
            raise ValueError("automated_reviewers must be a list of account names")

    def mirror(self):
        # Compatibility export only. SQLite remains authoritative after migration.
        activity.write_json(self.root/"prs.json", self.storage.export_registry())

    def refresh_prs(self, parameters):
        errors, refreshed = [], 0
        inventory, warning = None, None
        try:
            inventory = open_inventory(self.run)
        except (activity.CollectionError, ValueError, KeyError, TypeError):
            warning = "Account-wide count unavailable. Tracked PR refresh is independent."
        tracked = self.storage.list_prs(state=None)
        targets = sorted(value['url'] for value in tracked)
        versions = {value['url']: value['observation_version'] for value in tracked}
        reviews_for = {value['url'] for value in tracked if value['github'].get('state', 'open').lower() == 'open'}
        for offset in range(0, len(targets), 20):
            if self.stopping.is_set():
                errors.append('Refresh interrupted during application shutdown')
                break
            batch = targets[offset:offset+20]
            try:
                observations = fetch_batch(batch, self.run, reviews_for)
            except (activity.CollectionError, ValueError, KeyError, TypeError):
                observations = {}
            for url in batch:
                observation = observations.get(url)
                if observation is None:
                    error = "PR refresh failed. Last observation is stale."
                    self.storage.observation_error(url, error, expected_version=versions[url])
                    errors.append(f"{url}: {error}")
                    continue
                detail, reviews = observation
                state, reason = review_status(detail, reviews, self.config['automated_reviewers'])
                with self.mutation_lock:
                    applied = self.storage.observe(url, detail, state, reason, expected_version=versions[url])
                refreshed += int(applied)
        if inventory:
            confirmed = sum(value['github'].get('state') == 'open'
                            and (value['github'].get('author') or '').casefold() == inventory['author'].casefold()
                            for value in self.storage.list_prs(state=None))
            inventory.update(tracked_open=confirmed, untracked_open_estimate=max(0, inventory['total_open']-confirmed),
                             observed_at=now_iso(), error=None,
                             estimate_note='Untracked count is derived from latest tracked observations, not a separate full inventory.')
            self.storage.save_inventory(inventory)
        elif warning:
            previous = self.storage.inventory() or {}
            self.storage.save_inventory({**previous, 'error': warning})
        with self.mutation_lock:
            self.mirror()
        return {"ok": not errors, "refreshed": refreshed, "tracked": len(targets), "errors": errors,
                "inventory_warning": warning,
                "error": "Some GitHub observations could not be refreshed" if errors else None}

    def archive(self, parameters):
        if set(parameters) - {"since", "until", "sources"}:
            raise ValueError("Unknown collection parameters")
        sources = parameters.get("sources") or ["opencode", "slack", "github"]
        if not isinstance(sources, list) or not sources or set(sources) - {"opencode", "slack", "github"}:
            raise ValueError("Invalid collection sources")
        versions = {p['url']: p['observation_version'] for p in self.storage.list_prs(state=None)}
        manifest = activity.collect(self.root, parameters.get("until") or activity.iso(self.clock()), sources,
                                    since=parameters.get("since"), run=self.run,
                                    registry=self.storage.export_registry(), persist_registry=False)
        archive = self.root/manifest["directory"]
        github = activity.read_json(archive/"github.json", {}).get("prs", [])
        hints = activity.read_json(self.root/"correlations.json", {"creation-evidence": [], "messages": []})
        with self.mutation_lock:
            self.storage.merge_archive(github, hints["creation-evidence"], hints["messages"], expected_versions=versions)
            self.mirror()
        ok = all(value["status"] == "ok" for value in manifest["sources"].values())
        return {"ok": ok, "manifest": manifest, "error": None if ok else "Collection has failed or partial sources"}

    def dispatch(self, method, params):
        if not isinstance(params, dict):
            raise ValueError("Parameters must be an object")
        if method == "status":
            return {"state_directory": str(self.root), "storage": "sqlite", "pid": os.getpid(),
                    "config": self.config, "prs": len(self.storage.list_prs(state=None)),
                    "jobs": self.storage.jobs(), "inventory": self.storage.inventory(), "github_writes": True,
                    "mcp_github_writes": False, "github_write_operations": ["confirmed_pr_close"]}
        if method == "inventory":
            return self.storage.inventory()
        if method == "catalog":
            if params:
                raise ValueError("Hierarchy suggestions do not accept write parameters")
            return self.storage.catalog()
        if method == "jobs":
            return self.storage.jobs()
        if method == "list_prs":
            if set(params) - {"project", "workstream", "search", "state", "limit", "offset"}:
                raise ValueError("Unknown PR filter")
            limit, offset = params.get("limit", 50), params.get("offset", 0)
            if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
                raise ValueError("Limit must be 1 to 200; offset must be nonnegative")
            filters = {key: value for key, value in params.items() if key not in ("limit", "offset")}
            for value in filters.values():
                if value is not None and not isinstance(value, str):
                    raise ValueError("PR filters must be strings or null")
            values = self.storage.list_prs(**filters)
            return {"total": len(values), "items": values[offset:offset+limit], "limit": limit, "offset": offset,
                    "inventory": self.storage.inventory()}
        if method == "get_pr":
            url = activity.pr_url(params.get("url", ""))
            values = self.storage.list_prs(state=None)
            return next((value for value in values if value["url"] == url), None)
        if method in ('close_pr', 'close_result'):
            result = self.closer.close(params) if method == 'close_pr' else self.closer.result(params)
            if result['status'] in ('succeeded', 'already_closed'):
                with self.mutation_lock:
                    self.mirror()
            return result
        if method == "intent":
            if set(params) - {"key", "kind", "payload", "source"}:
                raise ValueError("Unknown intent arguments")
            with self.mutation_lock:
                result = self.storage.apply_intent(params.get("key"), params.get("kind"),
                                                   params.get("payload"), params.get("source", {}))
                self.mirror()
                return result
        if method == "refresh":
            if params:
                raise ValueError("Refresh does not accept shell commands or external write actions")
            return self.scheduler.queue("refresh_prs")
        if method == "collect":
            if set(params) - {"since", "until", "sources"}:
                raise ValueError("Unknown collection parameters")
            return self.scheduler.queue("archive", params)
        if method == "configure":
            config = {**self.config, **params}
            self._validate_config(config)
            with self.scheduler.lock:
                self.config.update(config)
                activity.write_json(self.root/"app-config.json", self.config)
                for job in self.storage.jobs():
                    if job["status"] != "running":
                        name = job.pop("name")
                        job["next_run"] = next_daily(self.clock(), config["archive_hour"], config["archive_minute"]) if name == "archive" else now_iso()
                        self.storage.save_job(name, job)
                self.scheduler.wake.set()
            return self.config
        if method == "legacy":
            return self.legacy(params)
        if method == "stop":
            self.stopping.set()
            return {"stopping": True}
        raise ValueError("Unknown local application operation")

    def legacy(self, params):
        action = params.get("action")
        if action == "collect":
            return self.dispatch("collect", {key: params[key] for key in ("since", "until", "sources") if params.get(key) is not None})
        registry = self.storage.export_registry()
        if action == "status":
            return {"state_directory": str(self.root), "checkpoints": activity.read_json(self.root/"checkpoints.json", {}),
                    "prs": len(registry), "projects": activity.count_projects(list(registry.values())), "storage": "sqlite"}
        if action == "snapshots":
            return activity.snapshot_index(self.root, params["since"], params["until"])
        if action != "pr":
            raise ValueError("Unknown legacy command")
        command = params.get("pr_action")
        if command in ("list", "count"):
            values = activity.filter_prs(registry, project=params.get("project"), state=params.get("state"),
                                        created_since=params.get("created_since"), created_before=params.get("created_before"))
            return values if command == "list" else (activity.count_projects(values) if params.get("by_project") else {"count": len(values)})
        url = activity.pr_url(params["url"])
        if command == "show":
            if url not in registry:
                raise ValueError("PR is not registered")
            return registry[url]
        payload = {"url": url}
        if command == "set":
            for name in ("project", "description"):
                if params.get(name) is not None:
                    payload[name] = params[name]
            if params.get("source_session"):
                info = activity.api("session.get", self.run, sessionID=params["source_session"])["data"]
                payload["source_session"] = {**activity.session_reference(info), "method": "manual"}
            if params.get("clear_source_session"):
                payload["clear_source_session"] = True
            kind = "register_pr"
        elif command in ("thread-add", "thread-remove"):
            activity.slack_url(params["thread"])
            if url not in registry:
                self.dispatch("intent", {"key": uuid.uuid4().hex, "kind": "register_pr", "payload": payload, "source": {"client": "legacy-cli"}})
            payload["thread"] = params["thread"]
            if params.get("description") is not None:
                payload["description"] = params["description"]
            kind = "attach_slack" if command == "thread-add" else "remove_slack"
        else:
            raise ValueError("Unknown legacy PR command")
        self.dispatch("intent", {"key": uuid.uuid4().hex, "kind": kind, "payload": payload, "source": {"client": "legacy-cli"}})
        return self.storage.export_registry()[url]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            data = self.rfile.readline(MAX_MESSAGE + 1)
            if len(data) > MAX_MESSAGE or not data.endswith(b"\n"):
                raise ValueError("Invalid or oversized request")
            request = json.loads(data)
            if not isinstance(request, dict) or set(request) - {"method", "params"}:
                raise ValueError("Invalid request envelope")
            result = self.server.application.dispatch(request.get("method"), request.get("params", {}))
            response = {"ok": True, "data": result}
        except (ValueError, KeyError, TypeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            response = {"ok": False, "error": "Application operation failed; inspect local service health"}
        try:
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(root, jobs=True):
    root = Path(root).expanduser().resolve()
    activity.private_directory(root)
    root.chmod(0o700)
    os.umask(0o077)
    lock = open(root/".lock", "a")
    os.chmod(root/".lock", 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("A local application or legacy collection already owns this datastore")
    path = root/"app.sock"
    if len(os.fsencode(path)) >= 104:
        lock.close()
        raise ValueError("State path is too long for a private Unix socket; choose a shorter --state-dir")
    try:
        if path.exists() or path.is_symlink():
            if not path.is_socket():
                raise ValueError("Refusing to replace a non-socket application endpoint")
            path.unlink()
        application = Application(root)
        with Server(str(path), Handler) as server:
            path.chmod(0o600)
            server.application = application
            server.timeout = 0.5
            def stop(signum, frame):
                application.stopping.set()
            previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            if jobs:
                application.scheduler.start()
            try:
                while not application.stopping.is_set():
                    server.handle_request()
                    if jobs and not application.scheduler.thread.is_alive():
                        raise RuntimeError("Scheduler stopped unexpectedly; restart the application")
            finally:
                application.scheduler.stop()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    finally:
        if path.is_socket():
            path.unlink()
        lock.close()
