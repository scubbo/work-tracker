"""Transactional storage boundary. Only the local application opens this store."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Protocol
import uuid

import work_activity as activity


UNCLASSIFIED = "Unclassified"


def now_iso():
    return activity.iso(datetime.now(timezone.utc))


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Storage(Protocol):
    def import_legacy(self, path: Path) -> int: ...
    def export_registry(self) -> dict: ...
    def apply_intent(self, key: str, kind: str, payload: dict, source: dict) -> dict: ...
    def list_prs(self, **filters) -> list: ...
    def catalog(self) -> dict: ...
    def observe(self, url: str, github: dict, review_state: str, reason: str, expected_version=None) -> bool: ...
    def observation_error(self, url: str, error: str, expected_version=None) -> None: ...
    def merge_archive(self, prs: list, evidence: list, messages: list, expected_versions=None) -> None: ...
    def begin_close(self, key: str, url: str, comment: str) -> tuple: ...
    def close_request(self, key: str) -> dict: ...
    def finish_close(self, key: str, status: str, message: str, phase: str) -> dict: ...
    def recover_closes(self) -> None: ...
    def jobs(self) -> list: ...
    def save_job(self, name: str, value: dict) -> None: ...
    def inventory(self) -> dict | None: ...
    def save_inventory(self, value: dict) -> None: ...


class SQLiteStorage:
    def __init__(self, path):
        self.path = Path(path)
        activity.private_directory(self.path.parent)
        self.path.parent.chmod(0o700)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError(f"Unsupported database schema {version}; refusing to change it")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS workstreams(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    name TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0, UNIQUE(project_id, name));
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, workstream_id TEXT NOT NULL REFERENCES workstreams(id),
                    title TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS prs(
                    url TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    metadata TEXT NOT NULL, github TEXT NOT NULL DEFAULT '{}',
                    review_state TEXT NOT NULL DEFAULT 'Awaiting review',
                    reason TEXT NOT NULL DEFAULT 'Not yet verified with GitHub',
                    observed_at TEXT, refresh_error TEXT);
                CREATE TABLE IF NOT EXISTS links(
                    owner_kind TEXT NOT NULL, owner_key TEXT NOT NULL, kind TEXT NOT NULL,
                    identity TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(owner_kind, owner_key, kind, identity));
                CREATE TABLE IF NOT EXISTS intents(
                    key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    source TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS untracked_cache(
                    url TEXT PRIMARY KEY, snapshot TEXT NOT NULL, cached_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pr_close_requests(
                    key TEXT PRIMARY KEY, url TEXT NOT NULL REFERENCES prs(url), comment TEXT NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL, phase TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(prs)')}
            for name, definition in (('state_changed_at', 'TEXT'), ('state_first_seen_at', 'TEXT'),
                                     ('observation_version', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE prs ADD COLUMN {name} {definition}')
            db.execute('PRAGMA user_version=3')
            self._workstream(db, UNCLASSIFIED, UNCLASSIFIED)
            db.commit()

    @contextmanager
    def _connection(self, read=False):
        db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if read:
            db.execute("BEGIN")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _workstream(self, db, project, workstream):
        for value in (project, workstream):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("Project and workstream names must be nonempty strings under 201 characters")
        db.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (uuid.uuid4().hex, project))
        pid = db.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO workstreams(id,project_id,name) VALUES(?,?,?)",
                   (uuid.uuid4().hex, pid, workstream))
        return db.execute("SELECT id FROM workstreams WHERE project_id=? AND name=?",
                          (pid, workstream)).fetchone()[0]

    def _ensure_pr(self, db, url, metadata=None):
        activity.pr_url(url)
        if db.execute("SELECT 1 FROM prs WHERE url=?", (url,)).fetchone():
            return
        cached = db.execute("SELECT snapshot FROM untracked_cache WHERE url=?", (url,)).fetchone()
        snapshot = json.loads(cached[0]) if cached else None
        if metadata is None and snapshot:
            metadata = {**json.loads(snapshot['pr']['metadata']), 'github': json.loads(snapshot['pr']['github'])}
        metadata = metadata or activity.new_pr(url)
        task_id = snapshot['task']['id'] if snapshot else uuid.uuid4().hex
        if db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            task_id = uuid.uuid4().hex
        ws = self._workstream(db, UNCLASSIFIED, UNCLASSIFIED)
        db.execute("INSERT INTO tasks(id,workstream_id,title) VALUES(?,?,?)",
                   (task_id, ws, metadata.get("github", {}).get("title") or url))
        github = metadata.get("github", {})
        curated = {key: value for key, value in metadata.items() if key != "github"}
        db.execute("INSERT INTO prs(url,task_id,metadata,github) VALUES(?,?,?,?)",
                   (url, task_id, encoded(curated), encoded(github)))
        for thread in metadata.get("slack-threads", []):
            self._link(db, url, "slack", thread["url"], thread)
        for session in metadata.get("agent-sessions", []):
            self._session(db, url, session)
        old = metadata.get("source-session")
        if old and old.get("directory") and old.get("id"):
            self._session(db, url, {"tool": "opencode", "path": old["directory"],
                                    "sessionId": old["id"], "machine": old.get("machine", "legacy-local")})
        if cached:
            db.execute("DELETE FROM untracked_cache WHERE url=?", (url,))

    def _link(self, db, url, kind, identity, payload):
        db.execute("INSERT INTO links VALUES('pr',?,?,?,?) "
                   "ON CONFLICT(owner_kind,owner_key,kind,identity) DO UPDATE SET payload=excluded.payload",
                   (url, kind, identity, encoded(payload)))

    def _session(self, db, url, session):
        if not isinstance(session, dict) or set(session) - {"tool", "path", "sessionId", "machine"}:
            raise ValueError("Session requires tool, path, sessionId, and machine only")
        if any(not isinstance(session.get(key), str) or not session[key].strip()
               for key in ("tool", "path", "sessionId", "machine")):
            raise ValueError("Session requires nonempty tool, path, sessionId, and machine")
        if not Path(session["path"]).is_absolute():
            raise ValueError("Session path must be absolute")
        identity = hashlib.sha256(encoded(session).encode()).hexdigest()
        self._link(db, url, "session", identity, session)

    def _registry(self, db):
        result = {}
        for row in db.execute("SELECT * FROM prs"):
            data = json.loads(row["metadata"])
            data["github"] = json.loads(row["github"])
            data["slack-threads"] = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM links WHERE owner_kind='pr' AND owner_key=? AND kind='slack' ORDER BY identity",
                (row["url"],))]
            data["agent-sessions"] = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM links WHERE owner_kind='pr' AND owner_key=? AND kind='session' ORDER BY identity",
                (row["url"],))]
            result[row["url"]] = data
        return result

    def export_registry(self):
        with self._connection(read=True) as db:
            return self._registry(db)

    def import_legacy(self, path):
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='legacy_imported'").fetchone():
                return 0
            data = activity.read_json(path, {})
            if not isinstance(data, dict):
                raise ValueError("Legacy PR register must be an object")
            if path.exists():
                activity.atomic_bytes(path.parent / "backups" / f"legacy-prs-{uuid.uuid4().hex}.json", path.read_bytes())
            for url, record in data.items():
                if not isinstance(record, dict) or record.get("url") != url:
                    raise ValueError("Legacy PR register contains an invalid record")
                self._ensure_pr(db, url, record)
            db.execute("INSERT INTO settings VALUES('legacy_imported',?)", (now_iso(),))
            return len(data)

    def apply_intent(self, key, kind, payload, source):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("An intent key of 1 to 200 characters is required")
        if not isinstance(payload, dict) or not isinstance(source, dict):
            raise ValueError("Intent payload and source must be objects")
        fields = {"register_pr": {"url", "description", "project", "workstream", "source_session", "clear_source_session"},
                  "update_pr": {"url", "description", "project", "workstream", "source_session", "clear_source_session", "expected"},
                  "attach_session": {"url", "session"}, "attach_slack": {"url", "thread", "description"},
                  "remove_slack": {"url", "thread"}}
        if kind not in fields or set(payload) - fields[kind]:
            raise ValueError("Unsupported intent or fields; GitHub state, reviews, and CI are read-only")
        url = activity.pr_url(payload.get("url", ""))
        with self._transaction() as db:
            old = db.execute("SELECT * FROM intents WHERE key=?", (key,)).fetchone()
            if old:
                if (old["kind"], old["payload"], old["source"]) != (kind, encoded(payload), encoded(source)):
                    raise ValueError("Intent key was already used for different content")
                return {"key": key, "duplicate": True, "url": url}
            if kind != "register_pr" and not db.execute("SELECT 1 FROM prs WHERE url=?", (url,)).fetchone():
                raise ValueError("PR is not registered")
            self._ensure_pr(db, url)
            row = db.execute("SELECT * FROM prs WHERE url=?", (url,)).fetchone()
            metadata = json.loads(row["metadata"])
            if "expected" in payload:
                expected = payload["expected"]
                allowed = {"description", "project_name", "workstream_name"}
                if not isinstance(expected, dict) or set(expected) - allowed:
                    raise ValueError("Only local metadata can be used as an edit precondition")
                branch = db.execute("SELECT p.name,w.name FROM tasks t JOIN workstreams w ON w.id=t.workstream_id "
                                    "JOIN projects p ON p.id=w.project_id WHERE t.id=?", (row["task_id"],)).fetchone()
                current = {"description": metadata.get("description"), "project_name": branch[0], "workstream_name": branch[1]}
                if any(current[key] != value for key, value in expected.items()):
                    raise ValueError("Metadata changed while this editor was open. Reload before saving.")
            if kind in ("register_pr", "update_pr"):
                if "description" in payload:
                    if not isinstance(payload["description"], str):
                        raise ValueError("Description must be a string")
                    metadata["description"] = payload["description"]
                if "project" in payload or "workstream" in payload:
                    if "workstream" in payload and "project" not in payload:
                        raise ValueError("Provide the project when assigning a workstream")
                    project = payload.get("project") or UNCLASSIFIED
                    ws = self._workstream(db, project, payload.get("workstream") or UNCLASSIFIED)
                    db.execute("UPDATE tasks SET workstream_id=? WHERE id=?", (ws, row["task_id"]))
                    metadata["project"] = project if project != UNCLASSIFIED else None
                if "source_session" in payload:
                    session = payload["source_session"]
                    if not isinstance(session, dict) or not session.get("id"):
                        raise ValueError("An explicitly resolved source session is required")
                    metadata["source-session"] = session
                if payload.get("clear_source_session"):
                    metadata["source-session"] = None
                    metadata["source-session-candidates"] = []
                db.execute("UPDATE prs SET metadata=? WHERE url=?", (encoded(metadata), url))
            elif kind == "attach_session":
                self._session(db, url, payload["session"])
            elif kind == "attach_slack":
                thread = activity.slack_url(payload["thread"])
                current = db.execute("SELECT payload FROM links WHERE owner_key=? AND kind='slack' AND identity=?",
                                     (url, thread)).fetchone()
                value = json.loads(current[0]) if current else {"url": thread}
                if "description" in payload:
                    if not isinstance(payload["description"], str):
                        raise ValueError("Thread description must be a string")
                    value["description"] = payload["description"]
                self._link(db, url, "slack", thread, value)
            elif kind == "remove_slack":
                thread = activity.slack_url(payload["thread"])
                db.execute("DELETE FROM links WHERE owner_kind='pr' AND owner_key=? AND kind='slack' AND identity=?",
                           (url, thread))
            db.execute("INSERT INTO intents VALUES(?,?,?,?,?)", (key, kind, encoded(payload), encoded(source), now_iso()))
        return {"key": key, "duplicate": False, "url": url}

    def catalog(self):
        """Known hierarchy names, including branches with no currently open PRs."""
        with self._connection(read=True) as db:
            projects = []
            for row in db.execute("SELECT id,name,closed FROM projects ORDER BY name COLLATE NOCASE,name"):
                workstreams = [{"id": item["id"], "name": item["name"], "closed": bool(item["closed"])}
                               for item in db.execute("SELECT id,name,closed FROM workstreams WHERE project_id=? "
                                                      "ORDER BY name COLLATE NOCASE,name", (row["id"],))]
                projects.append({"id": row["id"], "name": row["name"], "closed": bool(row["closed"]),
                                 "workstreams": workstreams})
            return {"projects": projects}

    def list_prs(self, project=None, workstream=None, search=None, state="open"):
        with self._connection(read=True) as db:
            registry = self._registry(db)
            rows = db.execute("SELECT prs.*, tasks.title AS task_title, workstreams.name AS workstream_name, "
                              "projects.name AS project_name FROM prs JOIN tasks ON tasks.id=prs.task_id "
                              "JOIN workstreams ON workstreams.id=tasks.workstream_id "
                              "JOIN projects ON projects.id=workstreams.project_id ORDER BY prs.url").fetchall()
            result = []
            pending_closes = {r['url']: dict(r) for r in db.execute(
                "SELECT * FROM pr_close_requests WHERE status IN ('running','uncertain')")}
            for row in rows:
                value = registry[row["url"]]
                github = value["github"]
                if state and github.get("state", "open").lower() != state.lower():
                    continue
                if project and row["project_name"] != project:
                    continue
                if workstream and row["workstream_name"] != workstream:
                    continue
                value.update({"task_id": row["task_id"], "task_title": row["task_title"],
                              "project_name": row["project_name"], "workstream_name": row["workstream_name"],
                              "review_state": "Blocked" if row["refresh_error"] else row["review_state"],
                              "review_reason": row["refresh_error"] or row["reason"], "observed_at": row["observed_at"],
                              "refresh_error": row["refresh_error"], "observation_version": row['observation_version'],
                              "close_request": pending_closes.get(row['url']),
                              "state_changed_at": None if row['refresh_error'] else row['state_changed_at'],
                              "state_first_seen_at": None if row['refresh_error'] else row['state_first_seen_at']})
                searchable = [value.get('url'), value.get('description'), github.get('title'),
                              value['task_title'], value['project_name'], value['workstream_name'],
                              value['slack-threads'], value['agent-sessions']]
                if search and search.casefold() not in encoded(searchable).casefold():
                    continue
                result.append(value)
            return result

    def observe(self, url, github, review_state, reason, expected_version=None):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM prs WHERE url=?', (url,)).fetchone()
            if row is None or expected_version is not None and row['observation_version'] != expected_version:
                return False
            previous = json.loads(row['github'])
            changed = (row['review_state'], previous.get('state')) != (review_state, github.get('state'))
            now = now_iso()
            first = now if changed or not row['state_first_seen_at'] else row['state_first_seen_at']
            transition = now if changed and row['observed_at'] else (None if changed else row['state_changed_at'])
            db.execute('UPDATE prs SET github=?,review_state=?,reason=?,observed_at=?,refresh_error=NULL, '
                       'state_changed_at=?,state_first_seen_at=?,observation_version=observation_version+1 WHERE url=?',
                       (encoded(github), review_state, reason, now, transition, first, url))
            return True

    def observation_error(self, url, error, expected_version=None):
        with self._transaction() as db:
            db.execute('UPDATE prs SET refresh_error=? WHERE url=? AND (? IS NULL OR observation_version=?)',
                       (error, url, expected_version, expected_version))

    def merge_archive(self, prs, evidence, messages, expected_versions=None):
        with self._transaction() as db:
            registry = self._registry(db)
            previous = {url: record['github'] for url, record in registry.items()}
            versions = {row['url']: row['observation_version'] for row in db.execute('SELECT url,observation_version FROM prs')}
            activity.merge_prs(registry, prs, evidence, messages)
            for url, record in registry.items():
                self._ensure_pr(db, url, record)
                if expected_versions is not None and url in versions and versions[url] != expected_versions.get(url):
                    record['github'] = previous[url]
                old = previous.get(url, {})
                github = record.get('github', {})
                lifecycle_changed = old.get('state') != github.get('state')
                curated = {key: value for key, value in record.items() if key != 'github'}
                db.execute('UPDATE prs SET metadata=?,github=?,observation_version=observation_version+?, '
                           'state_changed_at=CASE WHEN ? THEN NULL ELSE state_changed_at END, '
                           'state_first_seen_at=CASE WHEN ? THEN NULL ELSE state_first_seen_at END, '
                           'observed_at=CASE WHEN ? THEN NULL ELSE observed_at END WHERE url=?',
                           (encoded(curated), encoded(github), int(old != github), lifecycle_changed,
                            lifecycle_changed, lifecycle_changed, url))

    def begin_close(self, key, url, comment):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError('A close-request key of 1 to 200 characters is required')
        activity.pr_url(url)
        if not isinstance(comment, str) or len(comment) > 65536 or '\0' in comment:
            raise ValueError('The close comment must be text under 65,537 characters without null bytes')
        with self._transaction() as db:
            old = db.execute('SELECT * FROM pr_close_requests WHERE key=?', (key,)).fetchone()
            if old:
                if (old['url'], old['comment']) != (url, comment):
                    raise ValueError('Close-request key was already used for different content')
                return dict(old), False
            if not db.execute('SELECT 1 FROM prs WHERE url=?', (url,)).fetchone():
                raise ValueError('Only an ingested PR can be closed')
            if db.execute("SELECT 1 FROM pr_close_requests WHERE url=? AND status IN ('running','uncertain')", (url,)).fetchone():
                raise ValueError('A previous close request is pending or uncertain. Check its outcome on GitHub first.')
            now = now_iso()
            db.execute('INSERT INTO pr_close_requests VALUES(?,?,?,?,?,?,?,?)',
                       (key, url, comment, 'running', 'Checking the current GitHub PR state', 'preflight', now, now))
            return dict(db.execute('SELECT * FROM pr_close_requests WHERE key=?', (key,)).fetchone()), True

    def close_request(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError('A close-request key is required')
        with self._connection(read=True) as db:
            row = db.execute('SELECT * FROM pr_close_requests WHERE key=?', (key,)).fetchone()
            if row is None:
                raise ValueError('Close request not found')
            return dict(row)

    def finish_close(self, key, status, message, phase):
        if status not in ('running', 'succeeded', 'already_closed', 'failed', 'uncertain'):
            raise ValueError('Invalid close-request status')
        with self._transaction() as db:
            db.execute('UPDATE pr_close_requests SET status=?,message=?,phase=?,updated_at=? WHERE key=?',
                       (status, message, phase, now_iso(), key))
            return dict(db.execute('SELECT * FROM pr_close_requests WHERE key=?', (key,)).fetchone())

    def recover_closes(self):
        with self._transaction() as db:
            db.execute("UPDATE pr_close_requests SET status='uncertain',message=?,updated_at=? WHERE status='running'",
                       ('Application restarted during this request. Check GitHub before another close. No write will be replayed.', now_iso()))

    def inventory(self):
        with self._connection(read=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key='github_inventory'").fetchone()
            return json.loads(row[0]) if row else None

    def save_inventory(self, value):
        with self._transaction() as db:
            db.execute("INSERT INTO settings VALUES('github_inventory',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (encoded(value),))

    def quarantine_backfill(self, baseline_urls):
        """Offline, one-time correction of the initial over-broad discovery policy.

        All removed active rows are preserved verbatim in an untracked cache.
        Explicit intents, metadata changes, links, and legacy enrollments win.
        """
        baseline = set(baseline_urls)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='backfill_quarantined'").fetchone():
                return {'moved': 0, 'already_applied': True}
            intentional = {json.loads(row[0]).get('url') for row in db.execute('SELECT payload FROM intents')}
            moved = 0
            for row in db.execute('SELECT * FROM prs').fetchall():
                url = row['url']
                if url in baseline or url in intentional:
                    continue
                metadata = json.loads(row['metadata'])
                defaults = activity.new_pr(url)
                defaults.pop('github')
                if any(key not in defaults or value != defaults[key] for key, value in metadata.items()):
                    continue
                if db.execute("SELECT 1 FROM links WHERE owner_kind='pr' AND owner_key=?", (url,)).fetchone():
                    continue
                task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                branch = db.execute('SELECT w.name,p.name FROM workstreams w JOIN projects p ON p.id=w.project_id WHERE w.id=?',
                                    (task['workstream_id'],)).fetchone()
                github = json.loads(row['github'])
                references = db.execute('SELECT count(*) FROM prs WHERE task_id=?', (row['task_id'],)).fetchone()[0]
                if task['closed'] or tuple(branch) != (UNCLASSIFIED, UNCLASSIFIED) or references != 1:
                    continue
                if task['title'] not in (url, github.get('title')):
                    continue
                snapshot = {'pr': dict(row), 'task': dict(task)}
                db.execute('INSERT INTO untracked_cache VALUES(?,?,?)', (url, encoded(snapshot), now_iso()))
                db.execute('DELETE FROM prs WHERE url=?', (url,))
                db.execute('DELETE FROM tasks WHERE id=?', (row['task_id'],))
                moved += 1
            db.execute("INSERT INTO settings VALUES('backfill_quarantined',?)",
                       (encoded({'at': now_iso(), 'baseline_count': len(baseline), 'moved': moved}),))
            return {'moved': moved, 'already_applied': False}

    def jobs(self):
        with self._connection(read=True) as db:
            return [{"name": row[0], **json.loads(row[1])} for row in db.execute("SELECT name,value FROM jobs ORDER BY name")]

    def save_job(self, name, value):
        with self._transaction() as db:
            db.execute("INSERT INTO jobs VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       (name, encoded(value)))
