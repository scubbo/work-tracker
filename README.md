# Work tracker

Work tracker is a local application that owns a SQLite datastore and scheduled
jobs. The Textual TUI, CLI, and stdio MCP server call that application over a
private Unix socket. They do not open the database or run their own schedulers.
The TUI opens My Day for daily goals and persistent awareness items. F4 switches
to the open-PR workflow. Standalone project/workstream management remains a follow-up.

The first slice refreshes only PRs already ingested into the tracker. It supports
review/readiness status, local agent intent, project/workstream classification,
Slack links, and multiple agent-session references. Collection and refresh are
read-only. The TUI also offers explicit confirmation for PR closing with an optional
comment and head-pinned merging. MCP cannot create or modify GitHub PRs.

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
The default order is the full PR URL, ascending. Click a column header to sort.
Click it again to reverse the order, or press F2 to choose a column and direction.
Unknown dates stay last. The application saves the sort column and direction and restores them after TUI or backend restarts.
Filters still start clear. If a save fails, the TUI keeps the local order, warns, and retries after reconnection.
Press `s` to view saved agent sessions, including each tool, workspace, session ID, machine, and resume availability.
Choose `Activate open herdr pane` to find and focus an existing local pane that contains the saved session.
The application searches running local herdr sessions and requires an exact identity match. It never starts another conversation.
If no pane matches, use `Check and copy resume command`. Choose `Add session reference` only to attach another session.
Press `p` to copy the selected PR URL, including from the cached offline view.
On macOS, copying uses the system clipboard. Other platforms use terminal clipboard support.

Press `r` to request a GitHub refresh in the background application.
The TUI's three-second view poll only reads local data.
Closing it leaves the application and scheduled jobs running.

The Opened column shows the cached GitHub creation date in UTC (`YYYY-MM-DD`).
Full details show the complete opening timestamp. Missing or invalid dates show
`Unknown`. Titles and opening timestamps persist in local SQLite across restarts.
The TUI reads these cached fields, not GitHub, when it redraws the list.
A failed GitHub refresh retains the last known metadata and marks it stale.

The Since change column shows elapsed time in the displayed review/readiness
state. `≥` marks a lower bound since tracking began. Unknown history or stale
observations show `Unknown`. Ordinary refreshes do not reset the clock.

Press `x` on the selected PR to open Close PR. Enter an optional multiline
comment, then choose Close PR or press Ctrl+Enter to confirm the GitHub change.
Escape cancels before submission. Branches and the linked Task stay unchanged.
An uncertain request is not replayed. Reopen its dialog to check the same request.

Press `m` on the selected PR to open Merge PR. The application loads the current head commit and allowed merge methods.
Choose a method if several are available, then click Merge PR or press Ctrl+Enter to confirm.
The application only attempts an immediate merge when GitHub reports a clean, non-draft PR. It pins the request to the confirmed commit.
It does not enable auto-merge, bypass checks or merge queues, request branch deletion, or close the linked Task.
GitHub's repository policy for automatic head-branch deletion still applies.
If the outcome is uncertain, the dialog checks the same request without replaying a write.

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

## My Day

The TUI opens My Day by default. F4 switches between My Day and the PR list.
Use `work-tracker tui --view prs` to start directly with PRs. From that list, `a` links the selected PR's existing Task to today.

My Day separates daily goals, persistent awareness, and unfinished carry-forward candidates.
Add a goal with `a`, add awareness with `w`, and use Space to switch a goal between Open and Done.
Quick text creates a Task in the general Unclassified project/workstream. Existing Tasks and workstreams are searchable targets.
A day label can describe a specific outcome without renaming its underlying Task or workstream.
Completion affects only that day's item. It never closes a Task, workstream, or PR.

Awareness persists until explicitly dismissed. In the Carry forward tab, choose an unfinished prior goal and press `c`.
The application creates a new daily item linked to the same target, without modifying the source day or copying anything automatically.
After a carried goal is completed, its older unfinished ancestors are not suggested again.
Previous and Next browse read-only history. Today returns to the application's current local calendar day.
Midnight invalidates unsent old-day edits rather than assigning them silently to another day.

```sh
~/.local/bin/work-tracker day
~/.local/bin/work-tracker day add 'Finish the review' --key plan-review
~/.local/bin/work-tracker day add 'Watch the rollout' --awareness --key watch-rollout
~/.local/bin/work-tracker day targets --search 'review'
~/.local/bin/work-tracker day add --pr https://github.com/owner/repo/pull/123 --key plan-pr
~/.local/bin/work-tracker day done ITEM_ID --key complete-goal
~/.local/bin/work-tracker day --date 2026-11-05 --json
```

CLI output includes item IDs and versions. Use `reopen`, `edit`, `remove`, `dismiss`, or `carry` for explicit local changes.
Reuse the same key and payload after an uncertain response. Stale versions fail instead of overwriting another client's edits.
My Day is available through the TUI and CLI only. No My Day MCP tool is exposed, and planning never writes to GitHub.
Day plans stay in private application storage, not in Git or a separate TUI database.

## Daily PR statistics

Press F3 in the TUI to view daily history. Choose Overall, all projects, or one project.
The `Unallocated` bucket maps to the existing Unclassified project without changing saved classifications.
Known projects appear even when they have zero tracked PRs.

```sh
~/.local/bin/work-tracker stats
~/.local/bin/work-tracker stats --all-projects --days 30
~/.local/bin/work-tracker stats --project Unallocated --since 2026-11-01 --until 2026-11-30 --json
~/.local/bin/work-tracker stats --capture --wait
```

Each local calendar day records total tracked PRs, closures without a merge, merges, oldest observed open age, and mean/p90 closure time.
Closure time runs from GitHub creation to that day's closure or merge. A PR contributes its latest observed closure once per day.
Mean and p90 use only that day's completed PRs with valid timestamps. P90 uses nearest rank, and JSON durations use seconds.
A day without timed closures has no mean or p90, not zero. The table shows sample coverage, stale observations, and missing dates.
Counts marked `>=` are known lower bounds, not a claim of complete zero activity.

The application owns a startup snapshot and a daily job at 00:05 local time.
Refreshes, classification changes, ingestion, and successful close/merge actions also update today's provisional snapshot.
Past inventory and project membership stay fixed. Late GitHub closure observations can correct that day's closure metrics for its recorded members.
History starts with observed snapshots. Missed days remain gaps, and old inventory is not reconstructed from today's register.
Closing the TUI does not stop collection. Reading statistics does not refresh GitHub or create snapshots.

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

The installer also copies [the register-pr skill](skills/register-pr/SKILL.md) to
`~/.agents/skills/register-pr/SKILL.md`. It registers a PR through MCP or the CLI,
attaches verified creating-session identity, and reads back the saved record.
It preserves existing metadata and never invents missing session IDs.
It reports PR registration, reference attachment, and usable resume availability separately.
It requires a native read-only session lookup before reporting a usable local action.
Existing owned copies in `~/.fx/skills/register-pr` are updated during application installation, so an older copy cannot shadow the shared skill.

Include this instruction in an agent task:

```text
After creating the PR, use the register-pr skill to register it with work-tracker
and attach the verified creating session. Report any missing identity or tracking failure.
```

The broader `work-activity` skill remains available for reports and collection.
If a skill at the target path is unrelated or symlinked, installation refuses to overwrite it.

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
