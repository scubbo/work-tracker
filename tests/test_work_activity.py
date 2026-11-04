import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import shutil
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "work_activity.py"
spec = importlib.util.spec_from_file_location("work_activity", MODULE)
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)

PR = "https://github.com/acme/service/pull/42"
THREAD = "https://acme.slack.com/archives/C123/p1793390000000000"
NOW = "2026-10-30T17:00:00Z"
SINCE = "2026-10-29T17:00:00Z"


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "activity"

    def tearDown(self):
        self.temp.cleanup()

    def test_refresh_preserves_curated_metadata_and_closed_prs(self):
        registry = {PR: {"url": PR, "project": "Permissions", "description": "Read-only Git",
                         "slack-threads": [{"url": THREAD}],
                         "source-session": {"id": "ses_original"},
                         "github": {"state": "open"}}}
        wa.merge_prs(registry, [{"url": PR, "title": "Git access", "state": "merged"}], [], [])
        self.assertEqual(registry[PR]["github"]["state"], "merged")
        self.assertEqual(registry[PR]["project"], "Permissions")
        self.assertEqual(registry[PR]["description"], "Read-only Git")
        self.assertEqual(registry[PR]["slack-threads"], [{"url": THREAD}])
        self.assertEqual(registry[PR]["source-session"]["id"], "ses_original")
        wa.merge_prs(registry, [], [], [])
        self.assertIn(PR, registry)

    def test_group_and_filter_prs(self):
        prs = {PR: {"project": "Permissions", "github": {"state": "merged", "createdAt": NOW}},
               "other": {"project": "CLI", "github": {"state": "open", "createdAt": SINCE}}}
        self.assertEqual(len(wa.filter_prs(prs, project="Permissions", state="merged")), 1)
        self.assertEqual(wa.count_projects(list(prs.values())), {"CLI": 1, "Permissions": 1})
        self.assertEqual(len(wa.filter_prs(prs, created_since=NOW)), 1)

    def test_urls_are_validated_and_canonicalized(self):
        self.assertEqual(wa.pr_url(PR + "/"), PR)
        self.assertEqual(wa.slack_url(THREAD + "?thread_ts=1793390000.000000"),
                         THREAD + "?thread_ts=1793390000.000000")
        for url in ["file:///tmp/file", "https://github.com.evil/acme/service/pull/42", PR + "/files"]:
            with self.assertRaises(ValueError):
                wa.pr_url(url)
        with self.assertRaises(ValueError):
            wa.slack_url("https://evil.example/archives/C123/p123")

    def test_slack_pages_are_complete_deduplicated_and_time_bounded(self):
        start = wa.parse_time(SINCE).timestamp()
        message = {"ts": str(start), "text": PR, "permalink": THREAD}
        pages = []

        def run(args):
            page = int(args[args.index("--page") + 1])
            pages.append(page)
            return {"success": True, "data": {"messages": [message], "page": page,
                                                "pageCount": 2, "total": 2}}

        result = wa.collect_slack(SINCE, NOW, run)
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(result["messages_sent"]), 1)

    def test_slack_error_is_not_an_empty_success(self):
        with self.assertRaises(wa.CollectionError):
            wa.collect_slack(SINCE, NOW, lambda args: {"success": False, "error": "not_authed"})

    def test_session_pagination_and_transcript_archive(self):
        session = {"id": "ses_example", "title": "Git access", "projectID": "repo",
                   "time": {"created": 1, "updated": wa.parse_time(NOW).timestamp() * 1000},
                   "location": {"directory": "/missing worktree"}}
        transcript = {"info": session, "messages": [{"type": "user", "text": "Implement access"}]}
        cursors = []

        def run(args):
            operation = args[2]
            if operation == "session.active":
                return {"data": {}}
            if operation == "session.list":
                cursor = next((arg for arg in args if arg.startswith("cursor=")), None)
                cursors.append(cursor)
                if cursor:
                    return {"data": [], "cursor": {}}
                return {"data": [session], "cursor": {"next": "page2"}}
            if operation == "experimental.session.export":
                return {"data": transcript}
            raise AssertionError(args)

        result = wa.collect_sessions(SINCE, NOW, self.root / "transcripts", run)
        self.assertEqual(cursors, [None, "cursor=page2"])
        entry = result["sessions"][0]
        self.assertIn("--session ses_example", entry["resume"])
        self.assertIn("'/missing worktree'", entry["resume"])
        with gzip.open(self.root / entry["transcript"], "rt") as stream:
            self.assertEqual(json.load(stream), transcript)

    def test_creation_evidence_does_not_treat_mentions_as_authorship(self):
        transcript = {"info": {"id": "ses_create", "location": {"directory": "/work"}},
                      "messages": [{"id": "msg1", "type": "assistant", "content": [
                          {"type": "text", "text": PR},
                          {"type": "tool", "name": "shell", "state": {
                              "status": "completed", "input": {"command": "gh pr view 42"},
                              "content": [{"type": "text", "text": PR}]}}
                      ]}]}
        self.assertEqual(wa.creation_evidence(transcript), [])
        tool = transcript["messages"][0]["content"][1]
        tool["state"]["input"]["command"] = 'gh pr create --title "Git access" --body "Scope"'
        evidence = wa.creation_evidence(transcript)
        self.assertEqual(evidence[0]["url"], PR)
        self.assertEqual(evidence[0]["session"]["id"], "ses_create")
        tool["state"]["input"]["command"] += f"; echo {PR}"
        self.assertEqual(wa.creation_evidence(transcript), [])

    def test_ambiguous_sources_remain_candidates(self):
        registry = {}
        candidates = [{"url": PR, "session": {"id": sid}} for sid in ["ses_a", "ses_b"]]
        wa.merge_prs(registry, [{"url": PR, "title": "Access"}], candidates, [])
        self.assertIsNone(registry[PR]["source-session"])
        self.assertEqual(len(registry[PR]["source-session-candidates"]), 2)

    def test_slack_correlation_does_not_overwrite_curated_threads(self):
        registry = {}
        wa.merge_prs(registry, [{"url": PR}], [], [{"text": f"Review {PR}", "permalink": THREAD}])
        self.assertEqual(registry[PR]["slack-threads"], [])
        self.assertEqual(registry[PR]["related-slack-messages"][0]["url"], THREAD)

    def test_failed_source_keeps_its_checkpoint_and_saves_other_sources(self):
        checkpoints = {"slack": SINCE, "github": SINCE}
        wa.write_json(self.root / "checkpoints.json", checkpoints)

        def slack(since, until, run):
            raise wa.CollectionError("Slack unavailable")

        def github(since, until, run):
            return {"source": "github", "since": since, "until": until, "prs": []}

        result = wa.collect(self.root, NOW, ["slack", "github"],
                            collectors={"slack": slack, "github": github})
        saved = wa.read_json(self.root / "checkpoints.json", {})
        self.assertEqual(saved["slack"], SINCE)
        self.assertEqual(saved["github"], NOW)
        self.assertEqual(result["sources"]["slack"]["status"], "error")
        self.assertTrue((self.root / result["directory"] / "prs.json").exists())

    def test_retention_only_removes_expired_transcripts(self):
        old = self.root / "snapshots/2026-09-01/run1"
        new = self.root / "snapshots/2026-10-29/run1"
        for folder, captured in [(old, "2026-09-01T10:00:00Z"), (new, SINCE)]:
            wa.write_json(folder / "manifest.json", {"captured_at": captured})
            wa.write_json(folder / "sessions.json", {"sessions": []})
            wa.write_gzip(folder / "transcripts/ses_example.json.gz", {"private": True})
        wa.prune_transcripts(self.root, NOW)
        self.assertFalse((old / "transcripts/ses_example.json.gz").exists())
        self.assertTrue((old / "sessions.json").exists())
        self.assertTrue((new / "transcripts/ses_example.json.gz").exists())

    def test_archives_use_private_permissions(self):
        path = self.root / "snapshots/run/file.json"
        wa.write_json(path, {"private": True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_invalid_json_is_not_silently_replaced(self):
        self.root.mkdir()
        file = self.root / "prs.json"
        file.write_text("{broken")
        with self.assertRaises(json.JSONDecodeError):
            wa.read_json(file, {})

    def test_github_search_collects_all_pages_and_merges(self):
        pages = []

        def run(args):
            if args == ["gh", "api", "user"]:
                return {"login": "jack"}
            page = next(int(a.split("=", 1)[1]) for a in args if a.startswith("page="))
            pages.append(page)
            item = {"html_url": PR, "number": 42, "repository_url": "https://api.github.com/repos/acme/service",
                    "title": "Access", "user": {"login": "jack"}, "state": "closed",
                    "created_at": SINCE, "updated_at": NOW, "pull_request": {"merged_at": NOW}}
            return {"total_count": 101, "incomplete_results": False, "items": [item]}

        result = wa.collect_github(SINCE, NOW, run)
        self.assertEqual(pages, [1, 2])
        self.assertEqual(result["prs"][0]["state"], "merged")

    def test_incomplete_github_search_is_a_failure(self):
        def run(args):
            return {"login": "jack"} if args[-1] == "user" else {"total_count": 1001}

        with self.assertRaises(wa.CollectionError):
            wa.collect_github(SINCE, NOW, run)

    def test_cli_metadata_and_project_counts_without_network(self):
        def cli(*args):
            result = subprocess.run([sys.executable, str(MODULE), "--state-dir", str(self.root), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        cli("pr", "set", PR, "--project", "Permissions", "--description", "Read-only Git")
        cli("pr", "thread-add", PR, THREAD, "--description", "Design discussion")
        cli("pr", "thread-add", PR, THREAD)
        self.assertEqual(cli("pr", "count", "--by-project"), {"Permissions": 1})
        entry = cli("pr", "list", "--project", "Permissions")[0]
        self.assertEqual(entry["slack-threads"], [{"url": THREAD, "description": "Design discussion"}])
        cli("pr", "thread-remove", PR, THREAD)
        self.assertEqual(cli("pr", "show", PR)["slack-threads"], [])

    def test_source_hints_survive_when_github_is_temporarily_unavailable(self):
        hints = {"creation-evidence": [{"url": PR, "session": {"id": "ses_a"}}],
                 "messages": [{"permalink": THREAD, "text": PR, "ts": "123"}]}
        wa.write_json(self.root / "correlations.json", hints)

        def github(since, until, run):
            return {"source": "github", "since": since, "until": until, "prs": [{"url": PR}]}

        wa.collect(self.root, NOW, ["github"], collectors={"github": github})
        pr = wa.read_json(self.root / "prs.json", {})[PR]
        self.assertEqual(pr["source-session"]["id"], "ses_a")
        self.assertEqual(pr["related-slack-messages"][0]["url"], THREAD)

    def test_error_only_archives_are_visible_to_summaries(self):
        def github(since, until, run):
            raise wa.CollectionError("GitHub unavailable")

        wa.collect(self.root, NOW, ["github"], collectors={"github": github})
        self.assertEqual(len(wa.snapshot_index(self.root, SINCE, NOW)["runs"]), 1)
        self.assertEqual(wa.read_json(self.root / "checkpoints.json", {})["github"], SINCE)

    def test_creation_evidence_tracks_directory_at_creation_not_current_directory(self):
        transcript = {"info": {"id": "ses_create", "location": {"directory": "/later"}}, "messages": [
            {"id": "msg1", "type": "assistant", "content": [{"type": "tool", "name": "shell", "state": {
                "status": "completed", "input": {"command": "gh pr create --fill"},
                "content": [{"type": "text", "text": PR}]}}]},
            {"type": "location-switched", "previous": {"location": {"directory": "/original"}},
             "location": {"directory": "/later"}}
        ]}
        self.assertEqual(wa.creation_evidence(transcript)[0]["session"]["directory"], "/original")

    def fake_cli_json(self, command, content, returncode=0):
        config = {"commands": {command: sys.executable}}
        script = f"import sys; sys.stdout.write({content!r}); sys.exit({returncode})"
        with mock.patch.object(wa, "read_json", return_value=config):
            return wa.run_json([command, "-c", script])

    def test_slack_retry_notices_before_json_are_accepted_after_command_remapping(self):
        notice = "[INFO]  web-api:WebClient:0 API Call failed due to rate limiting. Will retry in 4 seconds.\n"
        payload = {"success": True, "data": {"messages": [{"text": notice}]}}
        notices = notice + notice.replace(":0 ", ":12 ").replace("4 seconds.\n", "12 seconds.\r\n")
        self.assertEqual(self.fake_cli_json("slack-cli", notices + json.dumps(payload)), payload)

    def test_slack_retry_notices_do_not_hide_invalid_json_or_unknown_output(self):
        notice = "[INFO]  web-api:WebClient:0 API Call failed due to rate limiting. Will retry in 4 seconds.\n"
        for content in [notice + '{"success":',
                        "unexpected output\n" + '{"success": true}',
                        notice + "unexpected output\n" + '{"success": true}',
                        '{"success": true}\n' + notice]:
            with self.subTest(content=content), self.assertRaises(wa.CollectionError) as error:
                self.fake_cli_json("slack-cli", content)
            self.assertIn("returned invalid JSON", str(error.exception))
            self.assertNotIn(content, str(error.exception))

    def test_other_cli_json_remains_strict(self):
        notice = "[INFO]  web-api:WebClient:0 API Call failed due to rate limiting. Will retry in 4 seconds.\n"
        for command in ["gh", "opencode"]:
            with self.subTest(command=command), self.assertRaises(wa.CollectionError):
                self.fake_cli_json(command, notice + '{"success": true}')

    def test_slack_retry_notice_does_not_hide_nonzero_exit(self):
        notice = "[INFO]  web-api:WebClient:0 API Call failed due to rate limiting. Will retry in 4 seconds.\n"
        with self.assertRaises(wa.CollectionError) as error:
            self.fake_cli_json("slack-cli", notice + '{"success": true}', returncode=1)
        self.assertIn("exited 1", str(error.exception))

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the stdout flush regression")
    def test_large_cli_output_is_not_truncated_by_immediate_process_exit(self):
        script = 'process.stdout.write(JSON.stringify({text: "x".repeat(2000000)})); process.exit(0)'
        result = wa.run_json(["node", "-e", script])
        self.assertEqual(len(result["text"]), 2000000)


if __name__ == "__main__":
    unittest.main()
