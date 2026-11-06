# Work tracker TUI

The Textual client opens My Day for daily planning. F4 switches to the open-PR workflow.
Standalone project/workstream management remains a follow-up. The application owns all data and scheduled jobs.

## Start the interface

The local application must be running. The interface does not start another
application, open SQLite, or create a scheduler.

```sh
~/.local/bin/work-tracker tui
```

For source development, run:

```sh
.venv/bin/python tracker.py tui
```

Put `--state-dir PATH` before `tui` to select an isolated application endpoint.
Closing the TUI does not stop the application or its jobs.

## My Day home view

The home view has Goals, Awareness, and Carry forward tabs. The day comes from the application's local calendar.
F4 or PRs switches to the PR list. F3 opens daily statistics. Escape returns to PRs, and `q` quits only the TUI.
Use Previous and Next for read-only history. Today follows the current local date, including after midnight.

Press `a` to add a goal or `w` to add persistent awareness. Quick text creates an Unclassified Task.
Choose Existing Task or Existing workstream to search known targets. An optional label describes the daily outcome without renaming its target.
Space changes only a daily goal's Open/Done state. `e` edits its label, and Delete removes it from today's plan without deleting the Task.
For awareness, Delete dismisses the persistent watch item. It stays visible on later days until dismissed.

Carry forward never runs automatically. Select a candidate in that tab and press `c` to create today's linked item.
The source day remains unchanged. Suggestions use the latest prior goal for each target, so completing a carried item does not revive older ancestors.
Linked PRs opens a picker of cached references and then full tracker details. A Task without a PR remains a valid target.
The picker shows up to the first 50 cached PR links for a target. Workstream classification links return to the filtered PR list.

From the PR list, `a` opens an add-goal form for the selected PR's existing Task.
Forms retain one key and frozen payload after a lost response. Retry repeats that request, not a new Task creation.
Item edits carry version preconditions. A conflicting edit reloads rather than overwriting another client.
If the calendar day changes, reopen an unsent old-day form. History is not silently rewritten.
No planning action changes GitHub or closes the linked Task/workstream. My Day has no MCP tools in this version.

## Browse tracked work

The table contains ingested open PRs only. All local pages are loaded before
filtering, so search does not silently stop at the first 200 records.
Search covers names, local descriptions, legacy project labels, and stored links.
It does not search archived messages, agent transcripts, or raw PR-body text.

Opened displays the GitHub creation date in UTC as `YYYY-MM-DD`.
Full details include the complete timestamp. Missing or invalid timestamps show
`Unknown` rather than a date inferred from another field.

Titles and opening timestamps are stored in the private SQLite datastore.
The interface reads them through the local application, including after a restart.
GitHub refresh failures retain the last known metadata with a stale warning.
Displaying this column makes no additional GitHub requests.

Filter by project, workstream, or the four review states.
The table keeps its selected PR across local reloads when that PR remains visible.
On a wide terminal, the detail pane follows the selection. On a narrow terminal,
Enter opens full details in a scrollable view. Full details update with the local
view poll and retain their PR identity if the PR closes or merges.

Project PRs and Workstream PRs buttons filter to related records.
They do not claim to be full project or workstream management screens.
Inventory opens the account-wide GitHub search page, not a local auto-ingestion
flow. Total-open and untracked-estimate counts remain separate from tracked work.
Observation times and failed background jobs remain visible.

The TUI reloads local state every three seconds. It does not schedule GitHub
polling itself. A manual refresh queues the application's existing refresh job.
If the application is unavailable, the last loaded view remains with a warning.
A failed connection is not shown as zero PRs. Editing is disabled until reconnect.

## Controls

| Key or control | Action |
| --- | --- |
| Arrow keys | Select and scroll PRs |
| `/` | Focus tracker-field search |
| Escape in the main view | Return focus to the PR table |
| Enter or Details | Open full PR details |
| `r` or Refresh GitHub | Request the application's read-only GitHub refresh, or reconnect if offline |
| `e` or Edit | Edit local description and project/workstream classification |
| `x` or Close PR | Open the GitHub close dialog with an optional multiline comment |
| `m` | Open merge confirmation for the selected PR |
| `a` in the PR list | Add the selected PR's Task to today |
| F4 | Switch between My Day and the PR list |
| `n` or Track PR | Explicitly ingest an existing GitHub PR URL |
| Links | Choose a local reference action with the mouse or keyboard |
| `t` | Attach a Slack thread |
| `s` | View saved sessions and resume actions, with an explicit Add session reference button |
| `d` | Remove a selected local Slack link through a dialog |
| `o` | Open the selected PR in the browser |
| `p` | Copy the selected PR URL |
| F2 | Choose the sort column and direction |
| F3 | View daily PR statistics overall or by project |
| Click a column header | Sort by that column, or reverse its current order |
| `c` or Clear filters | Clear search and filters |
| Ctrl+Enter in a local metadata form | Save local intent |
| Ctrl+Enter in Close PR | Confirm the GitHub comment and close |
| Ctrl+Enter in Merge PR | Confirm the previewed head commit and selected merge method |
| Escape or Cancel in a form | Cancel, unless a save is still in flight |
| `q` in the main view | Quit only the TUI |

