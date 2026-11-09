"""Follow-up client. Reads are cheap; only Analyze requests cloud processing."""

from copy import deepcopy
import uuid

from rich.text import Text
from textual import on,work
from textual.binding import Binding
from textual.containers import Horizontal,Vertical,VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button,DataTable,Input,Label,Select,Static,TabbedContent,TabPane,TextArea

from .tui_close import literal


class TaskFollowupForm(ModalScreen):
    BINDINGS=[Binding('escape','close','Back'),Binding('ctrl+enter','save','Save',priority=True)]
    CSS='''
    TaskFollowupForm { align: center middle; background: $background 75%; }
    TaskFollowupForm > Vertical { width: 86; max-width: 96%; height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    TaskFollowupForm VerticalScroll { height: 1fr; }
    TaskFollowupForm TextArea { height: 5; }
    TaskFollowupForm Static { height: auto; max-height: 4; }
    TaskFollowupForm Horizontal { height: 3; align-horizontal: right; }
    '''

    def __init__(self,candidate):
        super().__init__();self.candidate=deepcopy(candidate);self.value=None;self.pending=None;self.busy=False

    def compose(self):
        with Vertical():
            yield Static('Task follow-up: '+literal(self.candidate['title']),markup=False)
            with VerticalScroll():
                yield Static('Task ID: '+self.candidate['task_id'],markup=False)
                yield Label('Next concrete action');yield TextArea(id='followup-next-action')
                yield Label('Follow-on work after this Task');yield TextArea(id='followup-after-task')
                yield Label('Waiting on (person or condition)');yield Input(id='followup-waiting-on',max_length=2000)
                yield Label('Dependencies (comma-separated Task IDs)');yield Input(id='followup-dependencies')
                yield Label('Revisit after YYYY-MM-DD (blank clears)');yield Input(id='followup-revisit-after')
                yield Static('Dependencies are explicit facts, not inferred from PR numbers. No Task, PR, meeting or Slack action is executed.',markup=False)
            yield Static('Loading...',id='followup-form-status',markup=False)
            with Horizontal():
                yield Button('Back',id='followup-form-back')
                yield Button('Save',id='followup-form-save',disabled=True)

    def on_mount(self):self.load()

    @work(group='followup-form-load',exit_on_error=False)
    async def load(self):
        try:
            value=await self.app.call_api('followup_task',{'task_id':self.candidate['task_id']})
            if not self.is_mounted:return
            self.value=value
            for field in ('next_action','after_task'):self.query_one('#followup-'+field.replace('_','-'),TextArea).load_text(value[field])
            self.query_one('#followup-waiting-on',Input).value=value['waiting_on']
            self.query_one('#followup-dependencies',Input).value=', '.join(value['dependencies'])
            self.query_one('#followup-revisit-after',Input).value=value['revisit_after'] or ''
            self.query_one('#followup-form-status',Static).update('Only follow-up metadata changes. Ctrl+Enter saves.')
            self.query_one('#followup-form-save',Button).disabled=False
        except Exception:
            if self.is_mounted:self.query_one('#followup-form-status',Static).update('Metadata unavailable. Reopen after reconnection.')

    def action_close(self):
        if not self.busy:self.dismiss()

    def action_save(self):
        if not self.value or self.busy:return
        if self.pending is None:
            proposed={
                'next_action':self.query_one('#followup-next-action',TextArea).text,
                'after_task':self.query_one('#followup-after-task',TextArea).text,
                'waiting_on':self.query_one('#followup-waiting-on',Input).value,
                'dependencies':[item.strip() for item in self.query_one('#followup-dependencies',Input).value.split(',') if item.strip()],
                'revisit_after':self.query_one('#followup-revisit-after',Input).value.strip() or None}
            changes={field:value for field,value in proposed.items() if value!=self.value[field]}
            if not changes:
                self.query_one('#followup-form-status',Static).update('There are no follow-up changes to save.')
                return
            self.pending={'task_id':self.candidate['task_id'],'expected_version':self.value['version'],**changes}
            for widget in [*self.query(Input),*self.query(TextArea)]:widget.disabled=True
        self.busy=True;self.save()

    @work(group='followup-form-save',exit_on_error=False)
    async def save(self):
        try:
            result=await self.app.call_api('followup_task',deepcopy(self.pending))
            if self.is_mounted:self.dismiss(result)
        except ValueError as exc:
            if self.is_mounted:self.query_one('#followup-form-status',Static).update(literal(exc)+' Reopen to review current data.')
        except Exception:
            if self.is_mounted:self.query_one('#followup-form-status',Static).update('Response unknown. Retry sends the same frozen values and version.')
        finally:self.busy=False

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        if event.button.id=='followup-form-save':self.action_save()
        elif event.button.id=='followup-form-back':self.action_close()


