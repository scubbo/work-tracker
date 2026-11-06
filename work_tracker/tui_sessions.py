"""Session controls. The application verifies identities and owns pane activation."""

from copy import deepcopy

from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .session_actions import references


def session_text(value):
    return ''.join(char if char.isprintable() else f'\\x{ord(char):02x}' for char in str(value))


class SessionsScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding('escape', 'close', 'Back')]
    CSS = """
    SessionsScreen { align: center middle; background: $background 75%; }
    SessionsScreen > #sessions-dialog { width: 86; max-width: 96%; height: 90%;
        border: round $accent; padding: 1 2; background: $surface; }
    SessionsScreen #sessions-heading { height: 1; text-style: bold; }
    SessionsScreen #sessions-pr { height: auto; max-height: 3; }
    SessionsScreen #sessions-load { height: auto; max-height: 3; }
    SessionsScreen #sessions-scroll { height: 1fr; }
    SessionsScreen #sessions-buttons { height: 3; }
    SessionsScreen #sessions-buttons Button { width: auto; min-width: 10; margin-right: 1; }
    """

    def __init__(self, record):
        super().__init__()
        self.record = deepcopy(record)

    def compose(self):
        with Vertical(id='sessions-dialog'):
            yield Static('Agent sessions', id='sessions-heading', markup=False)
            yield Static(self.record['url'], id='sessions-pr', markup=False)
            yield Static('Loading current local references...', id='sessions-load', markup=False)
            with VerticalScroll(id='sessions-scroll'):
                yield SessionLinks(self.record, id='session-links')
            with Horizontal(id='sessions-buttons'):
                yield Button('Add session reference', id='sessions-add', disabled=not self.app.connected)
                yield Button('Back', id='sessions-back')

    def on_mount(self):
        self.query_one('#sessions-back', Button).focus()
        self.load_record()

    @work(exit_on_error=False)
    async def load_record(self):
        try:
            value = await self.app.call_api('get_pr', {'url': self.record['url']})
            if not value:
                raise ValueError('PR is no longer registered')
            await self.update_record(value)
            if self.is_mounted:
                self.query_one('#sessions-load', Static).update('Viewing saved references. Copying a local resume command checks the session first.')
        except Exception:
            if self.is_mounted:
                self.query_one('#sessions-load', Static).update('Current references are unavailable. Showing the last loaded session information.')

    async def update_record(self, record):
        if not self.is_mounted or self not in self.app.screen_stack:
            return
        self.record = deepcopy(record)
        await self.query_one('#session-links', SessionLinks).update_record(self.record)
        self.query_one('#sessions-add', Button).disabled = not self.app.connected

    def action_close(self):
        self.dismiss(None)

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'sessions-add':
            if self.app.connected:
                self.dismiss({'mode': 'session', 'record': self.record})
        elif event.button.id == 'sessions-back':
            self.action_close()


class SessionLinks(Vertical):
    DEFAULT_CSS = """
    SessionLinks { height: auto; margin-top: 1; }
    SessionLinks Static { height: auto; margin-bottom: 1; }
    SessionLinks Button { width: auto; max-width: 100%; margin-bottom: 1; }
    """

    def __init__(self, record, **kwargs):
        super().__init__(**kwargs)
        self.record = deepcopy(record)
        self.busy = False

    def contents(self):
        yield Static('Session references and resume actions', markup=False)
        actions = list(self.record.get('agent-session-actions') or [])
        for reference in references(self.record):
            if not any(action.get('reference') == reference for action in actions):
                actions.append({'reference': reference, 'status': 'unavailable',
                                'reason': 'The application did not supply a usable resume action.'})
        if not actions:
            yield Static('No agent sessions are attached. Use Add session reference to attach a known session.', markup=False, id='sessions-empty')
        for index, action in enumerate(actions):
            reference = action.get('reference') or {}
            info = '\n'.join(f'{label}: {session_text(reference.get(key) or "Unknown")}' for key, label in (
                ('tool', 'Tool'), ('path', 'Workspace'), ('sessionId', 'Session ID'), ('machine', 'Machine')))
            info += f"\n{session_text(action.get('status'))}: {session_text(action.get('reason'))}"
            yield Static(info, markup=False, id=f'session-status-{index}')
            if action.get('command'):
                yield Static(action['command'], markup=False)
            if action.get('can_activate_pane'):
                yield Button('Activate open herdr pane', id=f'activate-session-{index}', disabled=self.busy)
            remote = action.get('status') == 'other_machine'
            if action.get('can_verify') or remote:
                yield Button('Copy command for recorded machine' if remote else 'Check and copy resume command',
                             id=f'resume-session-{index}', disabled=self.busy)

    def compose(self):
        yield from self.contents()

    async def update_record(self, record, force=False):
        unchanged = self.record.get('agent-session-actions') == record.get('agent-session-actions')
        self.record = deepcopy(record)
        if not self.is_mounted or self.busy or unchanged and not force:
            return
        await self.remove_children()
        if self.is_mounted:
            await self.mount(*self.contents())

    @on(Button.Pressed)
    def pressed(self, event):
        key = event.button.id or ''
        if not key.startswith(('resume-session-', 'activate-session-')):
            return
        event.stop()
        actions = self.record.get('agent-session-actions') or []
        index = int(key.rsplit('-', 1)[1])
        if self.busy or index >= len(actions):
            return
        self.busy = True
        for button in self.query(Button):
            button.disabled = True
        if key.startswith('activate-session-'):
            self.activate_pane(deepcopy(actions[index]))
        else:
            self.check_and_copy(deepcopy(actions[index]))

    @work(exclusive=True, exit_on_error=False)
    async def activate_pane(self, action):
        try:
            value = await self.app.call_api('activate_session_pane', {
                'url': self.record['url'], 'reference': action['reference']})
            if self.is_mounted and self.screen in self.app.screen_stack:
                self.app.notify(value['reason'], severity='information' if value['status'] == 'activated' else 'warning', markup=False)
        except Exception:
            if self.is_mounted:
                self.app.notify('Could not activate the pane. Use the copy-resume command instead.', severity='error', markup=False)
        finally:
            self.busy = False
            if self.is_mounted and self.screen in self.app.screen_stack:
                await self.update_record(self.record, force=True)

    @work(exclusive=True, exit_on_error=False)
    async def check_and_copy(self, action):
        try:
            if action['status'] == 'other_machine':
                await self.app.copy_text(action['command'], 'Copied instructions for the recorded machine. Do not run them locally.')
                return
            value = await self.app.call_api('get_pr', {'url': self.record['url'], 'verify_sessions': True})
            if not self.is_mounted or self.screen not in self.app.screen_stack:
                return
            target = next((candidate for candidate in (value or {}).get('agent-session-actions', [])
                           if candidate.get('reference') == action.get('reference')), None)
            if value:
                self.record = deepcopy(value)
            if not target or target.get('status') != 'ready' or not target.get('usable_locally'):
                self.app.notify((target or {}).get('reason', 'No usable session link. The reference is no longer attached.'),
                                severity='warning', markup=False)
                return
            await self.app.copy_text(target['command'], 'Copied the verified resume command. Paste it into a terminal to resume.')
        except Exception:
            if self.is_mounted:
                self.app.notify('Could not check or copy the resume command. The saved reference is unchanged.', severity='error', markup=False)
        finally:
            self.busy = False
            if self.is_mounted and self.screen in self.app.screen_stack:
                await self.update_record(self.record, force=True)
