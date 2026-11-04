# Work tracker

Work tracker is a local application that owns a SQLite datastore and scheduled
jobs. The Textual TUI, CLI, and stdio MCP server call that application over a
private Unix socket. They do not open the database or run their own schedulers.
The first TUI covers open PR tracking. My Day and standalone project/workstream
management views remain follow-ups.

The first slice refreshes only PRs already ingested into the tracker. It supports
review/readiness status, local agent intent, project/workstream classification,
Slack links, and multiple agent-session references. GitHub access is read-only. MCP cannot create or modify GitHub PRs.

Read [the interview decisions](docs/requirements.md) for the full planned scope.
Read [the runtime guide](docs/application.md) for ownership and operational details.
Read [the collector guide](ACTIVITY.md) for archive coverage and retention.

## Set up and test

Use Python 3.11 or later. The core application uses the standard library.
The MCP adapter uses the official Python SDK. The TUI uses Textual.
Both are pinned in `requirements.lock`.
Tests use temporary state and fake service responses, not live models.

```sh
git clone https://github.com/scubbo/work-tracker.git
cd work-tracker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v
```

Run the application from a source checkout:

```sh
.venv/bin/python tracker.py serve
```

In another terminal, use the CLI:

```sh
.venv/bin/python tracker.py status
.venv/bin/python tracker.py prs
.venv/bin/python tracker.py refresh --wait
```

The `--state-dir` argument goes before the subcommand. Use it to isolate test
state. Clients do not automatically create another application process.

## Install on macOS

The collector uses the authenticated `opencode`, `gh`, and `slack-cli` commands.
The installer also resolves `node` on `PATH` for the Slack CLI.

```sh
.venv/bin/python install.py --load
```

The installer copies the application into `~/.local/lib/work-activity`, prepares
an application-owned Python environment, and installs both `work-tracker` and
the compatible `work-activity` command under `~/.local/bin`.
The installed code and dependencies do not need the source checkout at runtime.
The installer first checks installed package versions, then tries an existing
uv cache without network access. If dependencies are still missing, it uses the
user's configured pip registry. It never changes registry settings or credentials.
A fresh install without cached packages needs working registry access.

`com.scubbo.work-tracker` starts the application at login and restarts it after
failure. Its LaunchAgent contains no calendar or interval schedule.
The application owns the daily 10:00 local-time archive job and PR refresh job.
The default PR refresh interval is five minutes. Both jobs also accept manual
requests. Closing a CLI or MCP client does not stop the application.

During migration, the installer verifies the new application before removing
the legacy calendar job. It retains the old plist in the private backup directory.
Existing JSON PR data is imported once, with an exact private backup.
Archives, source checkpoints, and curated metadata remain outside Git.
Use `--load` to complete migration of an existing calendar-based installation.
Without it, installation writes artifacts but does not finish the service cutover.

## Open the TUI

The local application must already be running. Launch the interface with:

```sh
~/.local/bin/work-tracker tui
```

From a source checkout, use `.venv/bin/python tracker.py tui` instead.
The view includes search, project/workstream/review filters, a live detail pane,
and separate account-wide inventory counts. Enter opens full details.
Project and workstream buttons scope the PR list to related records.

Use Edit for local description and classification, Track PR for explicit
ingestion, and Links for Slack or multi-tool agent-session references.
Project and workstream inputs suggest stored names. Type a prefix and press
Right Arrow to complete it. Workstreams follow the entered project.
New names remain allowed.
Press `r` to request a GitHub refresh in the background application.
The TUI's three-second view poll only reads local data.
Closing it leaves the application and scheduled jobs running.

See [the TUI guide](docs/tui.md) for controls, retry/conflict handling, and limits.

## Track PRs

```sh
~/.local/bin/work-tracker prs
~/.local/bin/work-tracker prs --project 'Platform' --workstream 'Access'
~/.local/bin/work-tracker prs --search 'credentials' --json
~/.local/bin/work-tracker register-pr https://github.com/owner/repo/pull/123 \
  --project 'Platform' --workstream 'Access' --description 'Why this matters'
~/.local/bin/work-tracker refresh --wait
~/.local/bin/work-tracker jobs
~/.local/bin/work-tracker inventory
```

Account-wide total-open counts and a GitHub search link are separate from tracking.
The inventory lookup does not enroll untracked PRs or issue per-PR detail requests for them.
The untracked-open number is an estimate derived from the account total and the
latest tracked observations. The count includes its observation time.

A new PR gets a stable Task under the general Unclassified project/workstream.
Imported free-text project labels are preserved as metadata, not guessed into
the new hierarchy. Explicit classification moves the Task to the chosen branch.

The readiness display distinguishes Awaiting review, Changes requested,
Ready to Merge, and Blocked. Automated accounts do not count as human approval.
Unknown readiness and failed refreshes never produce a fresh Ready to Merge claim.
The initial policy is conservative about CI, including optional failing checks.
Automation using a normal GitHub user account must be explicitly excluded with
`work-tracker schedule --automated-reviewer ACCOUNT`.

The `work-activity` CLI remains available for daily-summary integrations.
After SQLite migration it calls the application. It does not silently fall back
to editing JSON if the application is unavailable. `prs.json` becomes a
compatibility export, not the authoritative register.

## Configure local agents

The local MCP launch command is:

```sh
/absolute/path/to/.local/bin/work-tracker mcp
```

Use your agent client's MCP registration format. For clients with an
`mcpServers` configuration, the entry has this shape:

```json
{
  "mcpServers": {
    "work-tracker": {
      "command": "/absolute/path/to/.local/bin/work-tracker",
      "args": ["mcp"]
    }
  }
}
```

The application must already be running. The MCP adapter exposes `submit_pr`,
`update_pr`, `attach_agent_session`, `attach_slack_thread`, `list_prs`, `get_pr`,
`refresh_prs`, and `application_status`.
Mutating tools require an `intent_key`. Reuse it for an identical retry.
Reusing a key for different content fails rather than overwriting an earlier intent.
Session links require explicit `tool`, `path`, `sessionId`, and `machine` values.
The installer does not rewrite existing agent configuration.

## Application-owned scheduling

```sh
~/.local/bin/work-tracker schedule
~/.local/bin/work-tracker schedule --archive-hour 10 --archive-minute 0 --refresh-seconds 300
~/.local/bin/work-tracker collect --sources github
```

Timing is saved in the private application's configuration and job records.
Failed jobs retry with backoff. Interrupted jobs recover on restart.
Daily jobs catch up once after downtime instead of replaying every missed day.
Source checkpoints retain the collection window after partial failures.
The runtime guide describes the migration and recovery limits.

## Private data

Keep runtime state outside this checkout. The default location is
`~/.local/state/work-activity`. Archives can contain private messages, agent
conversations, and credentials copied into those conversations.

The repository contains code, documentation, and synthetic test data only.
Ignore rules exclude runtime exports, backups, databases, logs, and common
credential files. Do not force-add ignored files or commit real activity as fixtures.

No license is selected for this project.
