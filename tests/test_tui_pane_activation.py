from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import Button

from test_herdr_sessions import Native, pane
from test_tui import record
from test_tui_view_controls import SessionAPI, settle
from work_tracker.herdr_sessions import HerdrSessions
from work_tracker.service import Application
from work_tracker.tui import WorkTrackerApp


class PaneAPI(SessionAPI):
    def __init__(self, root):
        super().__init__()
        self.reference = deepcopy(self.rows[0]['agent-sessions'][0])
        self.native = Native([pane(self.reference)])
        self.backend = Application(root)
        self.backend.herdr_sessions = HerdrSessions(machine='test.local', which=lambda name: '/bin/'+name, run=self.native)
        for key, kind, payload in (
            ('register', 'register_pr', {'url': self.rows[0]['url']}),
            ('attach', 'attach_session', {'url': self.rows[0]['url'], 'session': self.reference})):
            self.backend.dispatch('intent', {'key': key, 'kind': kind, 'payload': payload})

    def __call__(self, root, method, params=None):
        if method == 'activate_session_pane':
            self.calls.append((method, deepcopy(params)))
            return self.backend.dispatch(method, params)
        return super().__call__(root, method, params)


class PaneActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = PaneAPI(self.temp.name)
        self.copied = []
        self.app = WorkTrackerApp(Path('/tmp/not-a-datastore'), rpc=self.api, poll_interval=0, start_view='prs',
                                  clipboard_writer=self.copied.append)

    async def click(self, pilot, selector):
        self.app.screen.query_one(selector, Button).scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click(selector)
        await settle(self.app, pilot)

    async def test_s_and_click_activate_route_through_the_application_without_copying_or_editing(self):
        before = deepcopy(self.api.backend.storage.export_registry())
        async with self.app.run_test(size=(80, 24)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('s')
            await settle(self.app, pilot)
            self.assertEqual(self.api.native.calls, [])
            with mock.patch.object(self.app, 'notify') as notify:
                await self.click(pilot, '#activate-session-0')
                self.assertIn('Activated herdr pane', notify.call_args.args[0])
            self.assertEqual(self.copied, [])
            self.assertIn(('activate_session_pane', {'url': self.api.rows[0]['url'], 'reference': self.api.reference}), self.api.calls)
            self.assertFalse(self.app.screen.query_one('#activate-session-0', Button).disabled)
            self.assertEqual(self.api.backend.storage.export_registry(), before)

    async def test_no_match_and_tool_failure_keep_the_working_copy_fallback(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('s')
            await settle(self.app, pilot)
            for fail in (False, True):
                self.api.native.panes['default'] = []
                self.api.native.fail = fail
                with mock.patch.object(self.app, 'notify') as notify:
                    await self.click(pilot, '#activate-session-0')
                    self.assertEqual(notify.call_args.kwargs['severity'], 'warning')
                await self.click(pilot, '#resume-session-0')
                self.assertEqual(self.copied[-1], 'cd -- /tmp/test-workspace && fx --resume Saved123')
                self.assertFalse(any('focus' in call for call in self.api.native.calls))

    async def test_missing_lsof_displays_the_dependency_error_and_keeps_copy_available(self):
        self.api.native.panes['default'][0].pop('agent_session')
        self.api.backend.herdr_sessions.which = lambda name: None if name == 'lsof' else '/bin/herdr'
        async with self.app.run_test(size=(100, 30)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('s')
            await settle(self.app, pilot)
            with mock.patch.object(self.app, 'notify') as notify:
                await self.click(pilot, '#activate-session-0')
                self.assertIn('cannot find lsof', notify.call_args.args[0])
                self.assertEqual(notify.call_args.kwargs['severity'], 'warning')
            await self.click(pilot, '#resume-session-0')
            self.assertEqual(self.copied, ['cd -- /tmp/test-workspace && fx --resume Saved123'])
            self.assertFalse(any('focus' in call for call in self.api.native.calls))

    async def test_foreign_machine_has_no_local_activation_button(self):
        self.api.rows[0]['agent-sessions'][0]['machine'] = 'remote.local'
        async with self.app.run_test(size=(80, 24)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('s')
            await settle(self.app, pilot)
            self.assertFalse(self.app.screen.query('#activate-session-0'))
            self.assertIn('recorded machine', str(self.app.screen.query_one('#resume-session-0', Button).label))
            self.assertEqual(self.api.native.calls, [])

    async def test_original_pr_and_reference_are_retained_when_main_selection_changes(self):
        self.api.rows.append(record(43))
        original = self.api.rows[0]['url']
        async with self.app.run_test(size=(100, 35)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('s')
            await settle(self.app, pilot)
            self.app.selected_url = self.api.rows[1]['url']
            self.app.request_load()
            await settle(self.app, pilot)
            await self.click(pilot, '#activate-session-0')
            call = next(params for method, params in self.api.calls if method == 'activate_session_pane')
            self.assertEqual(call, {'url': original, 'reference': self.api.reference})


if __name__ == '__main__':
    unittest.main()
