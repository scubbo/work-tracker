import asyncio
from copy import deepcopy
from pathlib import Path
import threading
import unittest

from textual.widgets import DataTable, Select

from test_tui import FakeAPI
from test_tui_view_controls import settle
from work_tracker.tui import WorkTrackerApp


class SortPreferenceTUITests(unittest.IsolatedAsyncioTestCase):
    def app(self, api):
        return WorkTrackerApp(Path('/tmp/not-a-datastore'), rpc=api, poll_interval=0, clipboard_writer=lambda value: None)

    async def test_f2_selection_is_restored_by_a_fresh_tui_without_restoring_filters(self):
        api = FakeAPI()
        first = self.app(api)
        async with first.run_test(size=(100, 32)) as pilot:
            await settle(first, pilot)
            await pilot.press('f2')
            await pilot.pause()
            first.screen.query_one('#sort-field', Select).value = 'opened'
            first.screen.query_one('#sort-direction', Select).value = True
            await pilot.click('#sort-apply')
            await settle(first, pilot)
            first._project = 'Platform'
            self.assertEqual(api.sort_preference, {'field': 'opened', 'descending': True})
        second = self.app(api)
        async with second.run_test(size=(100, 32)) as pilot:
            await settle(second, pilot)
            self.assertEqual((second.sort_field, second.sort_descending), ('opened', True))
            self.assertEqual(second._project, '')
            second.action_clear_filters()
            self.assertEqual(api.sort_preference, {'field': 'opened', 'descending': True})
            await pilot.press('f2', 'escape')
            await pilot.pause()
            self.assertEqual(sum(method == 'set_pr_sort' for method, params in api.calls), 1)

    async def test_header_direction_changes_are_saved(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await settle(app, pilot)
            table = app.query_one('#prs', DataTable)
            x = sum(column.width + 2 * table.cell_padding for column in table.ordered_columns[:2]) + 2
            await pilot.click('#prs', offset=(x, 1))
            await settle(app, pilot)
            self.assertEqual(api.sort_preference, {'field': 'title', 'descending': False})
            await pilot.click('#prs', offset=(x, 1))
            await settle(app, pilot)
            self.assertEqual(api.sort_preference, {'field': 'title', 'descending': True})

    async def test_invalid_or_unavailable_saved_sort_does_not_hide_prs(self):
        for invalid in (True, False):
            with self.subTest(invalid=invalid):
                api = FakeAPI()
                if invalid:
                    api.sort_preference = {'field': 'unknown', 'descending': False}
                else:
                    api.fail_sort_read = True
                app = self.app(api)
                async with app.run_test(size=(100, 32)) as pilot:
                    await settle(app, pilot)
                    self.assertTrue(app.connected)
                    self.assertEqual(len(app.visible_rows), len(api.rows))
                    self.assertEqual((app.sort_field, app.sort_descending), ('pr', False))
                    self.assertFalse(any(method == 'set_pr_sort' for method, params in api.calls))

    async def test_save_failure_retains_local_choice_and_retries_on_reconnection(self):
        api = FakeAPI()
        api.fail_sort_save = True
        before = deepcopy(api.rows)
        app = self.app(api)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(app, pilot)
            app._select_sort('state-age', True)
            await settle(app, pilot)
            self.assertEqual((app.sort_field, app.sort_descending), ('state-age', True))
            self.assertEqual(api.sort_preference, {'field': 'pr', 'descending': False})
            self.assertIsNotNone(app._pending_sort)
            self.assertEqual(api.rows, before)
            api.fail_sort_save = False
            app.request_load()
            await settle(app, pilot)
            self.assertIsNone(app._pending_sort)
            self.assertEqual(api.sort_preference, {'field': 'state-age', 'descending': True})

    async def test_late_initial_read_cannot_replace_a_newer_user_choice(self):
        class SlowRead(FakeAPI):
            def __init__(self):
                super().__init__()
                self.sort_preference = {'field': 'review', 'descending': False}
                self.started, self.release = threading.Event(), threading.Event()
            def __call__(self, root, method, params=None):
                value = super().__call__(root, method, params)
                if method == 'get_pr_sort' and not self.started.is_set():
                    self.started.set()
                    if not self.release.wait(5):
                        raise RuntimeError('Test did not release the saved-sort read')
                return value
        api = SlowRead()
        self.addCleanup(api.release.set)
        app = self.app(api)
        async with app.run_test(size=(100, 32)) as pilot:
            self.assertTrue(await asyncio.to_thread(api.started.wait, 2))
            app._select_sort('opened', True)
            api.release.set()
            await settle(app, pilot)
            self.assertEqual((app.sort_field, app.sort_descending), ('opened', True))
            self.assertEqual(api.sort_preference, {'field': 'opened', 'descending': True})

    async def test_quit_waits_for_the_in_flight_sort_save(self):
        class SlowSave(FakeAPI):
            def __init__(self):
                super().__init__()
                self.started, self.release = threading.Event(), threading.Event()
            def __call__(self, root, method, params=None):
                if method == 'set_pr_sort':
                    self.started.set()
                    if not self.release.wait(5):
                        raise RuntimeError('Test did not release the sort save')
                return super().__call__(root, method, params)
        api = SlowSave()
        self.addCleanup(api.release.set)
        app = self.app(api)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(app, pilot)
            app._select_sort('opened', True)
            self.assertTrue(await asyncio.to_thread(api.started.wait, 2))
            quitting = asyncio.create_task(app.action_quit())
            await asyncio.sleep(0)
            self.assertFalse(quitting.done())
            api.release.set()
            await quitting
            self.assertEqual(api.sort_preference, {'field': 'opened', 'descending': True})

    async def test_rapid_choices_are_serialized_and_only_the_latest_pending_choice_is_saved(self):
        class SlowSave(FakeAPI):
            def __init__(self):
                super().__init__()
                self.started, self.release = threading.Event(), threading.Event()
            def __call__(self, root, method, params=None):
                if method == 'set_pr_sort' and not self.started.is_set():
                    self.started.set()
                    if not self.release.wait(5):
                        raise RuntimeError('Test did not release the sort save')
                return super().__call__(root, method, params)
        api = SlowSave()
        self.addCleanup(api.release.set)
        app = self.app(api)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(app, pilot)
            app._select_sort('opened', True)
            self.assertTrue(await asyncio.to_thread(api.started.wait, 2))
            app._select_sort('title', False)
            app._select_sort('state-age', True)
            api.release.set()
            await settle(app, pilot)
            writes = [params for method, params in api.calls if method == 'set_pr_sort']
            self.assertEqual(writes, [{'field': 'opened', 'descending': True}, {'field': 'state-age', 'descending': True}])
            self.assertEqual(api.sort_preference, {'field': 'state-age', 'descending': True})


if __name__ == '__main__':
    unittest.main()
