from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from textual.widgets import Button, DataTable, Input, Select

from test_tui import FakeAPI, record
from work_tracker.session_actions import SessionActions
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_sort import SortScreen, ordered_rows


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


class OrderingTests(unittest.TestCase):
    def test_default_preserves_url_order_and_title_is_case_insensitive_with_stable_ties(self):
        rows = [record(3, 'beta'), record(2, 'Alpha'), record(10, 'alpha')]
        self.assertEqual([r['url'].rsplit('/', 1)[1] for r in ordered_rows(rows, 'pr')], ['10', '2', '3'])
        self.assertEqual([r['url'].rsplit('/', 1)[1] for r in ordered_rows(rows, 'title')], ['10', '2', '3'])
        self.assertEqual([r['url'].rsplit('/', 1)[1] for r in ordered_rows(rows, 'title', True)], ['3', '10', '2'])

    def test_dates_sort_as_timestamps_and_unknown_values_stay_last_both_directions(self):
        rows = [record(1), record(2), record(3)]
        rows[0]['github']['createdAt'] = '2026-11-03T23:30:00-02:00'
        rows[1]['github']['createdAt'] = '2026-11-04T00:00:00Z'
        for descending, expected in ((False, [2, 1, 3]), (True, [1, 2, 3])):
            self.assertEqual([int(r['url'].rsplit('/', 1)[1]) for r in ordered_rows(rows, 'opened', descending)], expected)

    def test_elapsed_time_uses_anchors_not_formatted_labels_and_ignores_stale_observations(self):
        rows = [record(1), record(2), record(3)]
        rows[0]['state_changed_at'] = '2026-11-03T23:00:00Z'
        rows[1]['state_first_seen_at'] = '2026-11-03T00:00:00Z'
        rows[2]['state_changed_at'] = '2026-11-01T00:00:00Z'
        rows[2]['refresh_error'] = 'Stale'
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.assertEqual(ordered_rows(rows, 'state-age', True, now=now), [rows[1], rows[0], rows[2]])


class SessionAPI(FakeAPI):
    def __init__(self, tool='fx', machine='test.local'):
        row = record()
        row['agent-sessions'] = [{'tool': tool, 'path': '/tmp/test-workspace', 'sessionId': 'Saved123', 'machine': machine}]
        super().__init__([row])
        self.lookup = mock.Mock(return_value={'id': 'Saved123'})
        self.actions = SessionActions(machine='test.local', which=lambda tool: '/bin/'+tool,
                                      is_directory=lambda path: True, inspect=self.lookup)

    def __call__(self, root, method, params=None):
        value = super().__call__(root, method, params)
        if method == 'list_prs':
            value['items'] = [self.actions.project(row) for row in value['items']]
        elif method == 'get_pr' and value:
            value = self.actions.project(value, (params or {}).get('verify_sessions', False))
        return value


