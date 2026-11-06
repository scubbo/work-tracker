from pathlib import Path
import unittest

from textual.widgets import Button, Input, Static

from test_tui import FakeAPI, record
from test_tui_view_controls import SessionAPI, settle
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_forms import IntentForm
from work_tracker.tui_sessions import SessionsScreen


def text(screen, selector):
    return str(screen.query_one(selector, Static).render())


class SessionScreenTests(unittest.IsolatedAsyncioTestCase):
    def app(self, api, copied=None):
        return WorkTrackerApp(Path('/tmp/not-a-datastore'), rpc=api, poll_interval=0,
                              clipboard_writer=(copied if copied is not None else []).append)

    async def test_s_shows_saved_identity_instead_of_a_blank_attach_form(self):
        api = SessionAPI()
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pilot.press('s')
            await settle(app, pilot)
            self.assertIsInstance(app.screen, SessionsScreen)
            info = text(app.screen, '#session-status-0')
            for field in ('Tool: fx', 'Workspace: /tmp/test-workspace', 'Session ID: Saved123', 'Machine: test.local'):
                self.assertIn(field, info)
            self.assertEqual(len(app.screen.query(Input)), 0)
            self.assertFalse(app.screen.query_one('#sessions-add', Button).disabled)
            api.lookup.assert_not_called()
            self.assertFalse(any(method not in ('status', 'list_prs', 'get_pr', 'get_pr_sort') for method, params in api.calls))
            await pilot.press('s')
            self.assertEqual(len(app.screen_stack), 2)

    async def test_s_then_copy_uses_the_verified_leading_hyphen_session(self):
        api, copied = SessionAPI(), []
        api.rows[0]['agent-sessions'][0]['sessionId'] = '-Saved123'
        api.lookup.return_value = {'id': '-Saved123'}
        app = self.app(api, copied)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.press('s')
            await settle(app, pilot)
            self.assertIn('Session ID: -Saved123', text(app.screen, '#session-status-0'))
            api.lookup.assert_not_called()
            button = app.screen.query_one('#resume-session-0', Button)
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click('#resume-session-0')
            await settle(app, pilot)
            self.assertEqual(copied, ['cd -- /tmp/test-workspace && fx session resume --id -Saved123'])
            self.assertEqual(len(api.rows[0]['agent-sessions']), 1)
            self.assertFalse(any(method == 'intent' for method, params in api.calls))

    async def test_empty_view_requires_explicit_add_before_showing_an_attach_form(self):
        api = FakeAPI([record()])
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pilot.press('s')
            await settle(app, pilot)
            self.assertIsInstance(app.screen, SessionsScreen)
            self.assertIn('No agent sessions are attached', text(app.screen, '#sessions-empty'))
            await pilot.click('#sessions-add')
            await pilot.pause()
            self.assertIsInstance(app.screen, IntentForm)
            self.assertEqual(app.screen.mode, 'session')
            self.assertEqual(app.screen.query_one('#form-tool', Input).value, '')
            await pilot.press('escape')
            self.assertFalse(any(method == 'intent' for method, params in api.calls))

    async def test_reference_only_records_still_show_identity_without_fabricating_an_action(self):
        row = record()
        row['agent-sessions'] = [{'tool': 'custom-agent', 'path': '/tmp/other-workspace',
                                  'sessionId': 'KnownRef', 'machine': 'remote.local'}]
        api = FakeAPI([row])
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pilot.press('s')
            await settle(app, pilot)
            info = text(app.screen, '#session-status-0')
            for field in ('custom-agent', '/tmp/other-workspace', 'KnownRef', 'remote.local', 'unavailable'):
                self.assertIn(field, info)
            self.assertEqual(len(app.screen.query('#resume-session-0')), 0)
            self.assertFalse(any(method == 'intent' for method, params in api.calls))

    async def test_poll_updates_refs_but_retains_original_pr_even_if_it_closes(self):
        api = SessionAPI()
        original = api.rows[0]['url']
        api.rows.append(record(43))
        app = self.app(api)
        async with app.run_test(size=(100, 35)) as pilot:
            await settle(app, pilot)
            await pilot.press('s')
            await settle(app, pilot)
            api.rows[0]['agent-sessions'].append({'tool': 'other-tool', 'path': '/tmp/new-reference',
                                                  'sessionId': 'Another123', 'machine': 'remote.local'})
            api.rows[0]['github']['state'] = 'closed'
            app.selected_url = api.rows[1]['url']
            app.request_load()
            await settle(app, pilot)
            self.assertIsInstance(app.screen, SessionsScreen)
            self.assertEqual(app.screen.record['url'], original)
            self.assertIn('Another123', text(app.screen, '#session-status-1'))
            self.assertIn('unsupported', text(app.screen, '#session-status-1'))
            await pilot.click('#sessions-add')
            await pilot.pause()
            self.assertIsInstance(app.screen, IntentForm)
            self.assertEqual(app.screen.record['url'], original)

    async def test_offline_view_retains_saved_identity_and_disables_add(self):
        class OfflineAPI(SessionAPI):
            def __call__(self, root, method, params=None):
                if self.fail_read and method == 'get_pr':
                    raise RuntimeError('Offline')
                return super().__call__(root, method, params)
        api = OfflineAPI()
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            api.fail_read = True
            app.request_load()
            await settle(app, pilot)
            self.assertFalse(app.connected)
            await pilot.press('s')
            await settle(app, pilot)
            self.assertIsInstance(app.screen, SessionsScreen)
            self.assertIn('Saved123', text(app.screen, '#session-status-0'))
            self.assertIn('last loaded', text(app.screen, '#sessions-load'))
            self.assertTrue(app.screen.query_one('#sessions-add', Button).disabled)

    async def test_typing_s_in_search_does_not_open_sessions(self):
        app = self.app(SessionAPI())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pilot.press('/', 's')
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.query_one('#search', Input).value, 's')


if __name__ == '__main__':
    unittest.main()
