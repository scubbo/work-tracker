"""My Day home screen. Planning stays local and separate from PR lifecycle."""

from copy import deepcopy
from datetime import date, timedelta
import uuid

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Static, TabbedContent, TabPane

from .tui_close import literal
from .tui_day_forms import DayItemDetails, DayItemForm, DayPRPicker
from .tui_stats import StatisticsScreen
from .tui_followup import FollowupScreen


TABLES = {'day-goals':'goals','day-awareness':'awareness','day-carry':'carry_candidates'}


class MyDayScreen(Screen):
    BINDINGS = [Binding('q','quit','Quit'),Binding('escape','prs','PR list'),Binding('f4','prs','PR list'),
                Binding('a','add','Add goal'),Binding('w','watch','Watch'),Binding('space','toggle','Open/Done'),
                Binding('e','edit','Edit'),Binding('enter','details','Details'),Binding('delete','remove','Remove'),Binding('c','carry','Carry'),
                Binding('r','reload','Reload'),Binding('ctrl+r','retry','Retry request',show=False),Binding('f3','stats','Daily stats'),Binding('f5','followup','Follow-up')]
    CSS = """
    MyDayScreen { background: $background; padding: 0 1; }
    MyDayScreen #day-heading { height: 1; text-style: bold; color: $accent; }
    MyDayScreen #day-summary { height: auto; max-height: 2; }
    MyDayScreen #day-nav, MyDayScreen #day-actions { height: 3; }
    MyDayScreen Button { width: auto; min-width: 6; padding: 0 1; margin: 0; }
    MyDayScreen #day-tabs { height: 1fr; }
    MyDayScreen TabPane { padding: 0; }
    MyDayScreen DataTable { height: 1fr; }
    MyDayScreen #day-detail { height: auto; max-height: 4; padding: 0 1; background: $primary 10%; }
    MyDayScreen #day-status { height: auto; max-height: 2; color: $text-muted; }
    """

    def __init__(self):
        super().__init__()
        self.data = None
        self.fixed_day = None
        self.connected = False
        self.busy = False
        self.pending = None
        self.reveal_carry = None
        self.selected = {}

    def compose(self):
        yield Static('My Day',id='day-heading',markup=False)
        yield Static('Loading the application calendar...',id='day-summary',markup=False)
        with Horizontal(id='day-nav'):
            yield Button('PRs (F4)',id='day-prs')
            yield Button('Today',id='day-today')
            yield Button('Previous',id='day-previous')
            yield Button('Next',id='day-next')
            yield Button('Stats (F3)',id='day-stats')
        with Horizontal(id='day-actions'):
            for label,identifier in (('Add goal','day-add'),('Watch','day-watch'),('Done/Open','day-toggle'),
                                     ('Edit','day-edit'),('Remove','day-remove'),('Carry','day-carry-action'),
                                     ('Linked PRs','day-links'),('Retry','day-retry')):
                yield Button(label,id=identifier,disabled=True)
        with TabbedContent(initial='day-goals-pane',id='day-tabs'):
            for title,pane,table in (('Goals','day-goals-pane','day-goals'),('Awareness','day-awareness-pane','day-awareness'),
                                     ('Carry forward','day-carry-pane','day-carry')):
                with TabPane(title,id=pane):
                    yield DataTable(id=table,cursor_type='row',zebra_stripes=True,show_row_labels=False)
        yield Static('Select an item to see its target and cached PR links.',id='day-detail',markup=False)
        yield Static('Completion affects only a day item. Awareness stays until dismissed. Carry-forward is never automatic.',id='day-status',markup=False)
        yield Footer()

    def on_mount(self):
        for table in self.query(DataTable):
            for label,key,width in (('State/day','state',12),('Goal / awareness','label',34),('Project / workstream','branch',28),('PRs','prs',5)):
                table.add_column(label,key=key,width=width)
        self.query_one('#day-goals',DataTable).focus()
        self.load_day()
        if self.app.poll_interval:
            self.set_interval(self.app.poll_interval,self.action_reload)

    def active_table(self):
        active=self.query_one('#day-tabs',TabbedContent).active
        return {'day-goals-pane':'day-goals','day-awareness-pane':'day-awareness','day-carry-pane':'day-carry'}.get(active,'day-goals')

    def selected_item(self):
        if not self.data: return None
        table=self.active_table()
        values=self.data[TABLES[table]]
        identifier=self.selected.get(table)
        return next((value for value in values if value['id']==identifier),None)

    @work(group='my-day-read',exclusive=True,exit_on_error=False)
    async def load_day(self):
        requested=self.fixed_day
        try:
            value=await self.app.call_api('my_day',{'day':requested} if requested else {})
            if not self.is_mounted or self not in self.app.screen_stack or requested!=self.fixed_day: return
            revealing=self.reveal_carry and self.reveal_carry['day']==value['day']
            if revealing:
                self.selected['day-goals']=self.reveal_carry['item_id']
                self.fixed_day=None if value['editable'] else value['day']
            self.data=value
            self.connected=True
            self.query_one('#day-heading',Static).update('My Day '+value['day']+('' if value['editable'] else ' · read-only history'))
            self.query_one('#day-summary',Static).update(
                f"{value['open_count']} open / {value['done_count']} done · {len(value['awareness'])} awareness · {len(value['carry_candidates'])} carry candidates")
            for identifier,key in TABLES.items():
                table=self.query_one('#'+identifier,DataTable)
                table.clear()
                values=value[key]
                previous=self.selected.get(identifier)
                for item in values:
                    target=item['target']
                    state=item['day'] if key=='carry_candidates' else ('Watching' if item['kind']=='awareness' else ('[x] Done' if item['done'] else '[ ] Open'))
                    label=literal(item['label']).replace('\n',' ')[:160]+(' [notes]' if item.get('notes') else '')
                    branch=literal(target['project_name']+' / '+target['workstream_name'])
                    table.add_row(Text(state),Text(label),Text(branch),str(target['pr_count']),key=item['id'])
                selected=next((index for index,item in enumerate(values) if item['id']==previous),0)
                self.selected[identifier]=values[selected]['id'] if values else None
                if values: table.move_cursor(row=selected,animate=False,scroll=False)
            if revealing:
                self.reveal_carry=None
                self.query_one('#day-tabs',TabbedContent).active='day-goals-pane'
                if self.app.screen is self: self.query_one('#day-goals',DataTable).focus()
            self.render_detail()
        except Exception:
            self.connected=False
            if self.is_mounted:
                self.query_one('#day-status',Static).update('My Day is unavailable. Cached items remain visible. Reconnect before making changes.')
        finally:
            if self.is_mounted: self.update_buttons()

    def render_detail(self):
        item=self.selected_item()
        if item:
            target=item['target']
            detail=(literal(item['label'])+'\n'+literal(f"{target['kind']}: {target['title']} | {target['project_name']} / {target['workstream_name']}")+
                    ('\nNotes (Enter for full text): '+literal(item['notes']).replace('\n',' ')[:140] if item.get('notes') else '')+
                    '\n'+('\n'.join(literal(pr['url']) for pr in target['prs'][:2]) or 'This Task has no PR. Its identity is retained across days.'))
        else:
            detail=('No unfinished previous goals are available to carry into today.' if self.active_table()=='day-carry' else
                    'No item selected. Add a daily goal, keep an awareness item, or use Carry to choose a previous goal.')
        self.query_one('#day-detail',Static).update(detail)
        if self.connected and not self.pending:
            self.query_one('#day-status',Static).update('Open/Done affects only this day. Awareness persists until dismissed. Past goals and underlying Tasks stay unchanged.')
        self.update_buttons()

    def update_buttons(self):
        if not self.is_mounted: return
        item=self.selected_item()
        editable=bool(self.data and self.data['editable'] and self.connected and not self.busy and not self.pending)
        carrying=self.active_table()=='day-carry'
        for identifier in ('day-add','day-watch'):
            self.query_one('#'+identifier,Button).disabled=not editable
        for identifier in ('day-edit','day-remove'):
            self.query_one('#'+identifier,Button).disabled=not(editable and item and not carrying)
        self.query_one('#day-toggle',Button).disabled=not(editable and item and item['kind']=='goal' and not carrying)
        carry=self.query_one('#day-carry-action',Button)
        carry.disabled=not(self.data and self.connected and not self.busy and not self.pending)
        carry.tooltip='Add the selected prior goal to today' if carrying else 'Choose an unfinished previous goal to carry into today'
        self.query_one('#day-remove',Button).label='Dismiss' if item and item['kind']=='awareness' else 'Remove'
        self.query_one('#day-links',Button).disabled=not(item and item['target']['prs'])
        self.query_one('#day-retry',Button).disabled=not(self.pending and not self.busy)

    @on(DataTable.RowHighlighted)
    def highlighted(self,event):
        if event.data_table.id in TABLES:
            self.selected[event.data_table.id]=event.row_key.value
            self.render_detail()

    @on(TabbedContent.TabActivated,'#day-tabs')
    def tab_changed(self,event):
        if self.is_mounted: self.render_detail()

    @on(DataTable.RowSelected)
    def row_selected(self,event):
        if event.data_table.id in TABLES: self.action_details()

    def action_details(self):
        item=self.selected_item()
        if item and self.app.screen is self:
            self.app.push_screen(DayItemDetails(item))

    def action_reload(self): self.load_day()
    def action_prs(self):
        self.app.pop_screen()
        self.app._layout()
    async def action_quit(self): await self.app.action_quit()
    def action_stats(self): self.app.push_screen(StatisticsScreen())
    def action_followup(self): self.app.push_screen(FollowupScreen())

    def open_form(self,kind='goal',item=None):
        if self.data and self.data['editable'] and self.connected and not self.busy and not self.pending:
            self.app.push_screen(DayItemForm(kind,day=self.data['day'],item=item),self.saved)

    def action_add(self): self.open_form()
    def action_watch(self): self.open_form('awareness')
    def action_edit(self):
        if self.active_table()!='day-carry' and self.selected_item(): self.open_form(item=self.selected_item())

    def saved(self,result):
        if result: self.app.notify(literal(result['message']),markup=False)
        self.load_day()

    def mutate(self,action,payload):
        if not self.data or not self.data['editable'] or not self.connected or self.busy or self.pending: return
        self.pending={'key':uuid.uuid4().hex,'action':action,'payload':{'day':self.data['day'],**payload},'source':{'client':'tui'}}
        self.retry()

    def action_toggle(self):
        item=self.selected_item()
        if item and item['kind']=='goal' and self.active_table()!='day-carry':
            self.mutate('set_done',{'item_id':item['id'],'expected_version':item['version'],'done':not item['done']})

    def action_remove(self):
        item=self.selected_item()
        if item and self.active_table()!='day-carry':
            self.mutate('dismiss' if item['kind']=='awareness' else 'remove',{'item_id':item['id'],'expected_version':item['version']})

    def action_carry(self):
        if not self.data or not self.connected:
            self.app.notify('Reconnect to the application before carrying a goal.',severity='warning',markup=False)
            return
        if self.pending or self.busy:
            self.app.notify('Finish or retry the pending request before carrying another goal.',severity='warning',markup=False)
            return
        item=self.selected_item()
        if self.active_table()=='day-carry' and self.data['editable']:
            if item:
                self.mutate('carry',{'item_id':item['id'],'expected_version':item['version']})
            else:
                self.app.notify('No unfinished previous goals are available to carry into today.',markup=False)
            return
        if item and item['kind']=='goal' and item['day']<self.data['today']:
            self.selected['day-carry']=item['id']
        self.fixed_day=None
        self.query_one('#day-tabs',TabbedContent).active='day-carry-pane'
        self.query_one('#day-carry',DataTable).focus()
        self.load_day()

    def action_retry(self): self.retry()

    def retry(self):
        if self.pending and not self.busy:
            self.busy=True
            self.update_buttons()
            self.send()

    @work(group='my-day-write',exit_on_error=False)
    async def send(self):
        try:
            pending=deepcopy(self.pending)
            value=await self.app.call_api('day_intent',pending)
            self.pending=None
            if self.is_mounted:
                if pending['action']=='carry':
                    self.reveal_carry={'day':value['day'],'item_id':value['item_id']}
                    self.fixed_day=value['day']
                    self.app.notify('Carry complete. '+literal(value['message'])+' Showing Goals for '+value['day']+'.',markup=False)
                else:
                    self.app.notify(literal(value['message']),markup=False)
                self.load_day()
        except ValueError as exc:
            self.pending=None
            if self.is_mounted:
                self.app.notify(literal(str(exc)),severity='warning',markup=False)
                self.load_day()
        except Exception:
            if self.is_mounted:
                self.query_one('#day-status',Static).update('Response unavailable. Retry or Ctrl+R sends the same key and payload. Review the outcome before another change.')
        finally:
            self.busy=False
            if self.is_mounted: self.update_buttons()

    def change_day(self,delta=0):
        if not self.data: return
        self.reveal_carry=None
        self.fixed_day=(date.fromisoformat(self.data['day'])+timedelta(days=delta)).isoformat() if delta else None
        self.load_day()

    def linked_prs(self):
        item=self.selected_item()
        if item and item['target']['prs']:
            self.app.push_screen(DayPRPicker(item['target']['prs']),self.pr_selected)

    def pr_selected(self,url):
        if url: self.open_pr(url)

    @work(group='my-day-pr-read',exit_on_error=False)
    async def open_pr(self,url):
        try:
            value=await self.app.call_api('get_pr',{'url':url})
            if not value: raise ValueError('PR is no longer tracked')
            if self.is_mounted:
                from .tui import DetailScreen
                self.app.push_screen(DetailScreen(value),self.pr_details_closed)
        except Exception:
            if self.is_mounted: self.app.notify('The linked PR details are unavailable. No data changed.',severity='warning',markup=False)

    def pr_details_closed(self,filters):
        if filters:
            self.app.pop_screen()
            self.app._detail_closed(filters)

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        actions={'day-prs':self.action_prs,'day-today':lambda:self.change_day(),
                 'day-previous':lambda:self.change_day(-1),'day-next':lambda:self.change_day(1),
                 'day-stats':self.action_stats,'day-add':self.action_add,'day-watch':self.action_watch,
                 'day-toggle':self.action_toggle,'day-edit':self.action_edit,'day-remove':self.action_remove,
                 'day-carry-action':self.action_carry,'day-links':self.linked_prs,'day-retry':self.retry}
        if event.button.id in actions: actions[event.button.id]()
