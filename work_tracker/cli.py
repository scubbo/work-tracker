"""Command-line interface onto the local application."""

import argparse
import json
from pathlib import Path
import sys
import uuid

from .client import default_state, request, wait_for_job
from .state_age import state_age
from .daily_stats import duration_text


def terminal_text(value):
    return ''.join(char if char.isprintable() else f'\\x{ord(char):02x}' for char in str(value))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local work tracker: application, PRs, and agent intents")
    parser.add_argument("--state-dir", type=Path, default=default_state())
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="Run the local application and its own scheduler")
    server.add_argument("--no-jobs", action="store_true", help="Disable job execution for isolated tests")
    commands.add_parser("mcp", help="Run the stdio MCP adapter; the application must already be running")
    commands.add_parser("tui", help="Open the Textual interface onto the local application")
    commands.add_parser("status")
    commands.add_parser("jobs")
    commands.add_parser("inventory", help="Read account-wide counts without enrolling untracked PRs")
    commands.add_parser("stop")
    stats = commands.add_parser('stats', help='Read daily overall or per-project PR history')
    stats.add_argument('--since')
    stats.add_argument('--until')
    stats.add_argument('--days', type=int, default=30)
    scope = stats.add_mutually_exclusive_group()
    scope.add_argument('--project', help='Project name or ID; Unallocated is the default project bucket')
    scope.add_argument('--all-projects', action='store_true', help='Include overall and every project, including zero counts')
    stats.add_argument('--limit', type=int, default=1000)
    stats.add_argument('--offset', type=int, default=0)
    stats.add_argument('--json', action='store_true')
    stats.add_argument('--capture', action='store_true', help='Request an application-owned snapshot without querying GitHub')
    stats.add_argument('--wait', action='store_true', help='Wait for an explicitly requested capture')
    prs = commands.add_parser("prs", help="Read and filter locally tracked PRs")
    prs.add_argument("--project")
    prs.add_argument("--workstream")
    prs.add_argument("--search")
    prs.add_argument("--state", choices=("open", "closed", "merged", "all"), default="open")
    prs.add_argument("--limit", type=int, default=50)
    prs.add_argument("--offset", type=int, default=0)
    prs.add_argument("--json", action="store_true")
    register = commands.add_parser("register-pr", help="Submit an existing PR to the local tracker only")
    register.add_argument("url")
    register.add_argument("--project")
    register.add_argument("--workstream")
    register.add_argument("--description")
    register.add_argument("--key", default=None, help="Idempotency key to reuse on a retry")
    sessions = commands.add_parser('sessions', help='Read saved session references and their separate resume-action status')
    sessions.add_argument('url')
    sessions.add_argument('--verify', action='store_true', help='Check native saved-session identity without opening a conversation')
    intent = commands.add_parser("intent", help="Submit a typed local intent; never a GitHub write")
    intent.add_argument("kind", choices=("register_pr", "update_pr", "attach_session", "attach_slack", "remove_slack"))
    intent.add_argument("payload", help="JSON object")
    intent.add_argument("--key", required=True)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--wait", action="store_true")
    collect = commands.add_parser("collect")
    collect.add_argument("--since")
    collect.add_argument("--until")
    collect.add_argument("--sources", nargs="+", choices=("opencode", "slack", "github"))
    collect.add_argument("--no-wait", action="store_true")
    schedule = commands.add_parser("schedule", help="Configure application-owned job timing")
    schedule.add_argument("--archive-hour", type=int)
    schedule.add_argument("--archive-minute", type=int)
    schedule.add_argument("--refresh-seconds", type=int)
    schedule.add_argument("--automated-reviewer", action="append", dest="automated_reviewers")
    args = parser.parse_args(argv)
    root = args.state_dir.expanduser().resolve()
    try:
        if args.command == "serve":
            from .service import serve
            serve(root, jobs=not args.no_jobs)
            return 0
        if args.command == "mcp":
            try:
                from .mcp_server import run
            except ImportError as exc:
                raise RuntimeError("MCP SDK missing; install requirements.lock or use the installed work-tracker command") from exc
            run(root)
            return 0
        if args.command == "tui":
            try:
                from .tui import run
            except ImportError as exc:
                raise RuntimeError("Textual is missing. Install requirements.lock or reinstall work-tracker.") from exc
            run(root)
            return 0
        if args.command in ("status", "jobs", "inventory", "stop"):
            result = request(root, args.command)
        elif args.command == 'stats':
            if args.capture:
                queued = request(root, 'capture_statistics')
                result = wait_for_job(root, queued) if args.wait else queued
            else:
                if args.wait:
                    raise ValueError('--wait requires --capture')
                filters = {'days': args.days, 'project': '*' if args.all_projects else args.project,
                           'limit': args.limit, 'offset': args.offset}
                filters.update({name: getattr(args, name) for name in ('since', 'until') if getattr(args, name) is not None})
                result = request(root, 'statistics', filters)
                if not args.json:
                    print(f"Daily PR stats | history starts {result['first_recorded_day'] or 'not yet recorded'} | local calendar days")
                    print('Closure mean/p90 use that day\'s closures only; p90 uses nearest rank. Missing dates are not zero activity.')
                    if (result.get('collection') or {}).get('last_error'):
                        print(terminal_text('Statistics capture warning: '+result['collection']['last_error']))
                    for value in result['items']:
                        count_prefix = '' if value['closure_counts_complete'] else '>='
                        age = duration_text(value['oldest_open_age_seconds'])
                        if not value['oldest_open_complete']:
                            age = '>='+age if value['oldest_open_age_seconds'] is not None else 'Unknown'
                        print(terminal_text(f"{value['day']} | {value['project']} | tracked {value['total_tracked']} | "
                              f"closed {count_prefix}{value['closed_count']} | merged {count_prefix}{value['merged_count']} | oldest {age} | "
                              f"mean {duration_text(value['mean_closure_seconds'])} | p90 {duration_text(value['p90_closure_seconds'])} | "
                              f"timed {value['closure_sample_count']}/{value['completed_count']} | stale {value['stale_pr_count']} | "
                              f"missing dates O/C {value['missing_open_date_count']}/{value['missing_close_date_count']} | "
                              f"unknown state {value['unknown_state_count']}"))
                    print(f"{len(result['missing_days'])} date(s) have no recorded inventory snapshot.")
                    return 0
        elif args.command == "prs":
            result = request(root, "list_prs", {"project": args.project, "workstream": args.workstream,
                                               "search": args.search, "state": None if args.state == "all" else args.state,
                                               "limit": args.limit, "offset": args.offset})
            if not args.json:
                print(f"{result['total']} matching tracked PRs (showing {len(result['items'])}, offset {args.offset})")
                inventory = result.get('inventory') or {}
                if 'total_open' in inventory:
                    print(f"GitHub open: {inventory['total_open']} | tracked authored open: {inventory.get('tracked_open', '?')} | untracked estimate: {inventory.get('untracked_open_estimate', '?')}")
                    print(terminal_text(inventory['github_url']))
                    print(terminal_text(f"Inventory observed: {inventory.get('observed_at', 'unknown')}"))
                    if inventory.get('error'):
                        print(terminal_text(f"Inventory warning: {inventory['error']}"))
                for value in result["items"]:
                    print('\n' + terminal_text(f"{value['review_state']} | Since change: {state_age(value)} | {value['github'].get('title') or value['task_title']}"))
                    print(terminal_text(value["url"]))
                    print(terminal_text(f"{value['project_name']} / {value['workstream_name']} | {value['review_reason']}"))
                return 0
        elif args.command == 'sessions':
            value = request(root, 'get_pr', {'url': args.url, 'verify_sessions': args.verify})
            if value is None:
                raise ValueError('PR is not registered')
            result = {'url': value['url'], 'tracking-outcomes': value['tracking-outcomes'],
                      'agent-session-actions': value['agent-session-actions']}
        elif args.command in ("intent", "register-pr"):
            if args.command == "intent":
                payload, kind = json.loads(args.payload), args.kind
            else:
                payload = {"url": args.url, **{key: getattr(args, key) for key in ("project", "workstream", "description")
                                              if getattr(args, key) is not None}}
                kind = "register_pr"
            result = request(root, "intent", {"key": args.key or uuid.uuid4().hex, "kind": kind,
                                              "payload": payload, "source": {"client": "cli"}})
        elif args.command in ("refresh", "collect"):
            params = {key: getattr(args, key) for key in ("since", "until", "sources")
                      if hasattr(args, key) and getattr(args, key) is not None}
            queued = request(root, args.command, params)
            wait = args.wait if args.command == "refresh" else not args.no_wait
            result = wait_for_job(root, queued) if wait else queued
        else:
            config = {key: getattr(args, key) for key in ("archive_hour", "archive_minute", "refresh_seconds", "automated_reviewers")
                      if getattr(args, key) is not None}
            result = request(root, "configure", config) if config else request(root, "status")["config"]
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"work-tracker: {exc}", file=sys.stderr)
        return 1