Typing in input fields does not trigger single-letter application shortcuts.
The toolbar uses shorter labels at narrow widths. Browser links are opened only
on an explicit action and are checked for allowed HTTPS GitHub or Slack URLs.
Session controls can focus a verified existing herdr pane. They never execute stored shell text or start another conversation.

## Sorting and copying

The default sort is the full PR URL, ascending, including the PR number as text.
Click a column header to choose it. Click the active header again to reverse direction.
F2 opens a keyboard-accessible chooser. The active header shows an arrow.
Dates use their full timestamps, not their displayed strings. Unknown dates stay last in both directions.
Time since state change uses elapsed time, including the recorded lower bounds.
Review priority follows Ready to Merge, Changes requested, Blocked, then Awaiting review.
URL order breaks ties. The selected PR stays selected when its row moves.
Filters and refreshes retain the sort. Header clicks and F2 choices save the column and direction in the local application.
A fresh TUI restores that choice, including after the backend restarts. Filters still start clear.
The preference belongs to the selected application state directory. Existing windows retain their own current order.
Rapid changes save in sequence, with the latest choice last. Normal `q` exit waits for an in-flight save.
If saving fails, the local order remains active. The TUI warns and retries after reconnection.
It never writes a separate settings file or opens SQLite directly.

Press `p` from the list to copy the selected PR URL. Cached offline rows also support copying.
Typing `p` in a text field does not copy. macOS uses `pbcopy` through standard input, without a shell.
Other platforms use the terminal clipboard protocol and require terminal support.

## Daily statistics

F3 opens saved daily history. Choose a project or Overall and all projects to compare the series.
Unallocated is the default project bucket, including when its count is zero. The underlying Unclassified hierarchy stays unchanged.
The range selector covers 7, 30, 90, or 365 days. Scroll the table sideways to see every metric.
The view reads the application API only. Reload reads saved history without starting GitHub collection.

Tracked counts include all enrolled PRs, not the account-wide untracked estimate.
Closed counts exclude merges. Mean and p90 measure GitHub opening to the latest observed closure for each PR that day.
P90 uses nearest rank. Empty closure cohorts show no duration, and Timed/all shows timestamp coverage.
Stale and Missing O/C identify failed observations and missing opening/closing dates. Counts `>=` are known lower bounds.
An oldest-open age `>=` is a lower bound when opening dates are missing. JSON also includes unknown-state counts.

An asterisk marks today's provisional rows. Inventory and project assignment for past snapshots do not change when a PR is reclassified later.
Late closure observations can correct older closure metrics within their recorded membership.
The history notice identifies missing calendar days. Missing snapshots are not zero activity or an invented backfill.
If the application is offline, previously displayed rows stay visible. The TUI does not create its own statistics datastore.

## Session references and resume commands

PR registration, reference attachment, and resume availability are separate outcomes.
Press `s` to open the saved-session view. It shows each tool, workspace, session ID, machine, and action status.
Opening the view only reads saved data. It retains the selected PR while local data refreshes.
Choose `Add session reference` to open a separate attachment form. Existing sessions are not replaced.
The view retains cached identity if the application is offline. Adding a reference requires a connection.
Enter opens full PR details, which also expose session actions.

Choose `Activate open herdr pane` to search running local herdr sessions and focus a verified existing pane.
The application requires the recorded machine, workspace, tool, and exact session ID. It never starts another agent.
Herdr must report the session ID, or fx must hold its exact session files open in a foreground process.
The fx fallback requires `lsof` and the default `~/.fx` profile. Titles and working directories alone never identify a session.
The macOS installer includes `/usr/sbin` in the application's `PATH` so it can find `lsof`.
If that dependency is missing, the application reports an inspection failure rather than an absent pane.
If several panes match, no pane is activated. If no pane matches or inspection fails, use the separate copy-resume button.
This action changes the selected pane inside herdr. It does not select an internal fx instance or bring another terminal window forward.
Choose `Check and copy resume command` to verify the saved session and copy its command.
The application uses a native read-only lookup. It does not open a conversation or send a model request.
Paste the copied command into your terminal to resume in the recorded workspace.

