# Work tracker interview decisions

This document summarizes Jack's decisions from the current interview.
It is an assistant-written record, not a claim that a complete architecture or
implementation plan is approved.

Jack approved the standalone repository extraction, then selected open PR
tracking as the first workflow. He also requested a local MCP server so that
local agents can submit and update PRs.

The MCP server is required in the first PR-tracking slice. Jack clarified that
it accepts intent signals from local agents only. It must not create or modify
PRs on GitHub. The local application owns the datastore and scheduled jobs.
The TUI and MCP adapter are interfaces onto that application, not independent
datastore owners. Jack then accepted login/failure supervision through launchd
and instructed: "Yup, that's acceptable. Keep building."
The first headless slice uses Python, a private Unix socket, and the official
stdio MCP SDK. Jack subsequently approved Textual and requested TUI implementation.

## Current implementation

The first headless application slice implements SQLite behind a storage
interface, private local RPC, application-owned scheduling, a compatible CLI,
read-only PR discovery/readiness, typed agent intents, and stdio MCP.
Project/workstream classification and stable Tasks are present in the PR path.
Manual multi-tool session references and Slack links are supported.
The first Textual TUI implements the open-PR workflow, local metadata editing,
manual references, live details, and context filters. My Day adds local daily planning
through TUI and CLI. Broader hierarchy management views remain follow-ups. See `tui.md` and `application.md` for the
current scope, architecture, and limitations.

The installer now generates a process-supervision LaunchAgent without job times.
It retires the legacy calendar only after new-application readiness.
The legacy PR JSON is backed up and imported once. After migration it is a
compatibility export, not the canonical register. Archives and source checkpoints
remain in their existing private location. Existing private data stays outside Git.

## Local application and runtime ownership

The local application executable owns the datastore, job definitions, schedules,
and job execution. The TUI is an interface onto that application, not the
application runtime and not the scheduler. Closing the TUI must not stop jobs.
The local MCP adapter also submits intents through the application rather than
maintaining a separate register or independently writing its database.

Move the daily 10:00 local-time collection policy into the application's runtime
and configuration. Do not leave job timing in an installer-generated OS calendar
entry. The application also coordinates manual and automatic refresh requests.
External observations and local agent intents remain distinct.

Use a long-lived background application. Jack approved launchd to start it at
login and restart it after failure, with no collection times in the plist.
The implemented interfaces use private Unix RPC and stdio MCP.
Textual is selected for the first TUI client.

The migration must avoid running the old calendar job and the new application
scheduler at the same time. Verify replacement behavior before retiring the old
job. Catch-up after sleep or downtime, duplicate-job prevention, persistence of
job state, and recovery behavior need research and tests.

## Hierarchy and classification

Use `Project > Workstream > Task` as a tree. Cross-cutting tags are a later feature.
A task is separate from a PR. Default to one task per PR, but retain independent
identities so a task can later have several PR links. A task can exist without a
PR. A PR belongs to at most one task.

Newly discovered PRs and their default tasks enter an unclassified inbox.
They remain visible in My Open PRs before classification.
Use a general Unclassified project and workstream rather than parentless tasks.
The inbox can be a view over that branch.

Projects and workstreams stay open until explicitly closed. Tasks also close
manually in the first version. Closing a task when its PR merges is a follow-up.
Do not infer that zero open PRs means that a project or workstream is complete.

## Main views

The first version includes My Open PRs, My Open Workstreams, My Open Projects,
and My Day. Projects and workstreams show counts of open PRs.
PRs can be filtered by project and workstream. Views are searchable and link to
related tracker records.

The TUI supports local edits, not just browsing. It can manage the hierarchy,
tasks, classification, links, and My Day. Jack subsequently requested a Close PR
dialog with a comment box. This permits an explicitly confirmed close and comment
on GitHub. Jack later requested explicitly confirmed merging through `m`.
Other GitHub mutations and Slack posting remain outside the TUI's approved scope.
MCP remains local-intent only.

Search initially covers tracker names, descriptions, notes, and stored links.
Search of full Slack messages and agent conversations is a follow-up.

## PR state and refresh

