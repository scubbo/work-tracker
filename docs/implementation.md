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

## State timing and Close PR checkpoint

Jack requested elapsed time since state changes and a one-key Close PR dialog
with a comment box. The list and CLI now display an observed state age.
First tracking gives a lower bound, not a reconstructed GitHub event time.
Ordinary refreshes preserve the timestamp, while stale observations hide the age.

The `x` shortcut opens an immutable-target confirmation dialog with an optional
multiline comment. The application owns the GitHub operation. It records each
request before sending a comment or close, and never replays uncertain writes.
A reopened pending dialog retains the original key and comment.
Branches and linked Tasks stay unchanged. No MCP write tool was added.

The complete suite passed 117 tests, exit 0. Focused checks covered state timing,
schema migration, duplicate requests, interrupted operations, keyboard routing,
lost responses, cancellation, immutable targets, and small-terminal layout.
A private-copy migration preserved every old table row and field.
A private SQLite backup was taken before installation. Installation preserved
tracked records and schedule configuration, with installed code matching source.
A live read-only refresh initialized the timing anchors.
The installed TUI displayed the new column and opened/cancelled Close PR under a
read-only RPC guard. No real GitHub comment or close was performed for testing.

## Remaining work

The first Open PR TUI is implemented in Textual, backed by the same local
application as CLI and MCP. My Day now provides local daily planning through the
TUI and CLI. Broader project/workstream management views remain unimplemented. Detailed CODEOWNERS explanations, tags, agent hooks,
additional session adapters, internal-instance selection, full conversation search,
remote storage, and multi-user work remain follow-ups from the interview.

Tests drive clock boundaries deterministically. Verification did not wait for
a natural future 10:00 run.

Jack later requested the initial commit and push, and selected a public
`scubbo/work-tracker` repository. Publication excludes private activity,
credentials, runtime state, and local scan reports. No license is selected.

## Sorting, clipboard, and session actions checkpoint

The PR view retains ascending full-URL order by default. Header clicks and F2
select a column and direction. Unknown dates stay last, and refreshes preserve
sorting and selection. The `p` shortcut copies the selected PR URL. macOS uses
`pbcopy` with standard input, without shell interpolation.

Session reference attachment and usable resume availability are separate outcomes
in RPC, MCP, CLI, full details, and the canonical register-pr skill. The application
derives commands from the existing identity fields. Explicit fx and OpenCode
adapters perform native read-only identity checks before a local action is ready.
Foreign machines receive labeled instructions without local inspection. Unknown
tools and incomplete or unsafe identities retain their references without a usable action.
Legacy OpenCode source-session records remain intact, and stored commands are ignored.

The fx command shape was confirmed through installed help and the
[fx session documentation](https://fx.sh/docs/using-fx/sessions).
The application never resumes a conversation during verification. The UI checks
and copies a command for the user to run in the recorded workspace.

Verification: `.venv/bin/python -m unittest discover -s tests -v` passed 146 tests,
exit 0. The six socket/MCP interface checks also passed after adding the explicit
verification option to the SDK roundtrip. Temporary-home installer tests cover
updates to the shared skill and an existing owned fx copy, without replacing an
unrelated skill.

Installation with `.venv/bin/python install.py --load` passed, exit 0. A private
read-only RPC snapshot confirmed preservation of all saved PR metadata and schedule
configuration. Installed modules and both installed skill copies matched source.
The installed CLI and actual UI copy button confirmed an existing fx session,
with exactly one original attachment. The installed UI also sorted and copied the
PR URL. A captured clipboard avoided overwriting the user's clipboard. No real
agent conversation was launched and no GitHub write was used.

## PR workflow and daily statistics checkpoint

The Opened column uses cached creation timestamps. Sorting now persists through
application-owned preferences. The session view shows saved references and safe
copy-resume actions. Its activation button identifies and focuses a matching
existing local herdr pane without starting another agent.

The `m` shortcut opens a merge confirmation for the previewed head commit and an
allowed method. Schema 4 adds a durable merge ledger, restart recovery, and checks
that prevent conflicting pending close and merge operations.
MCP remains intent-only. No live GitHub merge was used for verification.

Schema 5 adds daily snapshots, observed closure events, and overall/per-project
statistics. Mean and nearest-rank p90 use each local day's closure cohort.
Unallocated is a separate bucket, including zero counts. Historical membership
stays fixed, and missing days remain gaps rather than reconstructed inventory.
The application owns daily collection. F3 and `work-tracker stats` read it.

The complete deterministic suite passed 234 tests with exit 0. Installed CLI,
private API, TUI controls, native herdr acknowledgments, and automatic statistics
capture passed focused checks. Verification preserved private metadata, links,
hierarchy, pending actions, scheduling configuration, and sort preferences.
Public notes omit live PR records, session identities, and activity totals.

## My Day checkpoint

Jack selected My Day as the home view, simple Open/Done goals, and TUI/CLI access only.
Schema 6 adds day items and an idempotency ledger without changing existing PR, statistics, reference, or operation records.
Quick-add creates a Task under Unclassified. Existing Tasks, workstreams, and tracked PR Tasks can be linked directly.
Daily completion and label edits do not close or rename their targets. Awareness persists until dismissed.

Carry-forward is explicit and preserves its source day. Latest completed or removed descendants suppress stale older suggestions.
History is read-only. The application calendar rejects unsent stale-day writes, while exact recorded retries remain valid across midnight.
Item versions reject concurrent stale edits. Task creation, plan changes, and receipts use one transaction.
My Day exposes no MCP tools and performs no GitHub mutations.

The PR list remains available through F4 or `tui --view prs`. Its `a` shortcut links the selected PR's existing Task.
Tests cover the default home view, literal labels, target selection, daily versus persistent state, explicit carry-forward,
unknown responses, unchanged request keys, midnight changes, version conflicts, migrations, and the real application socket.
Synthetic planning tests use isolated temporary state. Verification does not seed the live user's day plan.
The complete deterministic suite passed 257 tests with exit 0.

## My Day Task classification

The initial quick-add flow always used Unclassified, and Edit changed only the day label.
Project/workstream assignment is now optional for new Tasks and explicit in Edit for existing Task targets.
Known-name completion follows the PR metadata editor. Linking an existing target keeps its classification until a separate explicit edit.
The CLI supports `day add --project NAME --in-workstream NAME` and `day classify ITEM_ID --project NAME --workstream NAME`.

Task moves check both the day-item version and its observed workstream ID. Stale attempts cannot create stray hierarchy branches or overwrite another assignment.
Local PR metadata and the current statistics snapshot stay consistent with the shared Task. Day labels, lifecycle state, and historical statistics grouping stay unchanged.
Synthetic tests cover named creation, shared reassignment, conflicts, safe retries, CLI options, completion behavior, and socket boundaries.
The complete deterministic suite passed 271 tests with exit 0.

## Slack thread shortcut

The existing `slack-threads` collection already stores PR review/discussion/ping permalinks with optional descriptions.
Slack thread is the canonical UI label. The PR-list button and `l` shortcut open one reference directly or offer an explicit chooser for several.
The chooser retains its original PR, and unrelated screens cannot open the main view's hidden selection.
Cached references remain usable offline. URL validation and opener failure checks run before any success is reported.
No new metadata field or schema migration is needed. The action does not read or post Slack messages or invent an app URI.
The full deterministic suite passed 280 tests with exit 0, including nine added metadata and shortcut regressions.
Installed button and key checks passed with a captured opener. Live metadata, links, plan, schedule, sorting, and backend PID stayed unchanged.