class FollowupScreen(ModalScreen):
    BINDINGS=[Binding('escape','close','Back'),Binding('r','reload','Reload facts')]
    CSS='''
    FollowupScreen { align: center middle; background: $background 75%; }
    FollowupScreen > Vertical { width: 98%; height: 96%; padding: 0 1; background: $surface; border: round $accent; }
    FollowupScreen #followup-advice { height: auto; max-height: 4; }
    FollowupScreen #followup-filters { height: 3; }
    FollowupScreen Select { width: 1fr; }
    FollowupScreen TabbedContent { height: 1fr; min-height: 4; }
    FollowupScreen TabPane { padding: 0; }
    FollowupScreen DataTable { height: 1fr; }
    FollowupScreen #followup-detail { height: auto; max-height: 3; }
    FollowupScreen #followup-status { height: auto; max-height: 2; }
    FollowupScreen #followup-buttons { height: 3; }
    FollowupScreen Button { width: auto; min-width: 6; margin: 0; padding: 0 1; }
    '''

    def __init__(self):
        super().__init__();self.data=None;self.scope={'project':None,'workstream':None,'stale_days':7}
        self.analysis_key=None;self.analysis_params=None;self.submitting=False;self.rows={};self.selection={};self.loading=False;self.connected=False;self.decision_tag=None

    def compose(self):
        with Vertical():
            yield Static('Follow-up: advice only. No automatic actions or cloud calls.',markup=False)
            yield Static('Loading tracked work...',id='followup-advice',markup=False)
            with Horizontal(id='followup-filters'):
                yield Select([('All projects','')],value='',allow_blank=False,id='followup-project')
                yield Select([('All workstreams','')],value='',allow_blank=False,id='followup-workstream')
                yield Select([('7-day signals',7),('14-day signals',14),('30-day signals',30)],value=7,allow_blank=False,id='followup-days')
            with TabbedContent(id='followup-tabs'):
                with TabPane('What next?',id='followup-next-pane'):yield DataTable(id='followup-next',cursor_type='row',zebra_stripes=True)
                with TabPane('Through the cracks',id='followup-stale-pane'):yield DataTable(id='followup-stale',cursor_type='row',zebra_stripes=True)
            yield Static('',id='followup-detail',markup=False)
            yield Static('',id='followup-status',markup=False)
            with Horizontal(id='followup-buttons'):
                yield Button('Analyze AI',id='followup-analyze',disabled=True)
                yield Button('Reload',id='followup-reload')
                yield Button('Next steps',id='followup-edit',disabled=True)
                yield Button('Open PR',id='followup-open',disabled=True)
                yield Button('Back',id='followup-back')

    def on_mount(self):
        for table in self.query(DataTable):
            for title in ('Tracked work','Possible action','Project / workstream'):table.add_column(title)
        self.load();self.set_interval(3,self.load)

    def active_table(self):
        return 'followup-stale' if self.query_one('#followup-tabs',TabbedContent).active=='followup-stale-pane' else 'followup-next'

    def selected(self):return self.rows.get(self.active_table(),{}).get(self.selection.get(self.active_table()))

    @work(group='followup-read',exclusive=True,exit_on_error=False)
    async def load(self):
        scope=deepcopy(self.scope)
        try:
            value=await self.app.call_api('followup',scope)
            if not self.is_mounted or scope!=self.scope:return
            self.data=value;self.loading=True;self.connected=True
            for selector,choices,current in (('project',value['projects'],scope['project']),('workstream',value['workstreams'],scope['workstream'])):
                widget=self.query_one('#followup-'+selector,Select)
                widget.set_options([('All '+selector+'s',''),*[(Text(literal(name)),name) for name in choices]])
                widget.value=current or ''
            self.loading=False
            decision=value['next'];indexed={item['id']:item for item in value['candidates']}
            tag=(tuple(decision['candidate_ids']),value.get('ai_report_key'),tuple(sorted(scope.items())))
            new_decision=tag!=self.decision_tag
            if new_decision:
                self.selection['followup-next']=next(iter(decision['candidate_ids']),None)
                self.decision_tag=tag
            lines=['What should I do next? '+decision['mode'].upper()+(' (AI interpretation)' if value.get('ai_report_key') else '')]
            if decision['question']:lines.append(decision['question'])
            elif decision['candidate_ids']:
                item=indexed[decision['candidate_ids'][0]];lines.append(item['action']+' | '+item['title'])
            lines.append(decision['reason'])
            self.query_one('#followup-advice',Static).update(literal('\n'.join(lines)))
            for identifier,items in (('followup-next',value['candidates']),('followup-stale',value['stale'])):
                table=self.query_one('#'+identifier,DataTable);table.clear()
                self.rows[identifier]={item['id']:item for item in items}
                for item in items:table.add_row(Text(('* ' if item['id'] in decision['candidate_ids'] else '')+literal(item['title'])),Text(literal(item['action'])),Text(literal(item['project']+' / '+item['workstream'])),key=item['id'])
                index=next((index for index,item in enumerate(items) if item['id']==self.selection.get(identifier)),0)
                self.selection[identifier]=items[index]['id'] if items else None
                if items:table.move_cursor(row=index,animate=False,scroll=bool(new_decision and identifier=='followup-next'))
            analysis=value.get('analysis') or {};active=analysis.get('status') in ('queued','running')
            if self.analysis_key and analysis.get('key')==self.analysis_key:
                self.analysis_key=None;self.analysis_params=None
            configured=value['gateway']['configured']
            button=self.query_one('#followup-analyze',Button)
            button.disabled=not configured or active or self.submitting
            button.label='Retry request' if self.analysis_key else 'Analyze AI'
            status=(('AI '+analysis['status']+'. ') if analysis else '')+('Gateway configured. Analysis is explicit.' if configured else 'Gateway key missing: run work-tracker follow-up key in a terminal.')
            usage=(analysis.get('result') or {}).get('usage') or {}
            if usage:status+=f" Calls {usage.get('model_calls',0)}, context reads {usage.get('context_reads',0)}, reported tokens {usage.get('total_tokens',0)}."
            status+=f" {len(value['stale'])} flagged; {value['unknown_age_count']} unknown ages. Signals do not prove human inactivity."
            self.query_one('#followup-status',Static).update(literal(status))
            self.detail()
        except Exception:
            self.connected=False
            if self.is_mounted:
                self.query_one('#followup-status',Static).update('Follow-up unavailable. Cached facts remain; reconnect before edits or analysis.')
                self.query_one('#followup-edit',Button).disabled=True
                self.query_one('#followup-analyze',Button).disabled=True
        finally:self.loading=False

    def detail(self):
        if not self.is_mounted:return
        item=self.selected()
        text=('Task '+item['task_id']+'\n'+'\n'.join([*item['signals'],*item['evidence']])) if item else 'No item in this view. Unknown activity is not evidence that work is complete.'
        self.query_one('#followup-detail',Static).update(literal(text))
        self.query_one('#followup-edit',Button).disabled=item is None or not self.connected
        self.query_one('#followup-open',Button).disabled=not(item and item['url'])

    @on(DataTable.RowHighlighted)
    def highlighted(self,event):
        if event.data_table.id in ('followup-next','followup-stale'):
            self.selection[event.data_table.id]=event.row_key.value;self.detail()

    @on(TabbedContent.TabActivated,'#followup-tabs')
    def tabbed(self,event):
        if self.is_mounted:self.detail()

    @on(Select.Changed)
    def changed(self,event):
        if self.loading or not self.is_mounted:return
        mapping={'followup-project':'project','followup-workstream':'workstream','followup-days':'stale_days'}
        field=mapping.get(event.select.id)
        if not field or event.value is Select.BLANK:return
        value=event.value or None
        if self.scope[field]==value:return
        self.scope[field]=value
        if field=='project':self.scope['workstream']=None
        self.load()

    def action_reload(self):self.load()
    def action_close(self):self.dismiss()

    def action_analyze(self):
        if not self.data or not self.connected or self.submitting:return
        if self.analysis_params is None:
            self.analysis_key=uuid.uuid4().hex
            self.analysis_params={'key':self.analysis_key,**deepcopy(self.scope)}
        self.submitting=True;self.analyze()

    @work(group='followup-submit',exit_on_error=False)
    async def analyze(self):
        try:
            await self.app.call_api('followup_analyze',deepcopy(self.analysis_params))
        except ValueError as exc:
            if self.is_mounted:self.app.notify(literal(exc),severity='warning',markup=False)
            self.analysis_key=None;self.analysis_params=None
        except Exception:
            if self.is_mounted:self.app.notify('Analysis response unknown. Reload checks receipts; retry retains the same request key.',severity='warning',markup=False)
        finally:
            self.submitting=False
            if self.is_mounted:self.load()

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop();identifier=event.button.id;item=self.selected()
        if identifier=='followup-back':self.action_close()
        elif identifier=='followup-reload':self.action_reload()
        elif identifier=='followup-analyze':self.action_analyze()
        elif identifier=='followup-open' and item and item['url']:self.app.open_reference(item['url'],'pr')
        elif identifier=='followup-edit' and item and self.connected:self.app.push_screen(TaskFollowupForm(item),lambda result:self.load())
