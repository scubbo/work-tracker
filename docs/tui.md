# Open PR TUI

Jack approved Textual after the headless application checkpoint and asked to
start TUI implementation. This first client covers open PR tracking.
My Day and standalone project/workstream management screens are not included yet.

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

## Browse tracked work

The table contains ingested open PRs only. All local pages are loaded before
filtering, so search does not silently stop at the first 200 records.
Search covers names, local descriptions, legacy project labels, and stored links.
It does not search archived messages, agent transcripts, or raw PR-body text.

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
| `n` or Track PR | Explicitly ingest an existing GitHub PR URL |
| Links | Choose a local reference action with the mouse or keyboard |
| `t` | Attach a Slack thread |
| `s` | Attach an agent-session reference |
| `d` | Remove a selected local Slack link through a dialog |
| `o` | Open the selected PR in the browser |
| `c` or Clear filters | Clear search and filters |
| Ctrl+Enter in a form | Save local intent |
| Escape or Cancel in a form | Cancel, unless a save is still in flight |
| `q` in the main view | Quit only the TUI |

Typing in input fields does not trigger single-letter application shortcuts.
The toolbar uses shorter labels at narrow widths. Browser links are opened only
on an explicit action and are checked for allowed HTTPS GitHub or Slack URLs.
Agent-session references are displayed, not executed or reopened.

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