The fx adapter uses `fx --resume SESSION_ID`. The OpenCode adapter uses `opencode --session SESSION_ID`.
Both require complete identity and a confirmed saved session. OpenCode also checks its recorded workspace against the native response.
References on another machine show `Copy command for recorded machine`. That action copies instructions only and does not check remote availability.
Unsupported tools, incomplete identity, missing workspaces, and failed lookups do not report a usable local link.
The application derives commands from explicit adapters. It ignores stored shell commands and does not invent URI schemes.

The CLI equivalent is `work-tracker sessions PR_URL --verify`.
MCP callers use `get_pr(url, verify_sessions=True)` and inspect `agent-session-actions` separately from `agent-sessions`.
Legacy OpenCode `source-session` metadata stays intact. Session references and actions do not prove PR authorship.
Native verification is cached for five minutes in the running application. Clicking the copy action checks again.

## State age and closing

The Since change column measures elapsed time in the displayed review/readiness
state. `≥` marks the lower bound since the tracker first observed it.
`Unknown` means earlier history is unavailable or the current observation is stale.
The CLI also shows this age. Full details include the observed transition time.
Ordinary polling and unrelated title, comment, or description updates do not reset it.

From the PR list, press `x` to open Close PR. The selected target stays fixed even
if the underlying list refreshes. Enter an optional multiline comment.
Choose Close PR or press Ctrl+Enter to confirm the external GitHub operation.
Enter alone adds a newline to the comment. Escape cancels before submission.
This operation does not delete branches or close the linked Task.

Once submitted, the dialog freezes its comment and retains one request key.
A lost response does not authorize posting the comment again.
Check outcome reads the recorded result or verifies closure on GitHub.
If the original request never arrived, retry sends the identical confirmed payload.
Reopening a pending or uncertain close resumes that request, not a new write.
If a comment succeeded but closure remains uncertain, inspect or finish it on GitHub.
No close operation is exposed through MCP, and scheduled jobs remain read-only.

## Local edits and agent concurrency

Forms submit intent through the same application boundary as CLI and MCP clients.
They cannot change GitHub approval, merge state, checks, or PR bodies.
Slack actions only change local links. They do not post or delete Slack messages.

Project and workstream fields suggest known names as you type.
Press Right Arrow at the end of a prefix to accept the displayed completion.
Matching names are also shown below each field. Workstream suggestions are scoped
to the project entered in the form, and update when that project changes.
The lookup includes stored hierarchy branches without open PRs, not just names
visible in the current PR list. Existing closed branches remain known names.

You can still enter new names. An unambiguous case-insensitive exact match reuses
the stored spelling rather than creating another differently capitalized name.
The form does not replace the workstream draft when the project changes.
If the name lookup fails, a warning appears and manual entry remains available.
It never opens SQLite as a fallback. Registration forms use the same suggestions.

The editor sends only fields that changed. A description-only edit does not reset
classification or overwrite a preserved legacy project label.
Description and classification preconditions are checked inside the storage
transaction. If another agent changes an edited field, the stale save fails.
Cancel and reopen the editor to load the latest data before resolving the edit.
Background view polling never replaces text in an open editor.

Each form keeps one intent key. If the save response is uncertain, the fields
stay frozen and Retry save sends the same payload and key.
This allows an already-committed intent to return its receipt without applying
again. Cancel and reopen to reload the canonical data before changing the draft.
Closing a dialog cannot retract an intent that the application already accepted.

Session links require explicit tool, absolute path, sessionId, and machine.
The form does not discover sessions, infer IDs, or guess the target machine.
Tracking a PR creates a Task under Unclassified unless classification is explicit.
Registration does not itself create a GitHub PR or force a remote refresh.

## Implementation and tests

`work_tracker/tui.py` owns presentation and local read workers.
`tui_forms.py` owns local-intent dialogs. Blocking socket requests run outside
the UI event loop. The application remains the only datastore and job owner.

The implementation follows Textual's [worker guidance](https://textual.textualize.io/guide/workers/)
and uses [headless Pilot tests](https://textual.textualize.io/guide/testing/).
Tests cover pagination, search, filters, keyboard routing, selection, narrow
layout, browser validation, offline views, live details, edits, conflicts,
idempotent retry, and references. A separate test drives the editor through a
real isolated application socket and confirms that closing the UI leaves it alive.

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_tui*.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

Textual was not cached, and the configured company registry returned 401.
Jack explicitly chose public PyPI for this project's dependency download.
Textual and its dependencies were installed into the development virtualenv,
with the existing SDK pins and two-day package-age cutoff preserved.
No company registry configuration or credentials were changed.
Installed application dependencies can reuse those pinned wheels offline.

Full conversation search, session reopening, My Day, tags, and broader hierarchy
editing remain follow-ups. No commit, push, or GitHub publication is implied.
