"""Merge confirmation client. All GitHub reads and writes belong to the application."""

from copy import deepcopy
import uuid

from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from .pr_merging import METHODS
from .tui_close import literal


class MergePRScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel'), Binding('ctrl+enter', 'confirm', 'Merge PR', priority=True)]
    CSS = """
    MergePRScreen { align: center middle; background: $background 75%; }
    MergePRScreen > Vertical { width: 86; max-width: 96%; height: 90%; max-height: 32;
        padding: 1 2; background: $surface; border: round $warning; }
    MergePRScreen Static { height: auto; margin-bottom: 1; }
    MergePRScreen #merge-fields { height: 1fr; }
    MergePRScreen #merge-heading { text-style: bold; color: $warning; }
    MergePRScreen #merge-message { max-height: 5; color: $text-muted; }
    MergePRScreen Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    MergePRScreen Button { min-width: 10; margin-left: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)
        self.key = uuid.uuid4().hex
        self.preview = None
        self.pending = None
        self.busy = False
        self.last_status = None
        previous = self.record.get('merge_request')
        if previous and previous['status'] in ('running', 'uncertain'):
            self.key = previous['key']
            self.pending = {key: previous[key] for key in ('key', 'url', 'head_sha', 'method')}
            self.pending['confirmed'] = True
            self.last_status = previous['status']

    def compose(self):
        with Vertical():
            yield Static('Merge PR on GitHub', id='merge-heading', markup=False)
            with VerticalScroll(id='merge-fields'):
                yield Static(literal(self.record.get('github', {}).get('title') or self.record['url']), markup=False)
                yield Static(self.record['url'], id='merge-url', markup=False)
                yield Static('Loading the current head commit and allowed merge methods...', id='merge-head', markup=False)
                yield Select([], prompt='Choose a merge method', id='merge-method', disabled=True)
                yield Static('Confirmation merges this exact commit on GitHub. Repository rules still apply. '
                             'This does not enable auto-merge or request branch deletion. '
                             'GitHub can still delete the head branch through repository policy.', markup=False)
            yield Static('Ctrl+Enter confirms. Esc cancels before submission.', id='merge-message', markup=False)
            with Horizontal():
                yield Button('Open PR', id='merge-open')
                yield Button('Cancel', id='merge-cancel')
                yield Button('Merge PR', id='merge-confirm', variant='warning', disabled=True)

    def on_mount(self):
        self.query_one('#merge-cancel', Button).focus()
        if self.pending:
            selector = self.query_one('#merge-method', Select)
            selector.set_options([(METHODS[self.pending['method']], self.pending['method'])])
            selector.value = self.pending['method']
            self.query_one('#merge-head', Static).update('Confirmed head: '+self.pending['head_sha'])
            self.query_one('#merge-message', Static).update(literal(self.record['merge_request']['message']))
            self.update_buttons()
        else:
            self.load_preview()
        self.set_interval(1, self.poll_result)

    @work(group='merge-preview', exit_on_error=False)
    async def load_preview(self):
        try:
            value = await self.app.call_api('merge_preview', {'url': self.record['url']})
            if not self.is_mounted:
                return
            if value['url'] != self.record['url'] or not value['methods'] or any(method not in METHODS for method in value['methods']):
                raise ValueError('Invalid merge preview')
            self.preview = value
            self.query_one('#merge-head', Static).update('Head to merge: '+literal(value['head_sha']))
            selector = self.query_one('#merge-method', Select)
            selector.set_options([(METHODS[method], method) for method in value['methods']])
            selector.disabled = False
            if len(value['methods']) == 1:
                selector.value = value['methods'][0]
        except Exception as exc:
            if self.is_mounted:
                self.query_one('#merge-message', Static).update(literal(str(exc)))
        finally:
            if self.is_mounted:
                self.update_buttons()

    @on(Select.Changed, '#merge-method')
    def method_changed(self, event):
        self.update_buttons()

    def update_buttons(self):
        if not self.is_mounted:
            return
        selected = self.query_one('#merge-method', Select).value
        self.query_one('#merge-cancel', Button).disabled = self.busy
        button = self.query_one('#merge-confirm', Button)
        button.disabled = (self.busy or self.last_status == 'failed' or
                           self.pending is None and (not self.preview or selected not in self.preview['methods']))
        button.label = 'Working...' if self.busy else ('Check outcome' if self.pending else 'Merge PR')

    def action_cancel(self):
        if not self.busy:
            self.dismiss(None)

    def action_confirm(self):
        if self.busy or self.last_status == 'failed':
            return
        if self.pending is None:
            method = self.query_one('#merge-method', Select).value
            if not self.preview or method not in self.preview['methods']:
                return
            self.pending = {'key': self.key, 'url': self.record['url'], 'head_sha': self.preview['head_sha'],
                            'method': method, 'confirmed': True}
            self.query_one('#merge-method', Select).disabled = True
        self.busy = True
        self.update_buttons()
        self.send(check=self.last_status in ('running', 'uncertain'))

    def poll_result(self):
        if self.last_status == 'running' and not self.busy:
            self.busy = True
            self.update_buttons()
            self.send(check=True)

    @work(group='merge-pr-request', exit_on_error=False)
    async def send(self, check=False):
        try:
            result = await self.app.call_api('merge_result', {'key': self.key, 'check_github': self.last_status == 'uncertain'}) if check else await self.app.call_api('merge_pr', deepcopy(self.pending))
            self.last_status = result['status']
            if self.last_status in ('succeeded', 'already_merged'):
                self.dismiss(result)
                return
            self.query_one('#merge-message', Static).update(literal(result['message']))
        except Exception:
            if self.is_mounted:
                self.query_one('#merge-message', Static).update(
                    'Response unavailable. Check outcome reuses this request key and never repeats a recorded write.')
        finally:
            self.busy = False
            if self.is_mounted:
                self.update_buttons()

    @on(Button.Pressed)
    def button_pressed(self, event):
        event.stop()
        if event.button.id == 'merge-confirm':
            self.action_confirm()
        elif event.button.id == 'merge-open':
            self.app.open_reference(self.record['url'], 'pr')
        elif event.button.id == 'merge-cancel':
            self.action_cancel()
