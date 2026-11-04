# First application slice

Jack approved a local background application, SQLite behind an interface,
application-owned scheduling, and local-only MCP intent signals.
He accepted launchd for login and failure supervision, then said to keep building.
Open PR tracking is the first workflow. Textual was selected in the next
checkpoint and the first Open PR interface is now implemented.

During implementation, Jack clarified that routine refresh only needs records
already ingested into work-tracker. Account-wide open and untracked counts are
separate summary features. They must not enroll all account PRs.

## Implemented

The application owns the SQLite datastore, private Unix socket, durable job
records, daily 10:00 local-time archive job, and periodic PR refresh.
The CLI, compatible work-activity command, and stdio MCP adapter call it.
No interface opens its own database or creates a separate scheduler.

The MCP tools submit or update local intent, attach Slack/session references,
read records, and request read-only refresh. Mutations have idempotency keys.
Intent cannot set GitHub merge, review, or CI facts. No GitHub write operation
is exposed. Existing agent configuration was not changed automatically.

Tracked PR facts are read in batches of at most 20. Account-wide inventory uses
a lightweight count lookup and returns a GitHub link. The untracked-open count
is explicitly an estimate based on the latest tracked observations.

The initial broad backfill was stopped after the tracked-only clarification.
Original enrollments and explicit intents were retained. Extra observations
were moved verbatim into a private untracked cache, with a database backup.
The correction preserved the active register and isolated the extra observations.
No PR facts were discarded or published during that correction.

## Verification

Run the complete deterministic suite, including MCP subprocess tests, with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

At the headless checkpoint, the complete suite passed 68 tests with exit code 0.
It covers migration, data preservation, transactional concurrent reads and
writes, intent validation/retries, review gating, application-owned clocks,
DST/timezone changes, recovery, private socket ownership, compatible CLI routing,
old and new MCP protocol exchanges, and supervisor cutover ordering.

The installer command is:

```sh
.venv/bin/python install.py --load
```

Live verification established that the application owns SQLite, the installed
LaunchAgent has no calendar/interval trigger, the old calendar job is retired,
and the installed stdio MCP adapter reaches the application through eight tools.

A tracked-only live refresh completed with exit 0, no per-PR errors, and all
ingested records refreshed:

```sh
~/.local/bin/work-tracker refresh --wait
~/.local/bin/work-tracker inventory
```

Inventory totals are timestamped observations, not permanent counts or a full
untracked PR inventory. Global account totals do not drive enrollment.
Personal activity counts are omitted from these public notes.

Two deployment issues were diagnosed before retrying. The idle guard now reads
the job-level active count rather than a nested resource coalition count.
Activation waits for the previous job and datastore owner to stop before
bootstrapping a replacement. A live normal upgrade then completed with a new
application PID, unchanged tracked-record count, and unchanged persisted job
deadlines. The installer and upgrade checks exited 0.
Neither fix requires elevated privileges.

The configured pip registry returned 401 during dependency setup.
Credentials and registry settings were left unchanged. The tested pinned
packages were available locally, so deployment reused the offline cache.
A fresh machine without those packages still needs working registry access.

## Textual PR client checkpoint

Jack approved Textual and requested implementation of the TUI.
The first Open PR client is implemented and installed as `work-tracker tui`.
It supports search, project/workstream/review filters, live details, context
links, local metadata/classification edits, explicit registration, and manual
Slack/session references. My Day and standalone hierarchy management remain later
views. See `tui.md` for controls and boundaries.

At the first TUI checkpoint, the complete suite passed 85 tests with exit code 0 using:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

This includes headless Pilot interaction, narrow-terminal layout, polling
coalescing, edit conflicts, uncertain-save retry, pagination beyond 200 rows,
and a real isolated application-socket editor test.
The installed TUI was also launched from `/tmp` in a real terminal and quit with
`q`, exit 0. Closing it left the background application running.
The installer passed and preserved the register count and job configuration.
The installed MCP server still exposed eight working tools after the update.

The configured registry could not supply Textual because authentication returned
401. Jack explicitly approved a public PyPI download into this project's virtual
environment. Existing SDK pins and the two-day package-age cutoff were preserved.
Application deployment then reused those exact packages from the offline cache.
Company registry settings and credentials were not changed.

## Metadata completion checkpoint

The editor and registration forms now prompt with stored project/workstream
names. Right Arrow accepts inline completion. Workstreams are scoped to the
entered project, and changing that project preserves the workstream draft.
New names remain allowed. Unambiguous case-insensitive exact matches reuse
canonical spelling rather than create another capitalization variant.

The read-only application catalog includes branches with no open PRs.
The TUI never opens the database for suggestions. A failed lookup leaves manual
entry available with an explicit warning. A late lookup cannot reopen a cancelled
editor. No external service call is needed to retrieve these names.

The complete suite passed 93 tests, exit 0, with the same command above.
The installer passed and preserved tracked-record count and job configuration.
A read-only installed-editor smoke test against the live application verified
Right Arrow completion without submitting a save or stopping the background app.

## Remaining work

The first Open PR TUI is implemented in Textual, backed by the same local
application as CLI and MCP. My Day and broader project/workstream management
views remain unimplemented. Detailed CODEOWNERS explanations, tags, agent hooks, session
reopening, full conversation search, remote storage, and multi-user work remain
follow-ups from the interview.

Tests drive clock boundaries deterministically. Verification did not wait for
a natural future 10:00 run.

Jack later requested the initial commit and push, and selected a public
`scubbo/work-tracker` repository. Publication excludes private activity,
credentials, runtime state, and local scan reports. No license is selected.
