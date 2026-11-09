#!/usr/bin/env python3
"""Private activity snapshots and an editable pull-request register (Python 3.10+)."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


class CollectionError(Exception):
    pass


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone, for example 2026-10-30T17:00:00Z")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def private_directory(path):
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def atomic_bytes(path, content):
    path = Path(path)
    private_directory(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".activity-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def write_gzip(path, value):
    atomic_bytes(path, gzip.compress(json.dumps(value, ensure_ascii=False).encode(), mtime=0))


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


@contextmanager
def store_lock(root):
    private_directory(root)
    root.chmod(0o700)
    with open(root / ".lock", "a") as stream:
        os.chmod(root / ".lock", 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def pr_url(value):
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc != "github.com"
            or url.query or url.fragment
            or not re.fullmatch(r"/[\w.-]+/[\w.-]+/pull/[1-9]\d*/?", url.path)):
        raise ValueError("Expected a GitHub PR URL: https://github.com/owner/repo/pull/123")
    return value.rstrip("/")


def slack_url(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not re.fullmatch(r"[\w-]+\.slack\.com", url.netloc)
            or not re.fullmatch(r"/archives/[A-Z0-9]+/p\d+", url.path)):
        raise ValueError("Expected a Slack message/thread link: https://workspace.slack.com/archives/C…/p…")
    return value


def run_json(args, *, timeout=180):
    command = args[0]
    config_path = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    config = read_json(config_path / "work-activity/config.json", {})
    args = [config.get("commands", {}).get(args[0], args[0]), *args[1:]]
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    if config.get("path"):
        env["PATH"] = config["path"]
    try:
        # Some CLIs exit before an asynchronous pipe write drains. A regular file
        # makes their stdout writes synchronous and keeps large exports intact.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(args, stdout=output, stderr=subprocess.PIPE, timeout=timeout, env=env)
            output.seek(0)
            content = output.read()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CollectionError(f"Cannot run {Path(args[0]).name}: {type(error).__name__}") from error
    if result.returncode:
        # CLI output can contain prompts, credentials, or private messages. Keep it out of logs.
        raise CollectionError(f"{Path(args[0]).name} exited {result.returncode}; check authentication/service health")
    if command == "slack-cli":
        # Slack's SDK logs retry notices to stdout even in JSON mode. Remove only
        # these leading notices, not arbitrary output or text inside messages.
        content = re.sub(
            rb"\A(?:\[INFO\] +web-api:WebClient:\d+ API Call failed due to rate limiting\. "
            rb"Will retry in \d+ seconds\.\r?\n)+", b"", content, count=1)
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise CollectionError(f"{Path(args[0]).name} returned invalid JSON") from error


def api(operation, run, **params):
    args = ["opencode", "api", operation]
    for key, value in params.items():
        args += ["--param", f"{key}={value}"]
    result = run(args)
    if "data" not in result:
        raise CollectionError(f"OpenCode {operation} response is missing data")
    return result


def session_reference(info):
    sid = info["id"]
    if not re.fullmatch(r"ses_[A-Za-z0-9]+", sid):
        raise ValueError("Invalid OpenCode session ID")
    directory = info.get("location", {}).get("directory")
    command = f"opencode --session {shlex.quote(sid)}"
    if directory:
        command = f"cd {shlex.quote(directory)} && {command}"
    return {"id": sid, "directory": directory, "title": info.get("title"), "resume": command}


def creation_evidence(transcript):
    """Only a completed, standalone `gh pr create` with a bare URL result is evidence.

    Composite shell commands, prose, and PR-view results deliberately need manual attribution.
    """
    info = transcript["info"]
    locations = [m for m in transcript["messages"] if m.get("type") == "location-switched"]
    directory = info.get("location", {}).get("directory")
    if locations and locations[0].get("previous"):
        directory = locations[0]["previous"]["location"]["directory"]
    evidence = []
    for message in transcript["messages"]:
        if message.get("type") == "location-switched":
            directory = message["location"]["directory"]
        for tool in message.get("content", []):
            if tool.get("type") != "tool" or tool.get("name") != "shell":
                continue
            state = tool.get("state", {})
            if state.get("status") != "completed":
                continue
            command = state.get("input", {}).get("command", "")
            if not isinstance(command, str) or "\n" in command:
                continue
            try:
                tokens = list(shlex.shlex(command, posix=True, punctuation_chars=True))
            except ValueError:
                continue
            if tokens[:3] != ["gh", "pr", "create"] or any(
                t in {";", "&&", "||", "|", "&", "(", ")", ">", "<"} for t in tokens
            ):
                continue
            text = "\n".join(c.get("text", "") for c in state.get("content", []) if c.get("type") == "text")
            try:
                url = pr_url(text.strip())
            except ValueError:
                continue
            reference = session_reference({**info, "location": {"directory": directory}})
            reference.update({"method": "gh-pr-create", "messageID": message["id"]})
            evidence.append({"url": url, "session": reference})
    return evidence


def collect_sessions(since, until, transcript_dir, run=run_json):
    start = parse_time(since).timestamp() * 1000
    sessions = {}
    cursor = None
    seen = set()
    while True:
        params = {"cursor": cursor} if cursor else {"order": "desc"}
        page = api("session.list", run, limit=100, **params)
        for info in page["data"]:
            if info["time"]["updated"] >= start:
                sessions[info["id"]] = info
        cursor = page.get("cursor", {}).get("next")
        if not cursor or any(s["time"]["updated"] < start for s in page["data"]):
            break
        if cursor in seen:
            raise CollectionError("OpenCode repeated a session cursor")
        seen.add(cursor)
    # Long-running sessions may not have received a new prompt during this window.
    active = api("session.active", run)["data"]
    for sid in active:
        if sid not in sessions:
            sessions[sid] = api("session.get", run, sessionID=sid)["data"]
    entries, evidence, errors = [], [], []
    for sid, info in sessions.items():
        entry = {**info, **session_reference(info)}
        try:
            transcript = api("experimental.session.export", run, sessionID=sid, sanitize="false")["data"]
            if transcript["info"]["id"] != sid:
                raise CollectionError("OpenCode exported a different session")
            target = transcript_dir / f"{sid}.json.gz"
            write_gzip(target, transcript)
            entry["transcript"] = f"transcripts/{target.name}"
            entry["locations"] = [m for m in transcript["messages"] if m.get("type") == "location-switched"]
            evidence.extend(creation_evidence(transcript))
        except (CollectionError, ValueError, KeyError) as error:
            entry["error"] = str(error)
            errors.append(f"{sid}: {error}")
        entries.append(entry)
    return {"source": "opencode", "since": since, "until": until, "sessions": entries,
            "creation-evidence": evidence, "errors": errors}


def collect_slack(since, until, run=run_json):
    start, end = parse_time(since), parse_time(until)
    after = (start - timedelta(days=1)).strftime("%Y-%m-%d")
    before = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    messages = {}
    page = 1
    while True:
        result = run(["slack-cli", "search", "", "--me", "--after", after, "--before", before,
                      "--count", "100", "--page", str(page), "--sort", "timestamp", "--json"])
        if result.get("success") is not True:
            raise CollectionError("Slack search failed; check slack-cli auth status")
        data = result["data"]
        for message in data["messages"]:
            timestamp = datetime.fromtimestamp(float(message["ts"]), timezone.utc)
            if start <= timestamp < end:
                key = message.get("permalink") or (message.get("channel"), message["ts"])
                messages[key] = {k: message.get(k) for k in (
                    "ts", "text", "permalink", "channel", "user", "userName", "replyCount"
                )}
        page_count = int(data["pageCount"])
        if page >= page_count:
            break
        if not data["messages"]:
            raise CollectionError("Slack returned an empty page before the end of search results")
        page += 1
    return {"source": "slack", "since": since, "until": until,
            "messages_sent": sorted(messages.values(), key=lambda m: float(m["ts"]))}


def collect_github(since, until, run=run_json):
    user = run(["gh", "api", "user"])["login"]
    query = f"is:pr author:{user} updated:{since}..{until}"
    page, prs = 1, []
    while True:
        result = run(["gh", "api", "--method", "GET", "search/issues", "-f", f"q={query}",
                      "-f", "per_page=100", "-f", f"page={page}", "-f", "sort=updated", "-f", "order=desc"])
        if result.get("incomplete_results") or result["total_count"] > 1000:
            raise CollectionError("GitHub search is incomplete or exceeds 1,000 PRs; collect a shorter --since/--until window")
        for item in result["items"]:
            if item["user"]["login"].casefold() != user.casefold():
                continue
            merged = item.get("pull_request", {}).get("merged_at")
            prs.append({"url": pr_url(item["html_url"]), "number": item["number"],
                        "repository": item["repository_url"].split("/repos/", 1)[1],
                        "title": item["title"], "body": item.get("body"),
                        "state": "merged" if merged else item["state"], "draft": item.get("draft", False),
                        "author": user, "createdAt": item["created_at"], "updatedAt": item["updated_at"],
                        "closedAt": item.get("closed_at"), "mergedAt": merged,
                        "labels": [label["name"] for label in item.get("labels", [])]})
        if page * 100 >= result["total_count"]:
            break
        if not result["items"]:
            raise CollectionError("GitHub returned an empty page before the end of search results")
        page += 1
    return {"source": "github", "since": since, "until": until, "user": user, "prs": prs}


def new_pr(url):
    return {"url": url, "project": None, "description": None, "slack-threads": [],
            "source-session": None, "source-session-candidates": [], "related-slack-messages": [], "github": {}}


def merge_prs(registry, github_prs, evidence, messages):
    for pr in github_prs:
        registry.setdefault(pr["url"], new_pr(pr["url"]))["github"] = pr
    for item in evidence:
        # Evidence alone must not register someone else's PR as one Jack authored.
        if item["url"] not in registry:
            continue
        pr = registry[item["url"]]
        candidates = pr.setdefault("source-session-candidates", [])
        if item["session"]["id"] not in {s["id"] for s in candidates}:
            candidates.append({"method": "gh-pr-create", **item["session"]})
        source = pr.get("source-session")
        if source is None or source.get("method") == "gh-pr-create":
            pr["source-session"] = candidates[0] if len(candidates) == 1 else None
    for message in messages:
        if not message.get("permalink"):
            continue
        urls = re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*(?!\d)", message.get("text") or "")
        for url in set(urls) & registry.keys():
            related = registry[url].setdefault("related-slack-messages", [])
            if message["permalink"] not in {m["url"] for m in related}:
                related.append({"url": message["permalink"], "text": message.get("text"), "ts": message.get("ts")})


def filter_prs(prs, project=None, state=None, created_since=None, created_before=None):
    result = []
    for pr in prs.values():
        github = pr.get("github", {})
        if project is not None and pr.get("project") != project:
            continue
        if state and github.get("state") != state:
            continue
        created = github.get("createdAt")
        if created_since and (not created or parse_time(created) < parse_time(created_since)):
            continue
        if created_before and (not created or parse_time(created) >= parse_time(created_before)):
            continue
        result.append(pr)
    return sorted(result, key=lambda p: p.get("github", {}).get("createdAt", ""), reverse=True)


def count_projects(prs):
    return dict(sorted(Counter(pr.get("project") or "(unassigned)" for pr in prs).items()))


def prune_transcripts(root, now):
    cutoff = parse_time(now) - timedelta(days=30)
    removed = 0
    for manifest in (root / "snapshots").glob("*/*/manifest.json"):
        data = read_json(manifest, {})
        if not data.get("captured_at") or parse_time(data["captured_at"]) >= cutoff:
            continue
        for transcript in (manifest.parent / "transcripts").glob("ses_*.json.gz"):
            if transcript.is_file() and not transcript.is_symlink():
                transcript.unlink()
                removed += 1
    return removed


def collect(root, until, sources, since=None, run=run_json, collectors=None,
            registry=None, persist_registry=True):
    end = parse_time(until)
    if since and parse_time(since) >= end:
        raise ValueError("--since must be earlier than --until")
    collectors = collectors or {"slack": collect_slack, "github": collect_github}
    checkpoints = read_json(root / "checkpoints.json", {})
    captured = datetime.now(timezone.utc)
    local_date = captured.astimezone().strftime("%Y-%m-%d")
    directory = Path("snapshots") / local_date / (captured.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    archive = root / directory
    private_directory(archive)
    manifest = {"version": 1, "captured_at": iso(captured), "directory": str(directory), "sources": {}}
    results = {}

    def source_start(source):
        if since:
            return since
        checkpoint = checkpoints.get(source)
        # Search indexes and updates can become visible after a previous query.
        return iso(parse_time(checkpoint) - timedelta(minutes=15)) if checkpoint else iso(end - timedelta(days=1))

    def fetch(source):
        start = source_start(source)
        if parse_time(start) >= end:
            raise ValueError(f"{source} checkpoint is not before --until; provide --since for a historical window")
        if source == "opencode":
            data = collect_sessions(start, until, archive / "transcripts", run)
        else:
            data = collectors[source](start, until, run)
        return start, data

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {s: pool.submit(fetch, s) for s in sources}
        for source, future in futures.items():
            start = source_start(source)
            try:
                start, data = future.result()
                filename = "sessions.json" if source == "opencode" else f"{source}.json"
                write_json(archive / filename, data)
                results[source] = data
                manifest["sources"][source] = {"status": "partial" if data.get("errors") else "ok",
                                                "since": start, "until": until, "file": filename,
                                                "items": len(data.get({"opencode": "sessions", "slack": "messages_sent",
                                                                       "github": "prs"}[source], []))}
                if data.get("errors"):
                    manifest["sources"][source]["errors"] = data["errors"]
            except (CollectionError, ValueError, KeyError, TypeError) as error:
                manifest["sources"][source] = {"status": "error", "error": str(error), "since": start, "until": until}
    registry = read_json(root / "prs.json", {}) if registry is None else registry
    # Sources checkpoint independently. Retain hints even if GitHub is unavailable or
    # has not indexed a newly created PR yet, then attach them when the PR is discovered.
    hints = read_json(root / "correlations.json", {"creation-evidence": [], "messages": []})
    evidence = {(e["url"], e["session"]["id"]): e for e in hints["creation-evidence"]}
    for item in results.get("opencode", {}).get("creation-evidence", []):
        evidence[(item["url"], item["session"]["id"])] = item
    messages = {m["permalink"]: m for m in hints["messages"]}
    for message in results.get("slack", {}).get("messages_sent", []):
        if message.get("permalink") and "https://github.com/" in (message.get("text") or ""):
            messages[message["permalink"]] = message
    hints = {"creation-evidence": list(evidence.values()), "messages": list(messages.values())}
    merge_prs(registry, results.get("github", {}).get("prs", []),
              hints["creation-evidence"], hints["messages"])
    write_json(root / "correlations.json", hints)
    if persist_registry:
        write_json(root / "prs.json", registry)
    write_json(archive / "prs.json", registry)
    write_json(archive / "manifest.json", manifest)
    # Each source advances only after its output and the manifest are durable.
    for source, status in manifest["sources"].items():
        if status["status"] == "ok" and (not checkpoints.get(source) or end > parse_time(checkpoints[source])):
            checkpoints[source] = until
        elif source not in checkpoints:
            # A failed first run still remembers where recovery must start.
            checkpoints[source] = status["since"]
    write_json(root / "checkpoints.json", checkpoints)
    manifest["expired_transcripts_removed"] = prune_transcripts(root, iso(datetime.now(timezone.utc)))
    write_json(archive / "manifest.json", manifest)
    return manifest


def snapshot_index(root, since, until):
    start, end = parse_time(since), parse_time(until)
    if start >= end:
        raise ValueError("--since must be earlier than --until")
    runs = []
    for path in sorted((root / "snapshots").glob("*/*/manifest.json")):
        manifest = read_json(path, {})
        if any(s.get("since") and parse_time(s["since"]) < end and parse_time(s["until"]) > start
               for s in manifest.get("sources", {}).values()):
            runs.append({**manifest, "path": str(path.parent)})
    return {"since": since, "until": until, "runs": runs, "register": str(root / "prs.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "work-activity"
    parser.add_argument("--state-dir", type=Path, default=default_root)
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("collect", help="Snapshot authored PRs, sent Slack messages, and OpenCode sessions")
    capture.add_argument("--since", help="ISO8601 override for backfill; default: each source checkpoint or 24h ago")
    capture.add_argument("--until", help="ISO8601 end; default: now")
    capture.add_argument("--sources", nargs="+", choices=["opencode", "slack", "github"],
                         default=["opencode", "slack", "github"])
    sub.add_parser("status", help="Show per-source checkpoints and project counts")
    snapshots = sub.add_parser("snapshots", help="Find archives overlapping a daily/weekly report window")
    snapshots.add_argument("--since", required=True)
    snapshots.add_argument("--until", required=True)
    prs = sub.add_parser("pr", help="Manage the local PR register; never edits GitHub")
    commands = prs.add_subparsers(dest="pr_action", required=True)
    for name in ("list", "count"):
        command = commands.add_parser(name)
        command.add_argument("--project")
        command.add_argument("--state", choices=["open", "closed", "merged"])
        command.add_argument("--created-since")
        command.add_argument("--created-before")
        if name == "count":
            command.add_argument("--by-project", action="store_true")
    commands.add_parser("show").add_argument("url", type=pr_url)
    update = commands.add_parser("set")
    update.add_argument("url", type=pr_url)
    update.add_argument("--project")
    update.add_argument("--description")
    origin = update.add_mutually_exclusive_group()
    origin.add_argument("--source-session")
    origin.add_argument("--clear-source-session", action="store_true")
    thread = commands.add_parser("thread-add")
    thread.add_argument("url", type=pr_url)
    thread.add_argument("thread", type=slack_url)
    thread.add_argument("--description")
    remove = commands.add_parser("thread-remove")
    remove.add_argument("url", type=pr_url)
    remove.add_argument("thread", type=slack_url)
    args = parser.parse_args(argv)
    root = args.state_dir.expanduser().resolve()
    try:
        if (root / "application.json").exists() or (root / "tracker.sqlite3").exists():
            from work_tracker.client import request, wait_for_job
            parameters = {key: value for key, value in vars(args).items() if key != "state_dir"}
            result = request(root, "legacy", parameters)
            if args.action == "collect":
                result = wait_for_job(root, result)["manifest"]
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        with store_lock(root):
            registry = read_json(root / "prs.json", {})
            if args.action == "collect":
                result = collect(root, args.until or iso(datetime.now(timezone.utc)), args.sources, args.since)
            elif args.action == "status":
                result = {"state_directory": str(root), "checkpoints": read_json(root / "checkpoints.json", {}),
                          "prs": len(registry), "projects": count_projects(list(registry.values()))}
            elif args.action == "snapshots":
                result = snapshot_index(root, args.since, args.until)
            elif args.pr_action in ("list", "count"):
                entries = filter_prs(registry, args.project, args.state, args.created_since, args.created_before)
                result = entries if args.pr_action == "list" else (
                    count_projects(entries) if args.by_project else {"count": len(entries)})
            elif args.pr_action == "show":
                if args.url not in registry:
                    raise ValueError("PR is not registered; use pr set or collect first")
                result = registry[args.url]
            else:
                entry = registry.setdefault(args.url, new_pr(args.url))
                if args.pr_action == "set":
                    for field in ("project", "description"):
                        if getattr(args, field) is not None:
                            entry[field] = getattr(args, field)
                    if args.source_session:
                        info = api("session.get", run_json, sessionID=args.source_session)["data"]
                        entry["source-session"] = {**session_reference(info), "method": "manual"}
                    if args.clear_source_session:
                        entry["source-session"] = None
                        entry["source-session-candidates"] = []
                elif args.pr_action == "thread-add":
                    links = entry["slack-threads"]
                    existing = next((link for link in links if link["url"] == args.thread), None)
                    if existing is None:
                        existing = {"url": args.thread}
                        links.append(existing)
                    if args.description is not None:
                        existing["description"] = args.description
                else:
                    entry["slack-threads"] = [link for link in entry["slack-threads"] if link["url"] != args.thread]
                write_json(root / "prs.json", registry)
                result = entry
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if args.action == "collect" and any(s["status"] != "ok" for s in result["sources"].values()):
                return 1
    except (CollectionError, ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(f"work-activity: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