The PR list includes elapsed time since the displayed state changed.
Track observations durably, without treating unrelated GitHub updates as changes.
Unknown earlier history remains unknown. First tracking gives a lower bound.

The `x` shortcut opens Close PR for the selected record. The dialog has an
optional multiline close comment and explicit confirmation. The application
executes the operation, not the TUI. Branches and Tasks stay unchanged.
A persistent request key prevents comment duplication after a lost response.
Uncertain writes are not automatically retried, including after a restart.

Keep Awaiting review, Changes requested, Ready to Merge, and Blocked distinct.
Ready to Merge replaces the earlier proposed Approved label.
A PR with sufficient human approval but failing required CI needs an honest
Blocked display rather than a Ready to Merge label.

Automated feedback never takes a PR out of Awaiting review.
Display automated feedback separately from the human-review state.
Detailed approval, CI, and CODEOWNERS explanations are a stretch goal.
The exact readiness rules still need research and discussion. Do not equate one
approval with merge readiness or ignore required checks in the basic display.

Refresh review and CI data automatically while the TUI is open.
Also provide a manual refresh action. This is separate from daily archiving.
Jack clarified during implementation that refresh only needs PRs already
ingested into work-tracker. Do not fetch or automatically enroll every open PR
on the account. An account-wide total-open count, untracked-open count or
estimate, and a GitHub link are useful separate summary features.
The headless implementation provides those counts without enrolling the results.
The initial application interval is five minutes, configurable locally.
Unknown readiness blocks a Ready to Merge claim. Refresh failures retain prior
observations but mark them stale and Blocked. The detailed policy is documented
in `application.md`; CODEOWNERS explanations remain a follow-up.

Jack later requested `m` as a single-key entry to merging from the PR list.
The shortcut opens human confirmation, not an immediate write. It loads the current head and allowed merge methods from GitHub.
The application must recheck readiness and the selected method, then use GitHub's head-SHA precondition.
Do not enable auto-merge, bypass required queues, request branch deletion, or close the linked Task.
Make clear that GitHub's own policy for automatic branch deletion still applies.
Keep uncertain writes in a persistent ledger without automatic replay. Agent intents and MCP cannot request this GitHub mutation.

A later agent hook can mark work as Implementing when Jack starts work on it.
Do not implement the hook in the first version. Its relationship to review state
still needs definition.

## Daily PR statistics

Jack requested daily overall and per-project history for total tracked PRs, closed/merged counts, oldest open age, and mean/p90 closure time.
He selected PRs closed or merged on that day as the closure-time cohort, rather than cumulative completions.
Unallocated must be a separate project bucket. Include zero-count buckets without changing existing classifications.
Use observed GitHub creation and terminal timestamps. Expose missing and stale evidence instead of inventing durations or claiming zero activity.
Preserve historical inventory and classification. Do not reconstruct missed inventory dates from the current register.
The local application owns durable snapshots and timing. The TUI, CLI, and private API provide read-only history views.

## My Day

My Day contains intended outcomes for today and awareness items that Jack is
keeping an eye on. Awareness items do not imply active PR follow-up.
Existing workstreams and tasks can be selected into the view.

Quick text entry still creates a Task. Put it under the general Unclassified
project and workstream, then link it into My Day. Do not introduce taskless
standalone goals as a shortcut.

Awareness items persist until dismissed. Goals are selected afresh each day.
Offer yesterday's unfinished goals for explicit carry-forward rather than
copying them automatically. Completing a goal affects only that day's item,
not its linked task or workstream.

Jack selected My Day as the TUI's home view. Daily goals use Open/Done, not an additional progress workflow.
The first implementation exposes My Day through the TUI and CLI only. Do not add My Day MCP tools.
Keep the PR list available as a separate view, and provide a shortcut to add its selected PR's Task to today.

## Slack and agent-session links

Initially attach Slack threads and agent-session references to PRs.
Keep the design extendable to tasks and possibly other hierarchy levels.
Slack thread links can have optional descriptions.

Support references to different agent tools, including OpenCode, fx, and Claude.
A session reference contains the tool name, `path`, and `sessionId`.
Support several references rather than overwriting an earlier session.
Retain machine context for paths that are meaningful only on a particular host.