class ViewControlTests(unittest.IsolatedAsyncioTestCase):
    def app(self, api, copied=None):
        return WorkTrackerApp(Path('/tmp/not-a-datastore'), rpc=api, poll_interval=0, start_view='prs',
                              clipboard_writer=(copied if copied is not None else []).append)

    async def test_header_clicks_toggle_order_and_preserve_selected_pr_across_reload(self):
        api = FakeAPI([record(1, 'Zulu'), record(2, 'Alpha')])
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(app, pilot)
            selected = app.selected_url
            table = app.query_one('#prs', DataTable)
            column = table.columns[app.title_column]
            x = sum(c.width + 2 * table.cell_padding for c in table.ordered_columns[:2]) + 1
            await pilot.click('#prs', offset=(x+1, 1))
            await pilot.pause()
            self.assertEqual(app.sort_field, 'title')
            self.assertEqual(app.visible_rows[0]['github']['title'], 'Alpha')
            self.assertEqual(app.selected_url, selected)
            self.assertIn('↑', column.label.plain)
            await pilot.click('#prs', offset=(x+1, 1))
            await pilot.pause()
            self.assertTrue(app.sort_descending)
            self.assertEqual(app.visible_rows[0]['github']['title'], 'Zulu')
            api.rows.reverse()
            app.request_load()
            await settle(app, pilot)
            self.assertEqual(app.visible_rows[0]['github']['title'], 'Zulu')
            self.assertEqual(app.selected_url, selected)
            self.assertFalse(any(method not in ('list_prs', 'status', 'get_pr_sort', 'set_pr_sort') for method, params in api.calls))

    async def test_f2_chooser_applies_sort_and_clear_filters_does_not_reset_it(self):
        api = FakeAPI([record(1), record(2)])
        api.rows[0]['github']['createdAt'] = '2026-11-01T00:00:00Z'
        api.rows[1]['github']['createdAt'] = '2026-11-03T00:00:00Z'
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app, pilot)
            await pilot.press('f2')
            await pilot.pause()
            self.assertIsInstance(app.screen, SortScreen)
            app.screen.query_one('#sort-field', Select).value = 'opened'
            app.screen.query_one('#sort-direction', Select).value = True
            await pilot.click('#sort-apply')
            await pilot.pause()
            self.assertEqual(app.visible_rows[0]['url'], api.rows[1]['url'])
            app.action_clear_filters()
            await pilot.pause()
            self.assertEqual(app.sort_field, 'opened')
            self.assertTrue(app.sort_descending)
            table = app.query_one('#prs', DataTable)
            self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width)

    async def test_p_copies_selected_pr_offline_and_typing_p_in_search_does_not_copy(self):
        api, copied = FakeAPI(), []
        app = self.app(api, copied)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press('down', 'p')
            await pilot.pause()
            self.assertEqual(copied, [api.rows[1]['url']])
            api.fail_read = True
            app.request_load()
            await settle(app, pilot)
            await pilot.press('p')
            await pilot.pause()
            self.assertEqual(copied, [api.rows[1]['url']] * 2)
            await pilot.press('/', 'p')
            await pilot.pause()
            self.assertEqual(app.query_one('#search', Input).value, 'p')
            self.assertEqual(len(copied), 2)

    async def test_empty_view_and_metadata_form_do_not_copy_pr(self):
        copied = []
        app = self.app(FakeAPI([]), copied)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press('p')
            self.assertEqual(copied, [])
        app = self.app(FakeAPI(), copied)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press('e')
            await settle(app, pilot)
            app.screen.query_one('#form-description').focus()
            await pilot.pause()
            await pilot.press('p')
            await pilot.pause()
            self.assertEqual(copied, [])
            self.assertIn('p', app.screen.query_one('#form-description').text)

    async def test_actual_details_button_checks_and_copies_verified_fx_command(self):
        api, copied = SessionAPI(), []
        app = self.app(api, copied)
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(app, pilot)
            await pilot.press('enter')
            await pilot.pause()
            button = app.screen.query_one('#resume-session-0', Button)
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click('#resume-session-0')
            await settle(app, pilot)
            self.assertEqual(copied, ['cd -- /tmp/test-workspace && fx --resume Saved123'])
            self.assertIn(('get_pr', {'url': api.rows[0]['url'], 'verify_sessions': True}), api.calls)
            api.lookup.assert_called_once()
            self.assertEqual(len(api.rows[0]['agent-sessions']), 1)
            self.assertEqual(app.screen.query_one('#session-links').record['agent-session-actions'][0]['status'], 'ready')

    async def test_details_button_preserves_a_leading_hyphen_in_fx_session_id(self):
        api, copied = SessionAPI(), []
        api.rows[0]['agent-sessions'][0]['sessionId'] = '-Saved123'
        api.lookup.return_value = {'id': '-Saved123'}
        app = self.app(api, copied)
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(app, pilot)
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#resume-session-0', Button).scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click('#resume-session-0')
            await settle(app, pilot)
            self.assertEqual(copied, ['cd -- /tmp/test-workspace && fx session resume --id -Saved123'])
            api.lookup.assert_called_once_with(['/bin/fx', 'session', '--id', '-Saved123', '--json'], '/tmp/test-workspace')

    async def test_remote_button_copies_labeled_instructions_without_local_probe(self):
        api, copied = SessionAPI(machine='remote.local'), []
        app = self.app(api, copied)
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(app, pilot)
            await pilot.press('enter')
            await pilot.pause()
            button = app.screen.query_one('#resume-session-0', Button)
            self.assertIn('recorded machine', str(button.label))
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click('#resume-session-0')
            await settle(app, pilot)
            self.assertEqual(len(copied), 1)
            api.lookup.assert_not_called()
            self.assertFalse(any(method == 'get_pr' for method, params in api.calls))

    async def test_failed_verification_does_not_copy_or_claim_usable_session(self):
        api, copied = SessionAPI(), []
        api.lookup.return_value = {'id': 'WrongSession'}
        app = self.app(api, copied)
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(app, pilot)
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#resume-session-0', Button).scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click('#resume-session-0')
            await settle(app, pilot)
            self.assertEqual(copied, [])
            self.assertEqual(app.screen.query_one('#session-links').record['agent-session-actions'][0]['status'], 'unavailable')


class ClipboardTests(unittest.TestCase):
    def test_macos_clipboard_uses_stdin_not_shell_interpolation(self):
        from work_tracker.clipboard import copy_macos
        with mock.patch('work_tracker.clipboard.subprocess.run') as run:
            copy_macos('https://github.com/acme/service/pull/42')
            run.assert_called_once_with(['/usr/bin/pbcopy'], input=b'https://github.com/acme/service/pull/42', check=True, timeout=3)


if __name__ == '__main__':
    unittest.main()
