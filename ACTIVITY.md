# Work activity

`work_activity.py` saves private evidence for daily and weekly work reports. It
uses the authenticated `opencode`, `slack-cli`, and `gh` CLIs.
The installed application and MCP adapter require Python 3.11 or later.
It does not call a model, post to Slack, or write to GitHub.

The collector, macOS installer, and schedule now live in the standalone
`work-tracker` repository. Run this command from that checkout:

```sh
.venv/bin/python install.py --load
```

This installs the local application under `~/.local/lib/work-activity` and
preserves `~/.local/bin/work-activity` as a compatible interface.
The application owns daily collection at 10:00 local time, including weekends.
It also owns periodic PR refresh and the datastore. launchd only starts the
application at login and restarts it after failure; it has no calendar schedule.

The application catches up after sleep or downtime using source checkpoints.
It does not run while the machine is off or the user is logged out.
The installer verifies the new application before retiring the old calendar job.
See `docs/application.md` in the source repository for migration details.

The installed copy does not depend on a temporary Git worktree. Re-run the
installer after changing the source. It resolves fnm's temporary executable links
to their installed versions. Reinstall after removing/upgrading those versions if
the installed command paths change.

## Collect and inspect

```sh
~/.local/bin/work-activity collect
~/.local/bin/work-activity status
~/.local/bin/work-activity snapshots \
  --since 2026-10-26T00:00:00Z --until 2026-11-02T00:00:00Z
```

Collection uses independent source checkpoints, initially 24 hours ago, with a
15-minute overlap on subsequent runs to allow for search-index delays. A partial
failure saves the successful sources and exits nonzero. Failed sources retain
their checkpoints and are retried on the next run. Per-session export failures
also prevent OpenCode's checkpoint from advancing. Captures use unique directories
and never overwrite an earlier capture. Concurrent CLI operations share a file lock.

Slack can print rate-limit retry notices before its JSON result.
The collector ignores only these known notices at the start of Slack output.
Other unexpected output or invalid JSON still fails collection for that source.

For older PRs, explicitly backfill a window. This does not move checkpoints backward:

```sh
~/.local/bin/work-activity collect --sources github \
  --since 2026-10-01T00:00:00Z --until 2026-10-26T00:00:00Z
```

GitHub discovers PRs **authored by the currently authenticated account and updated
in the requested window**, across repositories. It includes bot-created PRs authored
under that account; counts are not a measure of individual effort. Unchanged PRs
outside the first window need a backfill. Search pages are all read. Incomplete
results or more than GitHub's 1,000-result search limit fail visibly; use smaller
windows rather than accepting a truncated inventory. A previously registered PR
is retained when merged or closed.

OpenCode sessions are selected by update time, with active sessions included even
if they have not received a new prompt. Child sessions are included. Each has its
ID, title, directory, parent, location changes, timestamps, and a quoted reopen
command. If a worktree is later deleted, the archived transcript still exists
until its retention period ends; the reopen command may need an existing directory.

These are observations made at capture time, not a historical point-in-time API.
Backfilling an old window cannot recreate deleted Slack messages, deleted sessions,
old PR bodies, or a session's former transcript. An exported long-running session
can contain messages outside the report window. Filter by message timestamps when
writing a daily/weekly report. Avoid counting the same PR, session, or Slack message
more than once across overlapping captures.

## PR metadata

```sh
WA=~/.local/bin/work-activity
PR=https://github.com/owner/repo/pull/123

$WA pr set "$PR" --project 'Sandbox permissions' \
  --description 'Allow read-only Git credentials without granting write access'
$WA pr set "$PR" --source-session ses_example
$WA pr thread-add "$PR" \
  'https://workspace.slack.com/archives/C123/p1793390000000000' \
  --description 'Design discussion'

$WA pr show "$PR"
$WA pr list --project 'Sandbox permissions'
$WA pr count --by-project
$WA pr count --project 'Sandbox permissions' --state merged
$WA pr count --by-project --created-since 2026-10-01T00:00:00Z
```

After migration, `prs.json` is an export from SQLite, not an editable source.
The compatible CLI sends updates through the local application.
The application must be running. It does not fall back to direct JSON edits.

Legacy `project` labels are preserved without inferred hierarchy assignments.
New explicit classification uses the application's project/workstream tree.
`description`
is your explanation of the work; GitHub's body is stored separately. `slack-threads`
is a list of links, each with an optional description. Repeating `thread-add` is
idempotent and can update its description. Use `thread-remove PR_URL SLACK_URL`
to remove a link. Metadata commands change only the local register.

`source-session` contains the verified OpenCode ID, title, directory, and reopen
command. `pr set --source-session` fetches the exact session from OpenCode; never
guess a session ID from the directory. `--clear-source-session` clears the current
attribution; later collection can discover new evidence.

Automatic attribution is deliberately conservative: a completed native shell tool
must run a standalone `gh pr create …` and return only that PR's URL. A mention,
`gh pr view`, a shared directory, or a composite shell command is not proof.
Multiple candidate sessions remain ambiguous until set manually. Manual attribution
always wins. Creations through other tools need manual attribution. The register
also keeps matching links from your sent Slack messages under
`related-slack-messages`; these never overwrite curated thread links. Correlation
hints are retained across partial failures and GitHub search-index delays.

## Storage and retention

```text
~/.local/state/work-activity/
  tracker.sqlite3                  # canonical register, intent audit, and jobs
  app.sock                         # local application endpoint
  prs.json                         # compatibility export after SQLite migration
  correlations.json                # source-session and Slack link evidence
  checkpoints.json                 # independent of the summary skill's checkpoint
  snapshots/YYYY-MM-DD/<run>/
    manifest.json                  # capture time, source windows and errors
    sessions.json                  # session index and reopen commands
    transcripts/<session-id>.json.gz
    slack.json                     # your sent messages, including accessible private/DM messages
    github.json                    # PR discovery results
    prs.json                       # register as observed at this capture
  logs/stdout.log
  logs/stderr.log
```

Only full transcript archives are automatically deleted, once their capture is
older than **30 days**. Retention runs during collection. Lightweight indices,
PR metadata, Slack snapshots, and correlation evidence are retained. Expired
transcript paths remain in the indices as historical references. No files in
OpenCode's own storage are modified or deleted.

Files are owner-only (`0600`) inside private directories (`0700`). Full exports
can contain secrets and private work data. They are intentionally not sanitized:
OpenCode's sanitizer removes the message text and paths needed for this feature.
Keep this directory out of Git, public gists, and shared/cloud folders. Machine
encryption and company data-retention policies still apply. Slack searches use
your existing CLI login; there is no desktop-token extraction in this collector.

`--state-dir PATH` (before the subcommand) overrides the archive root.
`XDG_STATE_HOME` and `XDG_CONFIG_HOME` are respected by the collector. The macOS
installer uses the default home-directory paths.

Check the service and logs:

```sh
launchctl print gui/$(id -u)/com.scubbo.work-tracker
tail -n 40 ~/.local/state/work-activity/logs/stdout.log
tail -n 40 ~/.local/state/work-activity/logs/stderr.log
```

Stop the application and its jobs without deleting evidence.
The CLI and MCP interfaces become unavailable until the application restarts.

```sh
launchctl bootout gui/$(id -u)/com.scubbo.work-tracker
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```
