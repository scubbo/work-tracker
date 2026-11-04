"""Local-intent dialogs for the TUI. No datastore or external service access."""

import copy
from pathlib import Path
import uuid

from rich.text import Text
from textual import on, work
from textual.events import DescendantFocus
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester, SuggestionReady
from textual.widgets import Button, Input, Label, Select, Static, TextArea

import work_activity as activity


class KnownNameSuggester(Suggester):
    """Read current choices without caching a previous project's workstreams."""
    def __init__(self, choices):
        super().__init__(use_cache=False, case_sensitive=False)
        self.choices = choices

    async def get_suggestion(self, value):
        if not value:
            return None
        return next((name for name in self.choices() if name.casefold().startswith(value.casefold())), None)


def canonical_name(value, choices):
    if value in choices:
        return value
    matches = [name for name in choices if name.casefold() == value.casefold()]
    return matches[0] if len(matches) == 1 else value


class IntentForm(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+enter", "save", "Save", priority=True)]
    CSS = """
    IntentForm { align: center middle; background: $background 75%; }
    IntentForm > #intent-dialog { width: 78; max-width: 96%; height: 90%; max-height: 38;
        padding: 1 2; border: round $accent; background: $surface; }
    IntentForm #form-title { height: 2; text-style: bold; color: $accent; }
    IntentForm #form-note { height: auto; max-height: 3; margin-bottom: 1; color: $text-muted; }
    IntentForm #form-fields { height: 1fr; }
    IntentForm .name-hint { height: auto; max-height: 3; color: $text-muted; }
    IntentForm #metadata-options-status { height: auto; max-height: 3; color: $text-muted; }
    IntentForm Label { margin-top: 1; height: 1; }
    IntentForm Input, IntentForm Select { width: 100%; }
    IntentForm TextArea { height: 6; min-height: 4; border: solid $primary; }
    IntentForm #form-error { height: auto; max-height: 5; color: $error; margin-top: 1; }
    IntentForm #form-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    IntentForm Button { margin-left: 1; }
    """

    def __init__(self, mode, record=None):
        super().__init__()
        self.mode = mode
        self.record = copy.deepcopy(record or {})
        self.busy = False
        self.pending = None
        self.intent_key = uuid.uuid4().hex
        self.catalog = []
        self.catalog_loading = mode in ('register', 'edit')
        self.project_choices = [self.record['project_name']] if self.record.get('project_name') else []
        self.workstream_choices = [self.record['workstream_name']] if self.record.get('workstream_name') else []
        self.titles = {"register": "Track an existing PR", "edit": "Edit local metadata",
                       "slack": "Attach a Slack thread", "session": "Attach an agent session",
                       "remove_slack": "Remove a local Slack link"}

    def compose(self) -> ComposeResult:
        with Vertical(id="intent-dialog"):
            yield Static(self.titles[self.mode], id="form-title", markup=False)
            note = "Local tracker changes only. No GitHub or Slack mutation. Ctrl+Enter saves; Esc cancels."
            if self.mode == "session":
                note = "Enter known session identity only. This stores a reference; it does not launch or discover an agent."
            yield Static(note, id="form-note", markup=False)
            with VerticalScroll(id="form-fields"):
                if self.mode == "register":
                    yield Label("GitHub PR URL")
                    yield Input(placeholder="https://github.com/owner/repo/pull/123", id="form-url")
                else:
                    yield Static(self.record.get("url", ""), markup=False)
                if self.mode in ("register", "edit"):
                    yield Static('Loading known names from the application...', id='metadata-options-status', markup=False)
                    yield Label("Project (blank uses Unclassified)")
                    yield Input(value=self.record.get("project_name", "") if self.mode == "edit" else "", id="form-project",
                                suggester=KnownNameSuggester(lambda: self.project_choices))
                    yield Static('', id='project-name-hint', classes='name-hint', markup=False)
                    yield Label("Workstream (blank uses Unclassified)")
                    yield Input(value=self.record.get("workstream_name", "") if self.mode == "edit" else "", id="form-workstream",
                                suggester=KnownNameSuggester(lambda: self.workstream_choices))
                    yield Static('', id='workstream-name-hint', classes='name-hint', markup=False)
                    yield Label("Local description / why this matters")
                    yield TextArea(self.record.get("description") or "", id="form-description")
                elif self.mode == "slack":
                    yield Label("Slack thread permalink")
                    yield Input(placeholder="https://workspace.slack.com/archives/C123/p123", id="form-thread")
                    yield Label("Optional description")
                    yield Input(id="form-thread-description")
                elif self.mode == "session":
                    for name, label, placeholder in (
                        ("tool", "Agent tool", "fx, claude, opencode, ..."),
                        ("path", "Absolute session/worktree path", "/absolute/path"),
                        ("sessionId", "Known session ID", "Do not infer or invent an ID"),
                        ("machine", "Machine where the session belongs", "Explicit machine name"),
                    ):
                        yield Label(label)
                        yield Input(placeholder=placeholder, id=f"form-{name}")
                else:
                    yield Label("Link to remove (the Slack message is not changed)")
                    yield Select([(Text(thread.get("description") or thread["url"]), thread["url"])
                                  for thread in self.record.get("slack-threads", [])],
                                 allow_blank=False, id="form-remove-thread")
            yield Static("", id="form-error", markup=False)
            with Horizontal(id="form-buttons"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Save locally" if self.mode != "remove_slack" else "Remove local link",
                             variant="primary" if self.mode != "remove_slack" else "warning", id="form-save")

    def on_mount(self):
        self.query("Input, TextArea, Select").first().focus()
        if self.catalog_loading:
            self.query_one('#form-save', Button).disabled = True
            self.load_known_names()

    @work(group='metadata-options', exit_on_error=False)
    async def load_known_names(self):
        try:
            result = await self.app.call_api('catalog')
            if not self.is_mounted:
                return
            projects = result['projects']
            if not isinstance(projects, list) or any(
                    not isinstance(p.get('name'), str) or not isinstance(p.get('workstreams'), list)
                    or any(not isinstance(w.get('name'), str) for w in p['workstreams']) for p in projects):
                raise ValueError('Invalid hierarchy names from the application')
            self.catalog = projects
            self.project_choices = sorted({p['name'] for p in projects} | set(self.project_choices), key=lambda n: (n.casefold(), n))
            self.query_one('#metadata-options-status', Static).update('Right Arrow accepts completion. New names are also allowed.')
        except Exception:
            if self.is_mounted:
                self.query_one('#metadata-options-status', Static).update(
                    'Known names are unavailable. You can still enter names manually; no database fallback is used.')
        finally:
            self.catalog_loading = False
            if self.is_mounted:
                self.query_one('#form-save', Button).disabled = self.busy
        await self.update_name_hints()

    def workstreams_for(self, project):
        names = {w['name'] for p in self.catalog if p['name'] == project for w in p['workstreams']}
        if project == self.record.get('project_name') and self.record.get('workstream_name'):
            names.add(self.record['workstream_name'])
        return sorted(names, key=lambda n: (n.casefold(), n))

    async def update_name_hints(self):
        if self.mode not in ('register', 'edit') or not self.is_mounted:
            return
        project = canonical_name(self.value('project') or 'Unclassified', self.project_choices)
        self.workstream_choices = self.workstreams_for(project)
        for field, choices in (('project', self.project_choices), ('workstream', self.workstream_choices)):
            widget = self.query_one(f'#form-{field}', Input)
            prefix = widget.value.strip().casefold()
            matches = [name for name in choices if name.casefold().startswith(prefix)]
            if matches:
                shown = ', '.join(name if len(name) <= 40 else name[:39]+'…' for name in matches[:4])
                suffix = f' (+{len(matches)-4} more)' if len(matches) > 4 else ''
                hint = f'Known matches: {shown}{suffix}'
            else:
                hint = 'No known matches. A new name is allowed.'
            self.query_one(f'#{field}-name-hint', Static).update(hint)
            suggestion = await widget.suggester.get_suggestion(widget.value.casefold()) if widget.value else None
            # Public Input message also clears a ghost completion when its project changed.
            widget.post_message(SuggestionReady(widget.value, suggestion or ''))

    @on(Input.Changed, '#form-project')
    @on(Input.Changed, '#form-workstream')
    async def name_changed(self, event):
        await self.update_name_hints()

    @on(DescendantFocus)
    async def name_focused(self, event):
        if event.widget.id in ('form-project', 'form-workstream'):
            await self.update_name_hints()

    def value(self, name):
        return self.query_one(f"#form-{name}", Input).value.strip()

    def build_intent(self):
        url = activity.pr_url(self.value("url")) if self.mode == "register" else activity.pr_url(self.record["url"])
        payload = {"url": url}
        if self.mode in ("register", "edit"):
            project = canonical_name(self.value("project") or "Unclassified", self.project_choices)
            workstream = canonical_name(self.value("workstream") or "Unclassified", self.workstreams_for(project))
            if len(project) > 200 or len(workstream) > 200:
                raise ValueError("Project and workstream names cannot exceed 200 characters.")
            description = self.query_one("#form-description", TextArea).text
            if self.mode == "register":
                if project != "Unclassified" or workstream != "Unclassified":
                    payload.update(project=project, workstream=workstream)
                if description:
                    payload["description"] = description
                return "register_pr", payload
            expected = {}
            if (project, workstream) != (self.record["project_name"], self.record["workstream_name"]):
                payload.update(project=project, workstream=workstream)
                expected.update(project_name=self.record["project_name"], workstream_name=self.record["workstream_name"])
            if description != (self.record.get("description") or ""):
                payload["description"] = description
                expected["description"] = self.record.get("description")
            if not expected:
                raise ValueError("There are no local changes to save.")
            payload["expected"] = expected
            return "update_pr", payload
        if self.mode == "slack":
            payload["thread"] = activity.slack_url(self.value("thread"))
            description = self.value("thread-description")
            if description:
                payload["description"] = description
            return "attach_slack", payload
        if self.mode == "session":
            session = {key: self.value(key) for key in ("tool", "path", "sessionId", "machine")}
            if not all(session.values()):
                raise ValueError("Tool, path, sessionId, and machine are all required.")
            if not Path(session["path"]).is_absolute():
                raise ValueError("The session path must be absolute.")
            payload["session"] = session
            return "attach_session", payload
        thread = self.query_one("#form-remove-thread", Select).value
        if not isinstance(thread, str):
            raise ValueError("Select the Slack link to remove.")
        payload["thread"] = activity.slack_url(thread)
        return "remove_slack", payload

    @on(Button.Pressed)
    def button_pressed(self, event):
        event.stop()
        if event.button.id == "form-save":
            self.action_save()
        elif event.button.id == "form-cancel":
            self.action_cancel()

    def action_cancel(self):
        if not self.busy:
            self.dismiss(None)

    def action_save(self):
        if self.busy or self.catalog_loading:
            return
        if self.pending is None:
            try:
                kind, payload = self.build_intent()
                self.pending = {"key": self.intent_key, "kind": kind, "payload": payload, "source": {"client": "tui"}}
            except ValueError as exc:
                self.query_one("#form-error", Static).update(str(exc))
                return
        self.busy = True
        for widget in self.query("Input, TextArea, Select"):
            widget.disabled = True
        self.query_one("#form-save", Button).disabled = True
        self.query_one("#form-cancel", Button).disabled = True
        self.query_one("#form-error", Static).update("Saving local intent...")
        self.save()

    @work(group="intent-save", exclusive=True, exit_on_error=False)
    async def save(self):
        try:
            receipt = await self.app.call_api("intent", self.pending)
        except Exception as exc:
            self.busy = False
            self.query_one("#form-error", Static).update(
                f"Could not confirm the save: {exc}\nRetry unchanged with the same intent key, or cancel and reopen to reload.")
            self.query_one("#form-save", Button).label = "Retry save"
            self.query_one("#form-save", Button).disabled = False
            self.query_one("#form-cancel", Button).disabled = False
        else:
            self.busy = False
            self.dismiss({"url": self.pending["payload"]["url"], "kind": self.pending["kind"], "receipt": receipt})
