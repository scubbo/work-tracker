from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest

from test_tui import FakeAPI, PR, record

HAS_TEXTUAL = importlib.util.find_spec('textual') is not None
if HAS_TEXTUAL:
    from textual.widgets import Button, DataTable, Input, TextArea
    from work_tracker.tui import WorkTrackerApp
    from work_tracker.tui_close import ClosePRScreen


class ClosingAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.close_receipts = {}
        self.close_writes = []
        self.lose_close_receipt = False
        self.closing_status = 'succeeded'

    def __call__(self, root, method, params=None):
        params = params or {}
        if method == 'close_result':
            self.calls.append((method, deepcopy(params)))
            return deepcopy(self.close_receipts[params['key']][1])
        if method != 'close_pr':
            return super().__call__(root, method, params)
        with self.lock:
            self.calls.append((method, deepcopy(params)))
            key = params['key']
            if key in self.close_receipts:
                old, result = self.close_receipts[key]
                if old != params:
                    raise ValueError('Changed close request')
                return deepcopy(result)
            self.close_writes.append(deepcopy(params))
            result = {'key': key, 'url': params['url'], 'status': self.closing_status,
                      'message': 'PR closed on GitHub' if self.closing_status == 'succeeded' else 'Preflight failed. No write was sent.'}
            self.close_receipts[key] = (deepcopy(params), deepcopy(result))
            row = next(row for row in self.rows if row['url'] == params['url'])
            if self.closing_status == 'succeeded':
                row['github']['state'] = 'closed'
            elif self.closing_status == 'uncertain':
                row['close_request'] = {**deepcopy(result), 'comment': params['comment']}
            if self.lose_close_receipt:
                self.lose_close_receipt = False
                raise RuntimeError('Response lost')
            return result


@unittest.skipUnless(HAS_TEXTUAL, 'Install requirements.lock for Textual UI tests')
class PRActionTUITests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def app(self, api):
        return WorkTrackerApp(Path('/tmp/no-pr-action-database'), rpc=api, poll_interval=0, start_view='prs')

    async def test_listing_contains_state_age_and_unknown_history(self):
        api = ClosingAPI()
        api.rows[0]['state_changed_at'] = (datetime.now(timezone.utc)-timedelta(hours=2, minutes=5)).isoformat()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            table = app.query_one('#prs', DataTable)
            self.assertEqual(len(table.columns), 5)
            self.assertTrue(table.get_row_at(0)[3].plain.startswith('2h'))
            self.assertEqual(table.get_row_at(1)[3].plain, 'Unknown')
            self.assertFalse(any(method == 'close_pr' for method, params in api.calls))

    async def test_one_key_opens_dialog_and_escape_sends_no_close(self):
        api = ClosingAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('x')
            self.assertIsInstance(app.screen, ClosePRScreen)
            app.screen.query_one('#close-comment', TextArea).load_text('Draft only')
            await pilot.press('enter')
            self.assertEqual(api.close_writes, [])
            await pilot.press('escape')
            self.assertNotIsInstance(app.screen, ClosePRScreen)
            self.assertEqual(api.close_writes, [])

    async def test_close_uses_original_pr_target_and_comment_after_live_selection_changes(self):
        api = ClosingAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('x')
            comment = 'Superseded.\nFollow up in the other workstream.'
            app.screen.query_one('#close-comment', TextArea).load_text(comment)
            app.selected_url = api.rows[1]['url']
            app.request_load()
            await self.settle(app, pilot)
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual(len(api.close_writes), 1)
            self.assertEqual(api.close_writes[0]['url'], PR)
            self.assertEqual(api.close_writes[0]['comment'], comment)
            self.assertIs(api.close_writes[0]['confirmed'], True)
            self.assertNotIn(PR, [r['url'] for r in app.visible_rows])

    async def test_uncertain_response_freezes_comment_and_reuses_the_same_request(self):
        api = ClosingAPI()
        api.lose_close_receipt = True
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('x')
            app.screen.query_one('#close-comment', TextArea).load_text('Close once')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, ClosePRScreen)
            self.assertTrue(app.screen.query_one('#close-comment', TextArea).disabled)
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, ClosePRScreen)
            self.assertEqual(len(api.close_writes), 1)
            calls = [params for method, params in api.calls if method == 'close_pr']
            self.assertEqual(calls[0], calls[1])

    async def test_narrow_dialog_and_failed_close_keep_draft_visible(self):
        api = ClosingAPI()
        api.closing_status = 'failed'
        app = self.app(api)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('x')
            button = app.screen.query_one('#close-confirm', Button)
            self.assertLessEqual(button.region.bottom, 24)
            app.screen.query_one('#close-comment', TextArea).load_text('Retain this comment')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, ClosePRScreen)
            self.assertEqual(app.screen.query_one('#close-comment', TextArea).text, 'Retain this comment')
            self.assertTrue(app.screen.query_one('#close-confirm', Button).disabled)
            self.assertIn(PR, [row['url'] for row in app.visible_rows])

    async def test_reopening_uncertain_close_resumes_existing_request_without_new_writes(self):
        api = ClosingAPI()
        api.closing_status = 'uncertain'
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('x')
            app.screen.query_one('#close-comment', TextArea).load_text('Existing request')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            key = app.screen.key
            await pilot.press('escape')
            await self.settle(app, pilot)
            app.action_table()
            await pilot.press('x')
            self.assertEqual(app.screen.key, key)
            self.assertTrue(app.screen.query_one('#close-comment', TextArea).disabled)
            self.assertEqual(app.screen.query_one('#close-comment', TextArea).text, 'Existing request')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual(len(api.close_writes), 1)
            self.assertTrue(any(method == 'close_result' and params['check_github'] for method, params in api.calls))

    async def test_typing_x_in_search_does_not_open_close_dialog(self):
        api = ClosingAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('/', 'x')
            self.assertNotIsInstance(app.screen, ClosePRScreen)
            self.assertEqual(app.query_one('#search', Input).value, 'x')
            self.assertEqual(api.close_writes, [])


if __name__ == '__main__':
    unittest.main()
