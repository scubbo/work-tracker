# Local application runtime

Jack approved the background application, application-owned storage and jobs,
local-only agent intent, and login/failure supervision. The instruction was:
"Yup, that's acceptable. Keep building."
Open PR tracking is the first workflow. The Textual TUI is a separate client
of the application, documented in [the TUI guide](tui.md).

## Ownership

`work_tracker.service.Application` owns the storage interface and job scheduler.
`SQLiteStorage` implements transactional tracker operations. The Textual TUI,
CLI, and stdio MCP adapter send requests through `app.sock`; they never open the
database. TUI metadata edits use atomic local-field preconditions to reject
concurrent changes, with stable intent keys for uncertain-response retries.
A private file lock admits one application or legacy collector per datastore.
The Unix socket and database use owner-only permissions inside a private directory.
There is no TCP listener or unauthenticated network endpoint.

MCP uses the [official SDK stdio transport](https://py.sdk.modelcontextprotocol.io/run/).
Each agent can launch its own adapter. All adapters reach the same application.
Tests use the [official SDK client](https://py.sdk.modelcontextprotocol.io/client/)
and a legacy 2025-11-25 initialization exchange.
No existing agent configuration is changed automatically.

Agent calls record local intent, with an idempotency key and source label.
Intent never establishes approval, CI success, merge state, or GitHub authorship.
The application accepts no arbitrary shell commands. A separate confirmed-close
operation posts an optional comment and closes one ingested PR on GitHub.
No MCP tool exposes this operation, and local intents cannot request it.
A failed connection is not permission to create a second local database.

## Private files

The default root remains `~/.local/state/work-activity`.

| File | Purpose |
| --- | --- |
| `tracker.sqlite3` | Canonical tracker data, hierarchy, links, intent audit, and job records |
| `app.sock` | Private application endpoint |
| `.lock` | Single-owner process lock, also excluding older collectors |
| `application.json` | Migration marker |
| `app-config.json` | Application-owned job timing and excluded automated reviewers |
| `prs.json` | Compatibility export after migration |
| `backups/legacy-prs-*.json` | Exact pre-import register backups |
| `backups/legacy-scheduler-*.plist` | Retired OS calendar artifacts |
| `snapshots/`, `checkpoints.json`, `correlations.json` | Existing private archive and source recovery data |

Imports retain every legacy metadata field. Existing project labels are kept
without guessing workstream relationships. Imported Tasks begin in Unclassified.
The database schema is versioned. Unsupported future versions fail without a
schema rewrite. Schema 2 also preserves observations from an interrupted,
over-broad discovery attempt in an `untracked_cache` table. They are not active
tracker entries and are not refreshed. Explicit ingestion can restore a cached
PR. The one-time correction retains original enrollments, all explicit intents,
and any record with evidence of local customization. It preserves cached rows
verbatim and takes a private database backup before changing the active set. Migration does not delete old snapshots or change retention.
Do not edit `prs.json` after migration; submit intent through an interface.

## Persistent PR sort

The application stores the PR sort under `tui_pr_sort` in SQLite's existing `settings` table.
This adds no schema version or migration. It does not change PR metadata, compatibility exports, schedules, or jobs.
The private `get_pr_sort` operation returns the saved column and direction, or the existing URL-ascending default.
`set_pr_sort` accepts exactly `field` and boolean `descending`. Unknown fields and extra parameters are rejected before writing.
Each application state directory owns its preference. No new MCP tool or client-owned datastore is required.

The TUI reads the preference on startup. A late read cannot replace a newer user selection.
Header clicks and F2 changes save through the private API. One writer serializes rapid changes and retains the latest pending choice.
Failed saves keep the local order, warn, and retry after a successful connection. Normal quit waits for the save attempt.
Existing TUI windows retain their current sort. A fresh window reads the most recently saved choice.
Filters are not persisted by this feature.

## Session action projection

Session references retain the four-field identity contract. `source-session` remains legacy OpenCode provenance.
`SessionActions` derives `agent-session-actions` and `tracking-outcomes` in application responses without changing stored references or the database schema.
`get_pr` accepts `verify_sessions: true` for native read-only lookups. MCP exposes the same option, and the CLI provides `sessions PR_URL --verify`.
Intent receipts distinguish reference attachment from resume availability. Attachment does not prove PR authorship.

Only explicit fx and OpenCode adapters construct commands. Native lookups use argument arrays, a recorded working directory, bounded execution, and regular-file JSON capture.
They do not evaluate stored commands, resume conversations, or send model requests.
Another machine never receives a local native lookup. Its command remains labeled as remote instructions.
Successful checks last five minutes in the running process. The TUI explicitly rechecks before copying a local resume command.
The user chooses when to run copied commands in a terminal.

The `activate_session_pane` private operation accepts a PR URL and an exact attached four-field reference.
The read-only `find_session_pane` operation applies the same attachment checks without changing focus.
Use this operation to test discovery inside the running application's environment, not only from an interactive shell.
`HerdrSessions` searches every running session from `herdr session list --json` through explicitly routed native commands.
The operation requires a local machine, matching workspace, tool, and exact live session identity. It never resumes or starts an agent.

Herdr can report the identity directly. The fx fallback checks that one foreground fx process holds both v2 session files open.
Those files are `~/.fx/sessions/v2/SESSION_ID/lock` and `log.jsonl`. This fallback requires `lsof` and the default fx profile.
A process can host several fx instances. Its current directory alone does not identify every loaded session.
The operation rechecks the matching pane before focus and checks the native focus receipt and resulting selection.
`herdr agent focus` returns `agent_info` with a nested `agent` object, not the `pane_focused` receipt from another operation.
The receipt must identify the expected pane and tool with `focused: true`. A subsequent snapshot must retain that pane selection.
Missing, ambiguous, remote, changed, or unverified targets never trigger an arbitrary pane selection. The copy-resume controls remain available.
Native calls use argument arrays, regular-file capture, and a shared deadline shorter than the client timeout.
The TUI only requests this operation. It does not inspect processes or control herdr directly.

## Daily PR statistics

Schema 5 adds daily inventory membership, observed closure events, and aggregate rows to the application-owned SQLite database.
The `statistics` private operation reads a bounded local-date range overall, per project ID/name, or across all project groups.
The `capture_statistics` operation queues the application's `daily_stats` job. It never queries GitHub itself.
The scheduler captures a startup baseline and then runs at 00:05 local time, with existing retry, restart, and timezone handling.
Refresh, archive ingestion, explicit PR classification, and confirmed lifecycle actions also update the current day.

Daily windows use local calendar midnights with stored UTC boundaries and timezone context, including 23-hour and 25-hour DST days.
The current row is provisional. Prior snapshots retain their last observed inventory membership and project IDs and names.
The default Unclassified project appears as Unallocated without rewriting the hierarchy. Every known project gets a row, including zero counts.
Closure events survive reopening. A PR contributes only its latest observed terminal event within each day's cohort.
Late closure observations revise closure metrics only where a membership snapshot exists. They do not add later-ingested PRs to earlier inventory.

Mean and nearest-rank p90 use creation-to-closure durations from PRs completed during that day, not cumulative completions.
Invalid, naive, negative, missing, or future timestamps never become invented zero durations.
Sample counts, undated events, stale observations, unknown states, and oldest-age completeness travel with each row.
Unavailable observations mark closure counts incomplete. Clients show known counts as lower bounds and expose timestamp coverage.
Missing days stay gaps. Clock rollback cannot overwrite newer snapshots. Timezone changes never relabel an existing day's UTC window.

Reads do not trigger capture, mutate PR metadata, or fetch GitHub. The TUI and CLI never open SQLite or run collectors.
The scheduler remains application-owned. launchd does not receive a calendar or interval timer for statistics.

## Explicit PR merging

Schema 4 adds a private merge-request ledger. Migration preserves the existing register, links, preferences, jobs, and close-request ledger.
The `merge_preview` private operation reads the current head commit, draft state, merge readiness, and repository merge methods from GitHub.
`merge_pr` requires explicit human confirmation, the exact previewed head SHA, an allowed method, and a stable request key.
The application checks those conditions again before a REST merge request with GitHub's `sha` precondition.
Only a clean, non-draft PR qualifies for an immediate merge. Required checks, approvals, and queue requirements cannot be bypassed through this action.

The operation never enables auto-merge, enrolls a PR in a merge queue, requests branch deletion, or closes a Task.
GitHub's repository policy for automatic head-branch deletion still applies.
A confirmed merge updates cached lifecycle state and protects it against older refreshes through observation versions.
Repeated keys return the original receipt. Lost or interrupted writes remain uncertain and are never replayed automatically.
`merge_result` can check an uncertain outcome through read-only GitHub calls. Confirmation requires the expected head commit.
Pending or uncertain close and merge requests exclude each other for the same PR. None of these operations are MCP tools or agent intents.

## State timing and explicit PR closing

Schema 3 adds state timing, observation versions, and a private close-request ledger.
The first timed observation provides a lower bound. A later observed review or
lifecycle transition sets the change time. Repeated observations and reason-only
changes keep that time. Refresh failures hide the age without inventing a change.
Timing reflects local observations, not an exact reconstructed GitHub timeline.

The application persists a confirmed close request before any external write.
It checks that the PR is ingested and still open, posts a nonblank comment,
then closes the PR. It does not delete branches or close the linked Task.
If a response is lost, the same request key returns its recorded outcome.
A comment can succeed even if the later close fails. Such outcomes are uncertain
and are never replayed automatically. Interrupted requests remain uncertain after
restart. Reopening the dialog retains its key and comment for a read-only check.
If the outcome remains uncertain, inspect or finish the close on GitHub.

Observation versions prevent an older refresh or archive from overwriting facts
that a concurrent close already updated. Ordinary jobs remain read-only and can
retry. Close requests are not scheduled jobs and never enter that retry loop.

## Scheduling

The application owns both job definitions and execution.
The archive job runs daily at 10:00 in the machine's local timezone by default.
The PR refresh job runs every 300 seconds by default. Manual calls queue those
same jobs rather than start a separate writer.

Job state persists in SQLite. A run is marked running before execution, then
records its result and next due time. An interrupted run is retried after restart.
External collection uses read-only APIs and unique archive directories, so a
crash can leave another archive observation, not duplicate GitHub mutations.
The scheduler does not claim exactly-once execution across a crash.

The next daily deadline uses local calendar dates, not a fixed 24-hour interval.
Timezone changes recalculate a pending local deadline. One catch-up run covers
downtime; source checkpoints determine the actual missing collection window.
A failed or partial job retries after 60 seconds, with backoff capped at one hour.
Requests for an already-running ordinary refresh are coalesced.
An explicit collection window is rejected while another collection runs.

The initial worker executes jobs serially. Long archive collection can delay PR
refresh, but it does not block local metadata requests. Stop waits for active
work; launchd can terminate the process after its exit timeout. Persistent running
state allows recovery after that interruption.

## GitHub observations

Refresh targets only records already ingested into the register. Account-wide
open PR counts never enroll untracked PRs or fetch all of their details.
Explicit agent/CLI submissions and the existing archive ingestion are enrollment
paths. Registered closed PRs can also be checked for reopening.
The application never closes a Task merely because its PR merged.

Tracked PRs are read in GraphQL batches of at most 20, with typed variables and
query operations only. Known closed PRs do not request review connections.
A capped review connection falls back to complete review pagination for that PR.

A separate lightweight lookup reads the account-wide total-open count and a
GitHub search link. `work-tracker inventory` exposes that summary.
The untracked-open estimate subtracts the latest confirmed tracked-open count.
It is not an independently enumerated untracked inventory and can be stale when
tracked observations fail. Errors and observation times remain visible.

Review pages are fetched read-only. Only current human opinions count toward
review state. Dismissals remove old approval. GitHub Bot accounts, `[bot]` names,
and configured automated reviewers do not count as human review.
A normal User account used for automation cannot be identified reliably by API
type alone. Add it to `automated_reviewers` through the schedule command.

Ready to Merge requires human approval, satisfied GitHub review requirements,
non-draft status, a confirmed clean/mergeable result, and successful known checks.
This deliberately blocks uncertain or optional failing checks in the first slice.
It does not implement a detailed CODEOWNERS explanation engine.
A failed refresh retains the old observation but marks it stale and Blocked.
A newly discovered PR whose details cannot be fetched remains visible as unverified.
Tracker search excludes archived Slack message bodies and agent transcripts.

## Service migration

The installed label is `com.scubbo.work-tracker`.
Its plist uses RunAtLoad and restart-on-failure supervision only.
It contains neither StartCalendarInterval nor StartInterval.
The application, not the plist, contains the job times.

The installer waits for application readiness before unloading
`com.scubbo.work-activity` and backing up its calendar plist.
If readiness fails, the old calendar registration is not removed.
A failed migration still needs operator attention: if SQLite or the migration
marker exists, the compatible CLI refuses unsafe JSON fallback.
Do not delete the database or marker to force fallback.

Installation refuses migration while the legacy collection is active.
Reinstallation also refuses while an application job is active.
Private data is not discarded to repair a failed startup.
After a successful cutover, only the application scheduler remains active.

Read health and job state with:

```sh
~/.local/bin/work-tracker status
~/.local/bin/work-tracker jobs
launchctl print gui/$(id -u)/com.scubbo.work-tracker
```

If the application is stopped, start its supervisor with:

```sh
launchctl kickstart gui/$(id -u)/com.scubbo.work-tracker
```

## Verification boundary

The test suite covers storage transactions, concurrent intents, migration,
review gating, clocks/DST, recovery, supervisor cutover, private RPC, the compatible
CLI, and actual stdio MCP exchanges. It uses fake external service responses.
A live deployment check is separate from this deterministic suite.
The Open PR TUI is implemented and uses local-view polling only.
My Day, a remote backend, and automatic session discovery remain follow-ups.

Dependency installation checks exact installed versions before using a registry.
It can reuse the local uv cache in strictly offline mode. Otherwise it honors
the configured pip registry and reports failures without trying another online
index. The current machine's pip registry returned 401 during setup; its
configuration and credentials were not changed. Cached dependencies allowed
installation to proceed without another registry request.
