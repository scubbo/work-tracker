"""Day-item forms. The backend owns Task creation and idempotent writes."""

from copy import deepcopy
import uuid

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from .tui_close import literal


class DayItemForm(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape','cancel','Cancel'),Binding('ctrl+enter','save','Save',priority=True)]
    CSS = """
    DayItemForm { align: center middle; background: $background 75%; }
    DayItemForm > Vertical { width: 88; max-width: 96%; height: 90%; max-height: 34;
        padding: 1 2; border: round $accent; background: $surface; }
    DayItemForm Static { height: auto; margin-bottom: 1; }
    DayItemForm #day-form-fields { height: 1fr; }
    DayItemForm #day-target-search { height: 3; }
    DayItemForm #day-target-query { width: 1fr; }
    DayItemForm #day-target-find { width: 12; }
    DayItemForm #day-form-message { height: auto; max-height: 4; }
    DayItemForm #day-form-buttons { height: 3; align-horizontal: right; }
    DayItemForm Button { margin-left: 1; }
    """

    def __init__(self, kind='goal', day=None, pr_url=None, item=None):
        super().__init__()
        self.kind, self.day, self.pr_url = kind, day, pr_url
        self.item = deepcopy(item)
        self.key = uuid.uuid4().hex
        self.pending = None
        self.busy = False
        self.targets = {}

    def compose(self):
        with Vertical():
            yield Static('Edit day label' if self.item else ('Add daily goal' if self.kind=='goal' else 'Add persistent awareness'),markup=False)
            with VerticalScroll(id='day-form-fields'):
                yield Static('Quick text creates a Task in Unclassified. Existing targets keep their own identity. '
                             'Editing a day label never renames or closes the underlying Task.',markup=False)
                yield Input(value=self.item['label'] if self.item else '',placeholder='Goal text or an optional label for an existing target',max_length=2000,id='day-label')
                options=[('New Task from text','new'),('Existing Task','task'),('Existing workstream','workstream')]
                if self.pr_url: options.append(('Selected tracked PR Task','pr'))
                yield Select(options,value='pr' if self.pr_url else 'new',allow_blank=False,id='day-target-kind',disabled=bool(self.item or self.pr_url))
                if self.pr_url: yield Static(self.pr_url,markup=False)
                with Horizontal(id='day-target-search'):
                    yield Input(placeholder='Search Task, workstream, project, or PR URL',id='day-target-query',disabled=True)
                    yield Button('Find',id='day-target-find',disabled=True)
                yield Select([],prompt='Choose a target',id='day-target',disabled=True)
            yield Static('Loading the application calendar day...' if self.day is None else f'Day: {self.day}',id='day-form-message',markup=False)
            with Horizontal(id='day-form-buttons'):
                yield Button('Cancel',id='day-form-cancel')
                yield Button('Save' if self.item else 'Add',id='day-form-save',variant='primary',disabled=self.day is None)

    def on_mount(self):
        self.query_one('#day-label',Input).focus()
        if self.day is None: self.load_day()

    @work(group='day-form-calendar',exit_on_error=False)
    async def load_day(self):
        try:
            value=await self.app.call_api('my_day',{})
            if self.is_mounted:
                self.day=value['today']
                self.query_one('#day-form-message',Static).update('Day: '+self.day)
                self.query_one('#day-form-save',Button).disabled=False
        except Exception:
            if self.is_mounted:
                self.query_one('#day-form-message',Static).update('The application calendar is unavailable. Reopen this form after reconnection.')

    @on(Select.Changed,'#day-target-kind')
    def mode_changed(self,event):
        if self.item or self.pr_url or not self.is_mounted: return
        enabled=event.value in ('task','workstream')
        for selector in ('#day-target-query','#day-target-find','#day-target'):
            self.query_one(selector).disabled=not enabled
        self.targets={}
        self.query_one('#day-target',Select).set_options([])
        if enabled: self.find_targets()

    @work(group='day-form-targets',exclusive=True,exit_on_error=False)
    async def find_targets(self):
        kind=self.query_one('#day-target-kind',Select).value
        query=self.query_one('#day-target-query',Input).value
        if kind not in ('task','workstream'): return
        try:
            value=await self.app.call_api('day_targets',{'kind':kind,'search':query,'limit':100})
            if not self.is_mounted or self.query_one('#day-target-kind',Select).value!=kind or self.query_one('#day-target-query',Input).value!=query:
                return
            self.targets={target['id']:target for target in value['items']}
            self.query_one('#day-target',Select).set_options([
                (Text(literal(f"{target['title']} | {target['project_name']} / {target['workstream_name']} | {target['pr_count']} PRs")),target['id'])
                for target in value['items']])
            self.query_one('#day-form-message',Static).update(f"{len(value['items'])}/{value['total']} targets. Refine the search if needed.")
        except Exception:
            if self.is_mounted:
                self.query_one('#day-form-message',Static).update('Targets are unavailable. Keep the draft and retry Find.')

    def action_cancel(self):
        if not self.busy: self.dismiss(None)

    def action_save(self):
        if self.busy or self.day is None: return
        if self.pending is None:
            label=self.query_one('#day-label',Input).value.strip()
            payload={'day':self.day}
            if self.item:
                payload.update(item_id=self.item['id'],expected_version=self.item['version'],text=label)
                action='edit'
            else:
                action='add'
                kind=self.query_one('#day-target-kind',Select).value
                payload.update(kind=self.kind,target_kind=kind)
                if label: payload['text']=label
                if kind=='new' and not label:
                    self.query_one('#day-form-message',Static).update('Enter text for a new Task.')
                    return
                if kind=='pr': payload['url']=self.pr_url
                elif kind in ('task','workstream'):
                    target=self.query_one('#day-target',Select).value
                    if target not in self.targets:
                        self.query_one('#day-form-message',Static).update('Choose a Task or workstream first.')
                        return
                    payload['target_id']=target
            self.pending={'key':self.key,'action':action,'payload':payload,'source':{'client':'tui'}}
            for widget in [*self.query(Input),*self.query(Select)]: widget.disabled=True
        self.busy=True
        self.query_one('#day-form-save',Button).disabled=True
        self.query_one('#day-form-cancel',Button).disabled=True
        self.save()

    @work(group='day-form-save',exit_on_error=False)
    async def save(self):
        try:
            value=await self.app.call_api('day_intent',deepcopy(self.pending))
            if self.is_mounted: self.dismiss(value)
        except ValueError as exc:
            if self.is_mounted:
                self.query_one('#day-form-message',Static).update(literal(str(exc))+' Reopen the form to review fresh data.')
        except Exception:
            if self.is_mounted:
                self.query_one('#day-form-message',Static).update('Response unavailable. Retry reuses the same key and frozen payload. Do not create a fresh duplicate.')
        finally:
            self.busy=False
            if self.is_mounted:
                self.query_one('#day-form-save',Button).disabled=False
                self.query_one('#day-form-save',Button).label='Retry same request'
                self.query_one('#day-form-cancel',Button).disabled=False

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        if event.button.id=='day-form-save': self.action_save()
        elif event.button.id=='day-form-cancel': self.action_cancel()
        elif event.button.id=='day-target-find': self.find_targets()


class DayPRPicker(ModalScreen[str | None]):
    BINDINGS=[Binding('escape','close','Back')]
    CSS='DayPRPicker {align: center middle;} DayPRPicker > Vertical {width: 90%;height: auto;padding: 1 2;border: round $accent;background: $surface;}'

    def __init__(self,prs):
        super().__init__()
        self.prs=deepcopy(prs)

    def compose(self):
        with Vertical():
            yield Static('Linked PRs (cached observations)',markup=False)
            yield Select([(Text(literal(f"{pr['state']} | {pr['title']} | {pr['url']}")),pr['url']) for pr in self.prs],
                         prompt='Choose a PR',id='day-pr-choice')
            yield Button('Open details',id='day-pr-open')
            yield Button('Back',id='day-pr-back')

    def action_close(self): self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        value=self.query_one('#day-pr-choice',Select).value
        if event.button.id=='day-pr-back': self.action_close()
        elif event.button.id=='day-pr-open' and any(pr['url']==value for pr in self.prs): self.dismiss(value)
