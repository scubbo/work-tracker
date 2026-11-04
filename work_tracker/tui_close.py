"""Human-only confirmation dialog. The application owns the GitHub operation."""

from copy import deepcopy
import uuid

from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static, TextArea


def literal(value):
    return ''.join(c if c.isprintable() or c == '\n' else f'\\x{ord(c):02x}' for c in str(value))


class ClosePRScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape', 'cancel', 'Cancel'), Binding('ctrl+enter', 'confirm', 'Close PR', priority=True)]
    CSS = """
    ClosePRScreen { align: center middle; background: $background 75%; }
    ClosePRScreen > Vertical { width: 82; max-width: 96%; height: 90%; max-height: 34;
        padding: 1 2; background: $surface; border: round $error; }
    ClosePRScreen Static, ClosePRScreen Label { height: auto; max-height: 4; margin-bottom: 1; }
    ClosePRScreen #close-title { text-style: bold; color: $error; }
    ClosePRScreen #close-fields { height: 1fr; }
    ClosePRScreen #close-comment { height: 7; min-height: 3; }
    ClosePRScreen #close-message { color: $text-muted; }
    ClosePRScreen Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    ClosePRScreen Button { min-width: 10; margin-left: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)
        self.key = uuid.uuid4().hex
        self.pending = None
        self.busy = False
        self.last_status = None
        previous = self.record.get('close_request')
        if previous and previous['status'] in ('running', 'uncertain'):
            self.key = previous['key']
            self.pending = {'key': self.key, 'url': self.record['url'], 'comment': previous['comment'], 'confirmed': True}
            self.last_status = previous['status']

    def compose(self):
        with Vertical():
            yield Static('Close PR on GitHub', id='close-title', markup=False)
            with VerticalScroll(id='close-fields'):
                yield Static(literal(self.record.get('github', {}).get('title') or self.record['url']), markup=False)
                yield Static(self.record['url'], markup=False)
                yield Static('Confirmation posts your comment and closes this PR on GitHub. Branches and the linked Task stay unchanged.', markup=False)
                yield Label('Close comment (optional; posted to the PR on GitHub)')
                yield TextArea(id='close-comment')
            yield Static('Ctrl+Enter confirms. Esc cancels without sending a request.', id='close-message', markup=False)
            with Horizontal():
                yield Button('Open PR', id='close-open')
                yield Button('Cancel', id='close-cancel')
                yield Button('Close PR', id='close-confirm', variant='error')

    def on_mount(self):
        field = self.query_one('#close-comment', TextArea)
        if self.pending:
            field.load_text(self.pending['comment'])
            field.disabled = True
            self.query_one('#close-message', Static).update(literal(self.record['close_request']['message']))
            self.update_buttons()
        else:
            field.focus()
        self.set_interval(1, self.poll_result)

    def action_cancel(self):
        if not self.busy:
            self.dismiss(None)

    def action_confirm(self):
        if self.busy or self.last_status == 'failed':
            return
        if self.pending is None:
            comment = self.query_one('#close-comment', TextArea).text
            if len(comment) > 65536 or chr(0) in comment:
                self.query_one('#close-message', Static).update('Comment limit: 65,536 characters; null bytes are not allowed.')
                return
            self.pending = {'key': self.key, 'url': self.record['url'], 'comment': comment, 'confirmed': True}
            self.query_one('#close-comment', TextArea).disabled = True
        self.busy = True
        self.update_buttons()
        self.send(check=self.last_status in ('running', 'uncertain'))

    def poll_result(self):
        if self.last_status == 'running' and not self.busy:
            self.busy = True
            self.update_buttons()
            self.send(check=True)

    def update_buttons(self):
        self.query_one('#close-cancel', Button).disabled = self.busy
        button = self.query_one('#close-confirm', Button)
        button.disabled = self.busy or self.last_status == 'failed'
        button.label = 'Working...' if self.busy else ('Check outcome' if self.pending else 'Close PR')

    @work(group='close-pr-request', exit_on_error=False)
    async def send(self, check=False):
        try:
            if check:
                result = await self.app.call_api('close_result', {'key': self.key, 'check_github': self.last_status == 'uncertain'})
            else:
                result = await self.app.call_api('close_pr', deepcopy(self.pending))
            self.last_status = result['status']
            if self.last_status in ('succeeded', 'already_closed'):
                self.dismiss(result)
                return
            self.query_one('#close-message', Static).update(literal(result['message']))
        except Exception:
            self.query_one('#close-message', Static).update(
                'Response unavailable. Outcome is unknown. Check outcome reuses this request key and will not repeat a recorded write.')
        finally:
            self.busy = False
            if self.is_mounted:
                self.update_buttons()

    @on(Button.Pressed)
    def button_pressed(self, event):
        event.stop()
        if event.button.id == 'close-confirm':
            self.action_confirm()
        elif event.button.id == 'close-open':
            self.app.open_reference(self.record['url'], 'pr')
        elif event.button.id == 'close-cancel':
            self.action_cancel()
