"""Textual client for the local application. No database or scheduler ownership."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import webbrowser

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

import work_activity as activity
from .client import request
from .tui_forms import IntentForm
from .tui_close import ClosePRScreen
from .state_age import state_age


REVIEW_STATES = ("Ready to Merge", "Changes requested", "Blocked", "Awaiting review")
COLORS = {"Ready to Merge": "green", "Changes requested": "magenta", "Blocked": "red", "Awaiting review": "cyan"}


def plain(value, multiline=False):
    text = str(value) if value is not None else ""
    return ''.join(char if char.isprintable() or multiline and char in '\n\t' else f'\\x{ord(char):02x}' for char in text)


def search_text(record):
    fields = [record.get('url'), record.get('description'), record.get('project'), record.get('task_title'),
              record.get('project_name'), record.get('workstream_name'), record.get('github', {}).get('title'),
              record.get('slack-threads', []), record.get('agent-sessions', [])]
    return json.dumps(fields, ensure_ascii=False).casefold()


def thread_button_id(url):
    return 'open-thread-' + hashlib.sha256(url.encode()).hexdigest()[:16]


def detail_text(record):
    if not record:
        return Text("Select a tracked PR to inspect its local metadata and observation freshness.", style="dim")
    github = record.get('github') or {}
    text = Text()
    def section(label, value):
        text.append(label + '\n', style='bold')
        text.append(plain(value, multiline=True) + '\n\n')
    section(plain(github.get('title') or record.get('task_title') or record['url']), record['url'])
    section('GitHub lifecycle', github.get('state') or 'Not yet verified')
    state = record.get('review_state', 'Awaiting review')
    text.append(state + '\n', style=COLORS.get(state, 'white'))
    text.append(plain(record.get('review_reason') or 'No current GitHub observation') + '\n\n')
    section('Time since state change', state_age(record) + '\n' +
            ('Change first observed: ' + record['state_changed_at'] if record.get('state_changed_at') else
             '≥ means at least this long since first tracking this state; older history is unknown.'))
    section('GitHub observation', record.get('observed_at') or 'Unverified. Request a background refresh.')
    if record.get('refresh_error'):
        section('Stale observation', record['refresh_error'])
    section('Project / Workstream', f"{record.get('project_name', 'Unclassified')} / {record.get('workstream_name', 'Unclassified')}")
    if record.get('project') and record['project'] != record.get('project_name'):
        section('Preserved legacy label (not yet classified)', record['project'])
    section('Task', f"{record.get('task_title') or '(untitled)'}\n{record.get('task_id') or '(no task ID)'}")
    section('Local description', record.get('description') or 'Not set. Press e to edit local metadata.')
    threads = record.get('slack-threads') or []
    section('Slack threads', '\n'.join(f"{t.get('description') or 'Thread'}\n{t['url']}" for t in threads) or 'None attached.')
    sessions = record.get('agent-sessions') or []
    section('Agent sessions (references only)', '\n\n'.join(
        f"{s.get('tool')} on {s.get('machine')}\n{s.get('path')}\nsessionId: {s.get('sessionId')}" for s in sessions) or 'None attached.')
    if github.get('body'):
        body = github['body']
        section('GitHub description', body[:4000] + ('\n[Display shortened. Open the PR for the full text.]' if len(body) > 4000 else ''))
    return text


class DetailScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape', 'close', 'Back')]
    CSS = """
    DetailScreen { align: center middle; background: $background 75%; }
    DetailScreen > #full-detail { width: 100; max-width: 96%; height: 92%; border: round $accent;
        padding: 1 2; background: $surface; }
    DetailScreen #detail-scroll { height: 1fr; }
    DetailScreen #detail-actions { height: 3; }
    DetailScreen Button { min-width: 10; margin-right: 1; }
    DetailScreen #detail-thread-buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)
        self._thread_targets = {thread_button_id(t['url']): t['url'] for t in self.record.get('slack-threads', [])}

    def compose(self):
        with Vertical(id='full-detail'):
            with VerticalScroll(id='detail-scroll'):
                yield Static(detail_text(self.record), id='full-detail-text', markup=False)
                with Vertical(id='detail-thread-buttons'):
                    for index, thread in enumerate(self.record.get('slack-threads') or []):
                        yield Button(f'Open Slack thread {index+1}', id=thread_button_id(thread['url']))
            with Horizontal(id='detail-actions'):
                yield Button('Open PR', id='detail-open')
                yield Button('Project PRs', id='detail-project')
                yield Button('Workstream PRs', id='detail-workstream')
                yield Button('Back', id='detail-back')

    async def update_record(self, record):
        if not self.is_mounted or self not in self.app.screen_stack:
            return
        previous_threads = self.record.get('slack-threads') or []
        self.record = deepcopy(record)
        self.query_one('#full-detail-text', Static).update(detail_text(self.record))
        threads = self.record.get('slack-threads') or []
        if threads != previous_threads:
            self._thread_targets = {thread_button_id(t['url']): t['url'] for t in threads}
            container = self.query_one('#detail-thread-buttons', Vertical)
            await container.remove_children()
            if self.is_mounted and self in self.app.screen_stack and threads:
                await container.mount(*[Button(f'Open Slack thread {index+1}', id=thread_button_id(t['url']))
                                        for index, t in enumerate(threads)])

    def action_close(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def button_pressed(self, event):
        event.stop()
        key = event.button.id or ''
        if key == 'detail-open':
            self.app.open_reference(self.record['url'], 'pr')
        elif key.startswith('open-thread-'):
            url = self._thread_targets.get(key)
            if url:
                self.app.open_reference(url, 'slack')
        elif key == 'detail-project':
            self.dismiss({'project': self.record.get('project_name', 'Unclassified')})
        elif key == 'detail-workstream':
            self.dismiss({'project': self.record.get('project_name', 'Unclassified'),
                          'workstream': self.record.get('workstream_name', 'Unclassified')})
        else:
            self.action_close()


class LinksScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape', 'close', 'Back')]
    CSS = """
    LinksScreen { align: center middle; background: $background 75%; }
    LinksScreen > Vertical { width: 62; max-width: 96%; height: auto; max-height: 90%;
        border: round $accent; background: $surface; padding: 1 2; }
    LinksScreen Static { height: auto; max-height: 3; margin-bottom: 1; }
    LinksScreen Button { width: 100%; margin-bottom: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)

    def compose(self):
        with Vertical():
            yield Static('Local PR references', markup=False)
            yield Static(self.record['url'], markup=False)
            yield Button('Add Slack thread', id='links-slack')
            yield Button('Add agent session reference', id='links-session')
            yield Button('Remove Slack link', id='links-remove', disabled=not self.record.get('slack-threads'))
            yield Button('Back', id='links-back')

    def action_close(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def button_pressed(self, event):
        event.stop()
        mode = {'links-slack': 'slack', 'links-session': 'session', 'links-remove': 'remove_slack'}.get(event.button.id)
        self.dismiss({'mode': mode, 'record': self.record} if mode else None)


class WorkTrackerApp(App):
    TITLE = 'Work tracker'
    SUB_TITLE = 'Open PRs · local application'
    BINDINGS = [Binding('q', 'quit', 'Quit'), Binding('/', 'search', 'Search'),
                Binding('r', 'refresh_prs', 'Refresh GitHub'), Binding('e', 'edit', 'Edit'),
                Binding('n', 'register', 'Track PR'), Binding('t', 'slack', 'Slack link', show=False),
                Binding('s', 'session', 'Agent session', show=False), Binding('d', 'remove_slack', 'Unlink Slack', show=False),
                Binding('o', 'open_pr', 'Open PR', show=False), Binding('x', 'close_pr', 'Close PR'),
                Binding('c', 'clear_filters', 'Clear filters', show=False), Binding('escape', 'table', 'PR list', show=False)]
    CSS = """
    Screen { background: $background; }
    #summary { height: auto; max-height: 3; padding: 0 1; background: $primary 15%; }
    #connection { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    #connection.offline { color: $error; }
    #toolbar { height: 3; padding: 0 1; }
    #toolbar Button { min-width: 8; width: auto; margin-right: 1; }
    #filters { height: 3; grid-size: 4; grid-columns: 2fr 1fr 1fr 1fr; grid-gutter: 0 1; padding: 0 1; }
    #body { height: 1fr; padding: 0 1; }
    #list-pane { width: 65%; height: 1fr; }
    #list-count { height: 1; color: $text-muted; }
    #prs { height: 1fr; border: round $primary; }
    #empty { height: auto; max-height: 4; padding: 0 1; color: $text-muted; }
    #preview { width: 35%; height: 1fr; border: round $primary; margin-left: 1; padding: 0 1; }
    #preview-title { text-style: bold; height: 1; color: $accent; }
    #preview-content { height: 1fr; }
    Screen.narrow #preview { display: none; }
    Screen.narrow #list-pane { width: 100%; }
    Screen.narrow #filters { grid-size: 2; grid-columns: 1fr 1fr; height: 6; }
    Screen.narrow #toolbar Button { min-width: 6; padding: 0 1; margin-right: 0; }
    """

    def __init__(self, root, rpc=request, opener=None, poll_interval=3):
        super().__init__()
        self.root = Path(root).expanduser().resolve()
        self.rpc = rpc
        self.opener = opener or webbrowser.open
        self.poll_interval = poll_interval
        self.all_rows = []
        self.visible_rows = []
        self.selected_url = None
        self.backend_status = {}
        self.inventory = None
        self.connected = False
        self.loaded = False
        self._fetching = False
        self._load_again = False
        self._refresh_requesting = False
        self._main = None
        self._project = self._workstream = self._review = self._search = ''
        self._project_options = self._workstream_options = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static('Connecting to the local application...', id='summary', markup=False)
        yield Static('The application owns the datastore and scheduled jobs.', id='connection', markup=False)
        with Horizontal(id='toolbar'):
            yield Button('Track PR', id='track-pr', variant='primary')
            yield Button('Refresh GitHub', id='refresh-prs')
            yield Button('Edit', id='edit-pr')
            yield Button('Close PR', id='close-pr', variant='warning')
            yield Button('Details', id='details-pr')
            yield Button('Links', id='manage-links')
            yield Button('Clear filters', id='clear-filters')
            yield Button('Inventory', id='open-inventory')
        with Grid(id='filters'):
            yield Input(placeholder='Search tracked names, descriptions, and links', id='search')
            yield Select([('All projects', '')], value='', allow_blank=False, id='project-filter')
            yield Select([('All workstreams', '')], value='', allow_blank=False, id='workstream-filter')
            yield Select([('All review states', ''), *[(state, state) for state in REVIEW_STATES]],
                         value='', allow_blank=False, id='review-filter')
        with Horizontal(id='body'):
            with Vertical(id='list-pane'):
                yield Static('Loading tracked PRs...', id='list-count', markup=False)
                yield DataTable(id='prs', cursor_type='row', zebra_stripes=True)
                yield Static('', id='empty', markup=False)
            with Vertical(id='preview'):
                yield Static('PR detail · Enter for full view', id='preview-title', markup=False)
                with VerticalScroll(id='preview-content'):
                    yield Static(detail_text(None), id='preview-text', markup=False)
        yield Footer()

    def on_mount(self):
        self._main = self.screen
        table = self._main.query_one('#prs', DataTable)
        table.add_column('Review', key='review', width=18)
        table.add_column('PR', key='pr', width=20)
        self.title_column = table.add_column('Title', key='title', width=30)
        table.add_column('Since change', key='state-age', width=12)
        table.focus()
        self._layout()
        self.request_load()
        if self.poll_interval:
            self.set_interval(self.poll_interval, self.request_load)

    def on_resize(self, event):
        self._layout(event.size.width)

    def _layout(self, width=None):
        width = self.size.width if width is None else width
        for screen in self.screen_stack:
            screen.set_class(width < 120, 'narrow')
        if self._main:
            for name, compact, full in (('refresh-prs', 'Refresh', 'Refresh GitHub'),
                                        ('clear-filters', 'Clear', 'Clear filters'),
                                        ('track-pr', 'Track', 'Track PR'), ('close-pr', 'Close', 'Close PR'),
                                        ('open-inventory', 'Totals', 'Inventory')):
                self._main.query_one('#'+name, Button).label = compact if width < 120 else full
            self.call_after_refresh(self._resize_columns)

    def _resize_columns(self):
        table = self._main.query_one('#prs', DataTable)
        if self.title_column in table.columns:
            table.columns[self.title_column].width = max(16, table.size.width - 60)
            table.refresh(layout=True)

    async def call_api(self, method, params=None):
        return await asyncio.to_thread(self.rpc, self.root, method, params or {})

    def _read_all_pages(self):
        for attempt in range(2):
            rows, offset = {}, 0
            inventory = None
            while True:
                page = self.rpc(self.root, 'list_prs', {'state': 'open', 'limit': 200, 'offset': offset})
                if not isinstance(page, dict) or not isinstance(page.get('items'), list) or type(page.get('total')) is not int:
                    raise ValueError('Invalid local PR-list response')
                inventory = page.get('inventory')
                for record in page['items']:
                    rows[record['url']] = record
                offset += len(page['items'])
                if offset >= page['total']:
                    break
                if not page['items']:
                    raise ValueError('Local PR list was incomplete. Retry the view.')
            if len(rows) == page['total']:
                return list(rows.values()), inventory
        raise ValueError('The register changed while paging. The previous view is retained until the next reload.')

    def request_load(self):
        if self._fetching:
            self._load_again = True
            return
        self._fetching = True
        self.load_view()

    @work(group='view-load', exit_on_error=False)
    async def load_view(self):
        try:
            (rows, inventory), status = await asyncio.gather(
                asyncio.to_thread(self._read_all_pages), self.call_api('status'))
            self.all_rows = rows
            self.inventory = inventory or status.get('inventory')
            self.backend_status = status
            self.connected = self.loaded = True
            self._set_options()
            self._render_rows()
            self._render_status()
            for screen in list(self.screen_stack):
                if isinstance(screen, DetailScreen):
                    current = next((r for r in rows if r['url'] == screen.record['url']), None)
                    if current is None:
                        current = await self.call_api('get_pr', {'url': screen.record['url']})
                    if current is not None and screen.is_mounted and screen in self.screen_stack:
                        await screen.update_record(current)
        except Exception as exc:
            self.connected = False
            message = f'Cannot update the local view: {plain(exc)}'
            if self.loaded:
                message += ' Last loaded records are retained.'
            self._main.query_one('#connection', Static).update(message)
            self._main.query_one('#connection').add_class('offline')
            if not self.loaded:
                self._main.query_one('#list-count', Static).update('Application unavailable, not zero PR activity.')
                self._main.query_one('#empty', Static).update('Start the local application, then press r to reconnect.')
        finally:
            self._fetching = False
            self._update_buttons()
            if self._load_again and self.is_running:
                self._load_again = False
                self.call_later(self.request_load)

    def _set_options(self):
        projects = sorted({value.get('project_name', 'Unclassified') for value in self.all_rows} | ({self._project} if self._project else set()))
        if projects != self._project_options:
            self._project_options = projects
            widget = self._main.query_one('#project-filter', Select)
            widget.set_options([('All projects', ''), *[(Text(plain(name)), name) for name in projects]])
            widget.value = self._project
        workstreams = sorted({value.get('workstream_name', 'Unclassified') for value in self.all_rows
                              if not self._project or value.get('project_name') == self._project}
                             | ({self._workstream} if self._workstream else set()))
        if workstreams != self._workstream_options:
            self._workstream_options = workstreams
            widget = self._main.query_one('#workstream-filter', Select)
            widget.set_options([('All workstreams', ''), *[(Text(plain(name)), name) for name in workstreams]])
            widget.value = self._workstream

    def _render_rows(self):
        previous = self.selected_url
        query = self._search.casefold()
        self.visible_rows = [value for value in self.all_rows
                             if (not self._project or value.get('project_name') == self._project)
                             and (not self._workstream or value.get('workstream_name') == self._workstream)
                             and (not self._review or value.get('review_state') == self._review)
                             and (not query or query in search_text(value))]
        table = self._main.query_one('#prs', DataTable)
        with self.batch_update():
            table.clear()
            for record in self.visible_rows:
                state = record.get('review_state', 'Awaiting review')
                parts = urlsplit(record['url']).path.strip('/').split('/')
                short = f'{parts[1]} #{parts[-1]}' if len(parts) >= 4 else record['url']
                title = record.get('github', {}).get('title') or record.get('task_title') or record['url']
                table.add_row(Text(plain(state), style=COLORS.get(state, 'white')), Text(plain(short)[:60]),
                              Text(plain(title)[:180]), Text(state_age(record), style='dim'), key=record['url'])
            selected = next((index for index, value in enumerate(self.visible_rows) if value['url'] == previous), 0)
            self.selected_url = self.visible_rows[selected]['url'] if self.visible_rows else None
            if self.visible_rows:
                table.move_cursor(row=selected, animate=False, scroll=False)
        self._main.query_one('#list-count', Static).update(f'{len(self.visible_rows)} matching / {len(self.all_rows)} open tracked PRs')
        empty = '' if self.visible_rows else ('No PRs match these filters. Clear filters to see the loaded register.' if self.all_rows
                                              else 'No open tracked PRs. Track an existing PR to start.')
        self._main.query_one('#empty', Static).update(empty)
        self._render_detail()
        self._update_buttons()
        self._resize_columns()

    def _render_status(self):
        inventory = self.inventory or {}
        summary = f"Open tracked: {len(self.all_rows)} · Total tracked: {self.backend_status.get('prs', '?')}"
        if 'total_open' in inventory:
            summary += f" · GitHub open: {inventory['total_open']} · Untracked estimate: {inventory.get('untracked_open_estimate', '?')}"
        self._main.query_one('#summary', Static).update(summary)
        job = next((value for value in self.backend_status.get('jobs', []) if value['name'] == 'refresh_prs'), {})
        state = job.get('status', 'unknown')
        message = f"Connected · GitHub refresh: {state} · Last complete: {job.get('last_finished') or 'not yet'}"
        if job.get('last_error'):
            message += f" · Last error: {plain(job['last_error'])}"
        if inventory.get('error'):
            message += f" · Inventory warning: {plain(inventory['error'])}"
        elif inventory.get('observed_at'):
            message += f" · Counts observed: {inventory['observed_at']}"
        widget = self._main.query_one('#connection', Static)
        widget.update(message)
        widget.remove_class('offline')

    @property
    def selected_record(self):
        return next((value for value in self.visible_rows if value['url'] == self.selected_url), None)

    def _render_detail(self):
        self._main.query_one('#preview-text', Static).update(detail_text(self.selected_record))

    def _update_buttons(self):
        if not self._main:
            return
        self._main.query_one('#track-pr', Button).disabled = not self.connected
        self._main.query_one('#edit-pr', Button).disabled = not self.connected or self.selected_record is None
        self._main.query_one('#details-pr', Button).disabled = self.selected_record is None
        self._main.query_one('#close-pr', Button).disabled = not self.connected or self.selected_record is None
        self._main.query_one('#manage-links', Button).disabled = not self.connected or self.selected_record is None
        self._main.query_one('#refresh-prs', Button).disabled = self._refresh_requesting
        self._main.query_one('#open-inventory', Button).disabled = not (self.inventory or {}).get('github_url')

    @on(Input.Changed, '#search')
    def search_changed(self, event):
        self._search = event.value
        self._render_rows()

    @on(Select.Changed)
    def filter_changed(self, event):
        if not isinstance(event.value, str) or not self._main:
            return
        key = event.select.id
        if key == 'project-filter' and event.value != self._project:
            self._project = event.value
            self._workstream = ''
            self._workstream_options = None
            self._set_options()
            self._main.query_one('#workstream-filter', Select).value = ''
        elif key == 'workstream-filter' and event.value != self._workstream:
            self._workstream = event.value
        elif key == 'review-filter' and event.value != self._review:
            self._review = event.value
        else:
            return
        self._render_rows()

    @on(DataTable.RowHighlighted, '#prs')
    def row_highlighted(self, event):
        key = event.row_key.value
        if any(value['url'] == key for value in self.visible_rows):
            self.selected_url = key
            self._render_detail()
            self._update_buttons()

    @on(DataTable.RowSelected, '#prs')
    def row_selected(self, event):
        self.action_details()

    @on(Button.Pressed)
    def button_pressed(self, event):
        actions = {'track-pr': self.action_register, 'refresh-prs': self.action_refresh_prs,
                   'edit-pr': self.action_edit, 'close-pr': self.action_close_pr,
                   'details-pr': self.action_details, 'manage-links': self.action_links,
                   'clear-filters': self.action_clear_filters, 'open-inventory': self.action_inventory}
        action = actions.get(event.button.id)
        if action:
            event.stop()
            action()

    def check_action(self, action, parameters):
        if len(self.screen_stack) > 1 and action in {'quit', 'search', 'refresh_prs', 'edit', 'register', 'slack', 'session', 'remove_slack', 'links', 'open_pr', 'clear_filters', 'table', 'close_pr'}:
            return False
        return True

    def action_search(self):
        self._main.query_one('#search', Input).focus()

    def action_table(self):
        self._main.query_one('#prs', DataTable).focus()

    def action_clear_filters(self):
        self._project = self._workstream = self._review = self._search = ''
        self._main.query_one('#search', Input).value = ''
        for name in ('project', 'workstream', 'review'):
            self._main.query_one(f'#{name}-filter', Select).value = ''
        self._set_options()
        self._render_rows()

    def action_details(self):
        if self.selected_record:
            self.push_screen(DetailScreen(self.selected_record), self._detail_closed)

    def _detail_closed(self, filters):
        if filters:
            self._project = filters.get('project', '')
            self._workstream = filters.get('workstream', '')
            self._set_options()
            self._main.query_one('#project-filter', Select).value = self._project
            self._main.query_one('#workstream-filter', Select).value = self._workstream
            self._render_rows()
        self._layout()

    def open_reference(self, url, kind):
        try:
            if kind == 'pr':
                activity.pr_url(url)
            elif kind == 'slack':
                activity.slack_url(url)
            else:
                parts = urlsplit(url)
                if parts.scheme != 'https' or parts.netloc != 'github.com' or parts.path != '/pulls':
                    raise ValueError('The inventory link is not a GitHub PR search URL.')
            self.opener(url)
        except Exception as exc:
            self.notify(f'Could not open link: {plain(exc)}', severity='error', markup=False)

    def action_open_pr(self):
        if self.selected_record:
            self.open_reference(self.selected_record['url'], 'pr')

    def action_inventory(self):
        url = (self.inventory or {}).get('github_url')
        if url:
            self.open_reference(url, 'inventory')

    def action_close_pr(self):
        if self.connected and self.selected_record and len(self.screen_stack) == 1:
            self.push_screen(ClosePRScreen(self.selected_record), self._close_pr_finished)

    def _close_pr_finished(self, result):
        if result:
            self.notify(plain(result['message']), markup=False)
        self.request_load()
        self._layout()

    def action_links(self):
        if self.connected and self.selected_record:
            self.push_screen(LinksScreen(self.selected_record), self._links_closed)

    def _links_closed(self, result):
        if result:
            self.open_form(result['mode'], record=result['record'])
        self._layout()

    def open_form(self, mode, record=None):
        if not self.connected:
            self.notify('The application is unavailable. Reconnect before editing.', severity='error', markup=False)
            return
        record = self.selected_record if record is None else record
        if mode != 'register' and not record:
            return
        self.push_screen(IntentForm(mode, record), self._form_closed)

    def action_register(self):
        self.open_form('register')

    def action_edit(self):
        self.open_form('edit')

    def action_slack(self):
        self.open_form('slack')

    def action_session(self):
        self.open_form('session')

    def action_remove_slack(self):
        if not self.selected_record or not self.selected_record.get('slack-threads'):
            self.notify('No Slack links are attached to the selected PR.', markup=False)
            return
        self.open_form('remove_slack')

    def _form_closed(self, result):
        if result:
            self.selected_url = result['url']
            self.notify('Local intent saved. Open-PR filters still apply.', markup=False)
        self.request_load()
        self._layout()

    def action_refresh_prs(self):
        if not self.connected:
            self.request_load()
            return
        if not self._refresh_requesting:
            self._refresh_requesting = True
            self._update_buttons()
            self.queue_refresh()

    @work(group='refresh-request', exclusive=True, exit_on_error=False)
    async def queue_refresh(self):
        try:
            await self.call_api('refresh')
            self.notify('GitHub refresh requested in the background application.', markup=False)
            self.request_load()
        except Exception as exc:
            self.notify(f'Refresh request failed: {plain(exc)}', severity='error', markup=False)
        finally:
            self._refresh_requesting = False
            self._update_buttons()


def run(root):
    WorkTrackerApp(root).run()
