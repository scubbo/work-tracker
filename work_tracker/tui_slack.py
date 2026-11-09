"""Explicit opening of saved Slack thread references. No Slack reads or writes."""

from copy import deepcopy

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static


def display(value):
    return ''.join(char if char.isprintable() else f'\\x{ord(char):02x}' for char in str(value))


def slack_threads(record):
    """Preserve existing reference order without choosing an arbitrary primary."""
    result, seen = [], set()
    for thread in (record or {}).get('slack-threads') or []:
        if not isinstance(thread, dict) or not isinstance(thread.get('url'), str) or not thread['url']:
            continue
        if thread['url'] not in seen:
            result.append(deepcopy(thread))
            seen.add(thread['url'])
    return result


class SlackThreadScreen(ModalScreen):
    BINDINGS = [Binding('escape','close','Back'), Binding('ctrl+enter','open','Open Slack thread',priority=True)]
    CSS = """
    SlackThreadScreen { align: center middle; background: $background 75%; }
    SlackThreadScreen > Vertical { width: 86; max-width: 96%; height: auto;
        max-height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    SlackThreadScreen Static { height: auto; margin-bottom: 1; }
    SlackThreadScreen #slack-thread-actions { height: 3; align-horizontal: right; }
    SlackThreadScreen Button { width: auto; margin-left: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)
        self.threads = slack_threads(self.record)

    def compose(self):
        with Vertical():
            yield Static('Slack thread',markup=False)
            yield Static(display(self.record['url']),id='slack-thread-pr',markup=False)
            yield Static('Several Slack threads are attached. Choose the review, discussion, or ping thread to open.',markup=False)
            yield Select([(Text(display(f"{thread.get('description') or 'Slack thread'} | {thread['url']}")),thread['url'])
                          for thread in self.threads],prompt='Choose a Slack thread',id='slack-thread-choice')
            with Horizontal(id='slack-thread-actions'):
                yield Button('Back',id='slack-thread-back')
                yield Button('Open Slack thread',id='slack-thread-open',variant='primary')

    def action_close(self):
        self.dismiss()

    def action_open(self):
        url=self.query_one('#slack-thread-choice',Select).value
        if not any(thread['url']==url for thread in self.threads):
            self.app.notify('Choose a Slack thread first.',severity='warning',markup=False)
            return
        if self.app.open_reference(url,'slack'):
            self.dismiss()

    @on(Button.Pressed)
    def pressed(self,event):
        event.stop()
        if event.button.id=='slack-thread-back': self.action_close()
        elif event.button.id=='slack-thread-open': self.action_open()
