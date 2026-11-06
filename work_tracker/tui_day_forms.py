"""Day-item forms. The backend owns Task creation and idempotent writes."""

from copy import deepcopy
import uuid

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static
from textual.suggester import SuggestionReady

from .tui_close import literal
from .tui_forms import KnownNameSuggester, canonical_name


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
        target = (self.item or {}).get('target') or {}
        self.catalog = []
        self.project_choices = [target['project_name']] if target.get('project_name') else []
        self.workstream_choices = [target['workstream_name']] if target.get('workstream_name') else []
        self.catalog_loading = not self.pr_url and (not self.item or self.item['target_kind']=='task')

    def compose(self):
        with Vertical():
            yield Static('Edit day item / Task classification' if self.item else ('Add daily goal' if self.kind=='goal' else 'Add persistent awareness'),markup=False)
            with VerticalScroll(id='day-form-fields'):
                yield Static('Quick text creates a Task. Project and workstream are optional. '
                             'A day label never renames or closes its target. Task classification applies to every linked day item and PR.',markup=False)
                yield Input(value=self.item['label'] if self.item else '',placeholder='Goal text or an optional label for an existing target',max_length=2000,id='day-label')
                options=[('New Task from text','new'),('Existing Task','task'),('Existing workstream','workstream')]
                if self.pr_url: options.append(('Selected tracked PR Task','pr'))
                yield Select(options,value='pr' if self.pr_url else 'new',allow_blank=False,id='day-target-kind',disabled=bool(self.item or self.pr_url))
                if self.pr_url: yield Static(self.pr_url,markup=False)
                with Horizontal(id='day-target-search'):
                    yield Input(placeholder='Search Task, workstream, project, or PR URL',id='day-target-query',disabled=True)
                    yield Button('Find',id='day-target-find',disabled=True)
                yield Select([],prompt='Choose a target',id='day-target',disabled=True)
                target = (self.item or {}).get('target') or {}
                yield Static('Existing links inherit classification. Edit an added Task to reassign it. Workstream items inherit their project.',markup=False)
                yield Static('Loading known names...' if self.catalog_loading else 'Classification is inherited by this target.',id='day-classification-status',markup=False)
                yield Label('Project (blank uses Unclassified)')
                yield Input(value=target.get('project_name',''),id='day-project',max_length=200,
                            suggester=KnownNameSuggester(lambda:self.project_choices),disabled=not self.catalog_loading)
                yield Label('Workstream (blank uses Unclassified)')
                yield Input(value=target.get('workstream_name',''),id='day-workstream',max_length=200,
                            suggester=KnownNameSuggester(lambda:self.workstream_choices),disabled=not self.catalog_loading)
                yield Static('',id='day-name-hints',markup=False)
            yield Static('Loading the application calendar day...' if self.day is None else f'Day: {self.day}',id='day-form-message',markup=False)
            with Horizontal(id='day-form-buttons'):
                yield Button('Cancel',id='day-form-cancel')
                yield Button('Save' if self.item else 'Add',id='day-form-save',variant='primary',disabled=self.day is None or self.catalog_loading)

    def on_mount(self):
        self.query_one('#day-label',Input).focus()
        if self.day is None: self.load_day()
        if self.catalog_loading: self.load_known_names()
        searching=not self.item and not self.pr_url and self.query_one('#day-target-kind',Select).value in ('task','workstream')
        self.query_one('#day-target-search').display=bool(searching)
        self.query_one('#day-target',Select).display=bool(searching)

    def classification_enabled(self):
        return (not self.pr_url and (self.item['target_kind']=='task' if self.item else
                self.query_one('#day-target-kind',Select).value=='new'))

    def workstreams_for(self,project):
        names={stream['name'] for value in self.catalog if value['name']==project for stream in value['workstreams']}
        target=(self.item or {}).get('target') or {}
        if target.get('project_name')==project and target.get('workstream_name'):
            names.add(target['workstream_name'])
        return sorted(names,key=lambda name:(name.casefold(),name))

    @work(group='day-form-names',exit_on_error=False)
    async def load_known_names(self):
        try:
            value=await self.app.call_api('catalog',{})
            if not self.is_mounted: return
            projects=value['projects']
            if not isinstance(projects,list) or any(not isinstance(project.get('name'),str) or
                not isinstance(project.get('workstreams'),list) or any(not isinstance(stream.get('name'),str) for stream in project['workstreams']) for project in projects):
                raise ValueError('Invalid hierarchy catalog')
            self.catalog=projects
            self.project_choices=sorted({project['name'] for project in projects}|set(self.project_choices),key=lambda name:(name.casefold(),name))
            self.query_one('#day-classification-status',Static).update('Right Arrow accepts completion. New project/workstream names are allowed.')
        except Exception:
            if self.is_mounted:
                self.query_one('#day-classification-status',Static).update('Known names are unavailable. You can still enter names manually.')
        finally:
            self.catalog_loading=False
            if self.is_mounted:
                self.query_one('#day-form-save',Button).disabled=self.busy or self.day is None
                await self.update_name_hints()

    async def update_name_hints(self):
        if not self.is_mounted: return
        project=canonical_name(self.query_one('#day-project',Input).value.strip() or 'Unclassified',self.project_choices)
        self.workstream_choices=self.workstreams_for(project)
        hints=[]
        for identifier,choices in (('day-project',self.project_choices),('day-workstream',self.workstream_choices)):
            widget=self.query_one('#'+identifier,Input)
            matches=[name for name in choices if name.casefold().startswith(widget.value.strip().casefold())]
            hints.append(('Projects: ' if identifier=='day-project' else 'Workstreams: ')+literal(', '.join(matches[:4]) or 'new names allowed'))
            suggestion=await widget.suggester.get_suggestion(widget.value.casefold()) if widget.value else None
            widget.post_message(SuggestionReady(widget.value,suggestion or ''))
        self.query_one('#day-name-hints',Static).update('\n'.join(hints))

    @on(Input.Changed,'#day-project')
    @on(Input.Changed,'#day-workstream')
    async def name_changed(self,event):
        await self.update_name_hints()

    @work(group='day-form-calendar',exit_on_error=False)
    async def load_day(self):
        try:
            value=await self.app.call_api('my_day',{})
            if self.is_mounted:
                self.day=value['today']
                self.query_one('#day-form-message',Static).update('Day: '+self.day)
                self.query_one('#day-form-save',Button).disabled=self.catalog_loading or self.busy
        except Exception:
            if self.is_mounted:
                self.query_one('#day-form-message',Static).update('The application calendar is unavailable. Reopen this form after reconnection.')

    @on(Select.Changed,'#day-target-kind')
    def mode_changed(self,event):
        if self.item or self.pr_url or not self.is_mounted: return
        enabled=event.value in ('task','workstream')
        self.query_one('#day-target-search').display=enabled
        self.query_one('#day-target',Select).display=enabled
        for selector in ('#day-target-query','#day-target-find','#day-target'):
            self.query_one(selector).disabled=not enabled
        self.targets={}
        self.query_one('#day-target',Select).set_options([])
        for identifier in ('day-project','day-workstream'):
            self.query_one('#'+identifier,Input).disabled=event.value!='new' or self.pending is not None
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
        if self.busy or self.day is None or self.catalog_loading: return
        if self.pending is None:
            label=self.query_one('#day-label',Input).value.strip()
            payload={'day':self.day}
            if self.item:
                payload.update(item_id=self.item['id'],expected_version=self.item['version'])
                if label!=self.item['label']: payload['text']=label
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
            if self.classification_enabled():
                project=canonical_name(self.query_one('#day-project',Input).value.strip() or 'Unclassified',self.project_choices)
                workstream=canonical_name(self.query_one('#day-workstream',Input).value.strip() or 'Unclassified',self.workstreams_for(project))
                previous=self.item['target'] if self.item else {'project_name':'Unclassified','workstream_name':'Unclassified'}
                if (project,workstream)!=(previous['project_name'],previous['workstream_name']):
                    payload.update(project=project,workstream=workstream)
                    if self.item: payload['expected_workstream_id']=previous['workstream_id']
            if self.item and not set(payload)&{'text','project','workstream'}:
                self.query_one('#day-form-message',Static).update('There are no label or Task classification changes to save.')
                return
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