New TUI session references can be entered manually in the first version.
Automatic discovery of sessions from additional tools is deferred.
This does not remove existing OpenCode archive collection.
Parsing new Slack messages into database updates remains a stretch goal.
Jack subsequently requested a usable resume action for verified session references.
Registration, reference attachment, and resume availability must remain distinct outcomes.
The application can derive a quoted copy-resume command for fx and OpenCode without an invented URL.
A native read-only lookup must confirm a local session before it is described as usable.
The recorded machine and workspace constrain the action. Another machine receives labeled instructions, not a local launch.
Unsupported or incomplete references stay intact and explicitly report no usable session link.
The canonical register-pr skill must verify the action, not only the saved identity fields.
Attaching a session reference never proves PR authorship.
Jack subsequently requested a button that finds and activates an existing local herdr pane for an attached session.
Search every running local herdr session. Require an exact live identity, tool, and workspace match on the recorded machine.
Do not infer identity from titles, transcripts, or workspace alone. Do not start another agent or evaluate stored shell text.
Keep the copy-resume command as a separate fallback. No match, an ambiguous match, or an inspection failure must leave that fallback available.

## Local agent MCP server

Include a local Model Context Protocol (MCP) server in the first open-PR-tracking
slice. Local agents must be configurable to submit and update PRs through it.
The server belongs to this repository, not a separate agent-specific integration.
Document how clients connect. Do not silently rewrite existing agent configuration.

The write scope is settled: accept intent signals from local agents only.
Never create or modify GitHub PRs through MCP. Agent submissions do not establish
GitHub facts such as merge state, CI success, or human approval. Keep external
observations distinct from agent intent and local tracker metadata.

Route intents into the local application, which owns storage and scheduled jobs.
The TUI, CLI, and MCP interface must not maintain separate copies of tracker
state. The stdio server and validated local intent vocabulary are implemented.
Agent-client configuration is documented but is not installed automatically.
Do not treat this boundary as approval for previously deferred agent hooks or
automatic session discovery.

## Storage, users, and machines

Use single-user operation first. Keep personal data, external account identity,
and potentially shared project records conceptually distinct for future work.
Do not implement multi-user sign-in or collaboration in the first version.

The local application uses local SQLite behind a storage interface.
The TUI accesses application operations rather than owning the database itself.
Do not implement a remote backend merely to prove the interface.
A future remote mode can use one shared database and require connectivity for
edits. Offline synchronization and CRDTs remain deferred even for that mode.
Do not silently create divergent local state when a remote connection fails.

Existing collector data is not automatically SQLite data. A future migration
must preserve curated metadata, links, checkpoints, and archive references.
The existing free-text project labels need an explicit mapping to the new tree.
Do not guess project or workstream meaning from a repository name.

## Follow-ups

- Cross-cutting tags.
- Closing a task when its PR merges.
- Agent hooks for Implementing status.
- Detailed approval, CI, and CODEOWNERS explanations.
- Automatic session discovery for additional agent tools.
- Parsing Slack replies into structured updates.
- Jumping back to an agent session.
- Full-text search of Slack and agent conversations.
- A remote storage backend.
- Offline synchronization and CRDTs.
- Multi-user collaboration.

## Open interview and setup decisions

Jack selected open PR tracking as the first usable workflow.
All main views remain in the proposed first-version scope.
Agent submissions and updates are local intent signals only, never GitHub
mutations. The application owns both the datastore and scheduled jobs.
The lifecycle decision is settled: launchd supervises the background process,
and all job timing belongs to the application.

Textual is approved and the first PR interface is implemented.
Platform scope beyond the current macOS installer remains unselected. The first readiness algorithm is conservative; detailed
approval/CI/CODEOWNERS explanation still needs future work.
SQLite import and the legacy CLI bridge are implemented. Live checks confirmed
the private application, installed MCP adapter, retained legacy metadata, and a
successful refresh of all ingested PR records. The initial over-broad account
backfill was corrected. Extra observations remain in an untracked cache,
not the active register. See `implementation.md` for verification and limits.
Jack explicitly requested an initial commit and push, then selected a public
`scubbo/work-tracker` repository. No license is selected. Private runtime data
must stay outside Git.
