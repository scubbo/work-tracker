from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from textual.widgets import Button, Input, Select, Static

from test_pr_merging import HEAD, PR, MergeGitHub
from test_tui import FakeAPI
from test_tui_view_controls import settle
from work_tracker.service import Application
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_merge import MergePRScreen


class MergingAPI(FakeAPI):
    def __init__(self, root):
        super().__init__()
        self.github = MergeGitHub()
        self.backend = Application(root, run=self.github)
        self.backend.dispatch('intent', {'key': 'fixture', 'kind': 'register_pr', 'payload': {'url': PR}})
        self.lose_receipt = False

    def __call__(self, root, method, params=None):
        if method not in ('merge_preview', 'merge_pr', 'merge_result'):
            return super().__call__(root, method, params)
        self.calls.append((method, deepcopy(params)))
        result = self.backend.dispatch(method, params)
        current = self.backend.storage.list_prs(state=None)[0]
        row = next(row for row in self.rows if row['url'] == PR)
        if method != 'merge_preview':
            row['github'].update(current['github'])
            row['merge_request'] = current['merge_request']
        if method == 'merge_pr' and self.lose_receipt:
            self.lose_receipt = False
            raise RuntimeError('Response lost after merge')
        return result


class MergeTUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = MergingAPI(self.temp.name)
        self.app = WorkTrackerApp(Path('/tmp/not-a-merge-datastore'), rpc=self.api, poll_interval=0, start_view='prs')

    async def open(self, pilot, choose=True):
        await settle(self.app, pilot)
        await pilot.press('m')
        await settle(self.app, pilot)
        self.assertIsInstance(self.app.screen, MergePRScreen)
        if choose:
            self.app.screen.query_one('#merge-method', Select).value = 'squash'
            await pilot.pause()

    def puts(self):
        return [args for args in self.api.github.calls if args[3] == 'PUT']

    async def test_m_opens_confirmation_and_escape_sends_no_merge(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.open(pilot, choose=False)
            self.assertIn(HEAD, str(self.app.screen.query_one('#merge-head', Static).render()))
            self.assertTrue(any('GitHub can still delete the head branch' in str(widget.render()) for widget in self.app.screen.query(Static)))
            self.assertNotIn(self.app.screen.query_one('#merge-method', Select).value, ('merge', 'squash', 'rebase'))
            self.assertTrue(self.app.screen.query_one('#merge-confirm', Button).disabled)
            self.assertLessEqual(self.app.screen.query_one('#merge-confirm', Button).region.bottom, 24)
            await pilot.press('m')
            self.assertEqual(len(self.app.screen_stack), 2)
            await pilot.press('escape')
            self.assertEqual(self.puts(), [])

    async def test_confirm_merges_original_pr_and_previewed_head_after_selection_changes(self):
        async with self.app.run_test(size=(120, 36)) as pilot:
            await self.open(pilot)
            self.app.selected_url = self.api.rows[1]['url']
            self.app.request_load()
            await settle(self.app, pilot)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertNotIsInstance(self.app.screen, MergePRScreen)
            params = next(params for method, params in self.api.calls if method == 'merge_pr')
            self.assertEqual((params['url'], params['head_sha'], params['method']), (PR, HEAD, 'squash'))
            self.assertIs(params['confirmed'], True)
            self.assertEqual(len(self.puts()), 1)
            self.assertNotIn(PR, [row['url'] for row in self.app.visible_rows])

    async def test_single_allowed_method_is_selected_without_choosing_an_arbitrary_default(self):
        self.api.github.repo.update(allow_merge_commit=False, allow_rebase_merge=False)
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open(pilot, choose=False)
            self.assertEqual(self.app.screen.query_one('#merge-method', Select).value, 'squash')
            self.assertFalse(self.app.screen.query_one('#merge-confirm', Button).disabled)
            self.assertEqual(self.puts(), [])

    async def test_changed_head_fails_without_merging_or_changing_the_confirmation(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open(pilot)
            self.api.github.remote['head']['sha'] = 'b'*40
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertIsInstance(self.app.screen, MergePRScreen)
            self.assertIn('head commit changed', str(self.app.screen.query_one('#merge-message', Static).render()))
            self.assertTrue(self.app.screen.query_one('#merge-confirm', Button).disabled)
            self.assertEqual(self.puts(), [])

    async def test_lost_response_reuses_the_identical_request_and_never_merges_twice(self):
        self.api.lose_receipt = True
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open(pilot)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertIsInstance(self.app.screen, MergePRScreen)
            self.assertTrue(self.app.screen.query_one('#merge-method', Select).disabled)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertNotIsInstance(self.app.screen, MergePRScreen)
            calls = [params for method, params in self.api.calls if method == 'merge_pr']
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(len(self.puts()), 1)

    async def test_uncertain_merge_reopens_with_same_key_and_checks_read_only(self):
        self.api.github.lose_response = True
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open(pilot)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            key = self.app.screen.key
            self.assertEqual(self.app.screen.last_status, 'uncertain')
            await pilot.press('escape')
            self.app.action_table()
            await self.open(pilot, choose=False)
            self.assertEqual(self.app.screen.key, key)
            self.assertTrue(self.app.screen.query_one('#merge-method', Select).disabled)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertNotIsInstance(self.app.screen, MergePRScreen)
            self.assertEqual(len(self.puts()), 1)
            self.assertIn(('merge_result', {'key': key, 'check_github': True}), self.api.calls)

    async def test_typing_m_in_search_or_offline_never_opens_merge(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            await settle(self.app, pilot)
            await pilot.press('/', 'm')
            self.assertEqual(self.app.query_one('#search', Input).value, 'm')
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertFalse(any(method == 'merge_preview' for method, _ in self.api.calls))
            self.app.action_clear_filters()
            self.api.fail_read = True
            self.app.request_load()
            await settle(self.app, pilot)
            self.app.action_table()
            await pilot.press('m')
            self.assertEqual(len(self.app.screen_stack), 1)
            self.assertEqual(self.puts(), [])

    async def test_blocked_preview_disables_confirmation(self):
        self.api.github.remote['mergeable_state'] = 'blocked'
        async with self.app.run_test(size=(100, 30)) as pilot:
            await self.open(pilot, choose=False)
            self.assertTrue(self.app.screen.query_one('#merge-confirm', Button).disabled)
            await pilot.press('ctrl+enter')
            await settle(self.app, pilot)
            self.assertEqual(self.puts(), [])


if __name__ == '__main__':
    unittest.main()
