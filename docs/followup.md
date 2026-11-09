# Follow-up advice

F5 opens Follow-up from My Day or the PR list. It does not change your daily plan.
The first version advises only. It never merges, sends Slack messages, books meetings, starts agents, or changes Task completion.

## Two questions

What next shows one concrete action when only one candidate is available or the AI can justify its choice.
When candidates compete, it shows a clarification question rather than inventing your personal priority policy.
The table remains alphabetical, not a hidden ranking. Asterisks mark the recommended or competing candidates, and new advice selects its first candidate.
Project and workstream filters narrow the decision.

Through the cracks lists signals that need attention. The default threshold is seven days without an observed review-state or planning change.
A PR open for at least 30 days also gets an age signal. Age does not establish neglect or priority.
The 14-day and 30-day choices change the inactivity-signal threshold. The age-only threshold is never less than 30 days.

The application does not know every human action. Review-state history is not a complete conversation log.
Bot comments and ordinary background refreshes do not reset the review-state clock. Missing dates remain unknown.
Resolved PRs remain retained, but their default open Tasks do not automatically flood this view.
Tasks without PRs and explicitly planned or annotated Tasks remain candidates. Task completion stays separate from PR resolution.

## Explicit next-step metadata

Select a row and choose Next steps. Its metadata belongs to the underlying Task, not only today's goal.
Next action records the immediate step. After Task records follow-on work. Waiting on records a person or condition.
Dependencies contain existing Task IDs. Unknown IDs and dependency cycles are rejected.
Revisit after accepts a local calendar date. The Task and its PRs stay out of advice until that date.

The selected Task ID appears below the table. Use `work-tracker day targets` to find other existing Task IDs.
Edits use version checks and send changed fields only. Retrying identical saved values does not overwrite a newer, different edit.
These fields do not rename, reclassify, complete, merge, or close anything.

```sh
work-tracker follow-up
work-tracker follow-up --project Alpha --workstream Review --json
work-tracker follow-up task TASK_ID
work-tracker follow-up task TASK_ID --next-action 'Discuss the blocker with the reviewer'
work-tracker follow-up task TASK_ID --dependencies OTHER_TASK_ID
work-tracker follow-up task TASK_ID --after-task 'Prepare the next PR'
work-tracker follow-up task TASK_ID --revisit-after 2026-12-01
work-tracker follow-up task TASK_ID --revisit-after ''
```

## Enable AI Gateway

Local advice works without a key. The AI path requires your own Gateway credential.
Run this command in a terminal. The prompt masks the key and refuses an unmasked fallback.
Do not put the key in chat, shell arguments, Git, or a public environment file.

```sh
work-tracker follow-up key
```

The application stores the key in `gateway.key` under its private state directory, with owner-only permissions.
The normal state directory is `~/.local/state/work-activity`. The installer preserves this file.
`AI_GATEWAY_API_KEY` or `VERCEL_AI_GATEWAY_API_KEY` in the service environment takes precedence over the private file.
A shell variable does not automatically reach an existing LaunchAgent.
Status returns configuration presence, never the key. Provider error bodies and request headers are not logged or saved.

The default model is `openai/gpt-4.1-mini`. Choose a different model explicitly if needed.
The adapter uses the documented [OpenAI-compatible Gateway endpoint](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions).
It requests [schema-constrained output](https://vercel.com/docs/ai-gateway/sdks-and-apis/openai-chat-completions/structured-outputs).
Available model identifiers appear in the [public Gateway catalog](https://ai-gateway.vercel.sh/v1/models).

```sh
work-tracker follow-up model openai/gpt-4.1-mini
work-tracker follow-up analyze --wait
work-tracker follow-up --project Alpha analyze --request-key analysis-example --wait
work-tracker follow-up history
```

## Progressive context and spending

Opening the view, polling it, changing filters, and Reload read local data only.
Only Analyze AI or `follow-up analyze` requests cloud work. There is no periodic analysis or automatic paid retry.
One application-owned manual job runs at a time. Interrupted requests remain interrupted after a backend restart.
Reuse the same request key after an unknown response. It returns the original receipt instead of charging for another analysis.

The first request contains compact candidate facts, not every PR body, daily note, Slack message, path, or saved session.
The shortlist has at most 24 candidates and a 16,000-byte summary budget.
When the backlog exceeds that shortlist, the agent cannot declare a unique overall winner. Narrow the project or workstream instead.

The model can request additional Task context, GitHub discussion, or an explicitly linked Slack thread for a known candidate.
Each requested excerpt has a 3,000-character limit. Task context contains explicit guidance and at most two recent daily goals.
GitHub context reads the latest ten issue comments and ten reviews in one bounded query. Inline review threads are not included.
Slack context reads at most ten messages from the first explicitly attached thread through the configured Slack CLI workspace.
It does not search other channels, fetch all attached threads, or read agent-session transcripts.
An unavailable source is reported as unavailable. The agent cannot infer its contents.

Each analysis permits at most three model-call attempts and three context requests. Each model response has a 1,200-token output limit.
Repeated requests for the same context do not fetch it again. The loop stops when no further progress is possible.
The fixed Gateway endpoint refuses redirects. Native context reads have a 12-second timeout, and Gateway calls have a 25-second timeout.

Private history records the compact candidate summaries, model, interpretation, context-source identifiers, input bytes, and reported token usage.
It does not retain full copied Slack or GitHub response bodies. Token counts missing after a failed request do not establish zero billing.
No dollar estimate is invented. Consult Gateway billing for actual charges.

## Facts and interpretation

The application builds candidate actions from tracked facts and explicit metadata. The model selects only known candidate identifiers.
Current facts and scope must still match before an AI interpretation replaces local advice. Changed facts invalidate the cached report without another model call.
Merge advice requires fresh cached clean readiness. Opening merge confirmation still performs the existing independent, head-pinned checks.
Uncertain close or merge operations take precedence over further mutation advice.
Explicit next actions remain visible with stale-data warnings, but they do not establish verified merge readiness.

PR bodies, comments, notes, and Slack text are evidence, not instructions. They cannot supply commands, endpoints, tools, or new unregistered targets.
The model cannot execute its recommendation. No follow-up operation is exposed as a new MCP tool.

## Deliberate limits

This is an initial workflow for gathering experience, not a learned personal prioritizer.
Clarification questions can suggest a conversation, but there is no in-app chat yet.
Private analysis history supports later evaluation. Feedback-driven policy updates and a personal `SOUL.md` remain follow-ups.
No ambient learning or automatic rule rewriting is enabled.
