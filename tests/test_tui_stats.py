from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import DataTable, Select, Static

from test_tui import FakeAPI, PR
from test_tui_view_controls import settle
from work_tracker.service import Application
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_stats import StatisticsScreen


class StatsAPI(FakeAPI):
    def __init__(self, root):
        super().__init__()
        self.backend = Application(root, run=mock.Mock(), clock=lambda:datetime(2026,11,5,12,tzinfo=timezone.utc))
        self.backend.dispatch('intent',{'key':'stats-fixture','kind':'register_pr','payload':{'url':PR,'project':'[bold]Alpha'}})
        self.fail_stats = False

    def __call__(self, root, method, params=None):
        if method=='statistics':
            self.calls.append((method,deepcopy(params)))
            if self.fail_stats: raise RuntimeError('Offline')
            return self.backend.dispatch(method,params)
        return super().__call__(root,method,params)


class StatsTUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api=StatsAPI(self.temp.name)
        self.app=WorkTrackerApp(Path('/tmp/not-a-statistics-datastore'),rpc=self.api,poll_interval=0,start_view='prs')

    async def test_f3_opens_saved_history_without_capture_or_external_queries(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('f3')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,StatisticsScreen)
            table=self.app.screen.query_one('#stats-table',DataTable)
            self.assertEqual(table.row_count,1)
            self.assertEqual(table.get_row_at(0)[1].plain,'Overall')
            self.assertTrue(any(method=='statistics' for method,_ in self.api.calls))
            self.assertFalse(any(method=='capture_statistics' for method,_ in self.api.calls))
            self.api.backend.run.assert_not_called()
            await pilot.press('escape')
            self.assertEqual(len(self.app.screen_stack),1)

    async def test_all_project_filter_includes_unallocated_at_zero_and_keeps_names_literal(self):
        async with self.app.run_test(size=(100,30)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('f3')
            await settle(self.app,pilot)
            self.app.screen.query_one('#stats-project',Select).value='*'
            await settle(self.app,pilot)
            table=self.app.screen.query_one('#stats-table',DataTable)
            names=[table.get_row_at(i)[1].plain for i in range(table.row_count)]
            self.assertEqual(set(names),{'Overall','Unallocated','[bold]Alpha'})
            index=names.index('Unallocated')
            self.assertEqual(str(table.get_row_at(index)[2]),'0')
            projects=self.api.backend.dispatch('statistics',{})['projects']
            unallocated=next(project['id'] for project in projects if project['name']=='Unallocated')
            self.app.screen.query_one('#stats-project',Select).value=unallocated
            await settle(self.app,pilot)
            self.assertEqual(self.app.screen.query_one('#stats-table',DataTable).row_count,1)
            self.assertTrue(any(method=='statistics' and params.get('project')==unallocated for method,params in self.api.calls))

    async def test_offline_reload_retains_previous_rows_and_explains_the_failure(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('f3')
            await settle(self.app,pilot)
            self.api.fail_stats=True
            await pilot.press('r')
            await settle(self.app,pilot)
            self.assertEqual(self.app.screen.query_one('#stats-table',DataTable).row_count,1)
            self.assertIn('unavailable',str(self.app.screen.query_one('#stats-status',Static).render()))
            self.assertFalse(any(method=='refresh' for method,_ in self.api.calls))

    async def test_days_filter_is_passed_to_the_application_and_modal_hotkeys_do_not_leak(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('f3')
            await settle(self.app,pilot)
            self.app.screen.query_one('#stats-days',Select).value=7
            await settle(self.app,pilot)
            self.assertTrue(any(method=='statistics' and params['days']==7 for method,params in self.api.calls))
            await pilot.press('m','x','f3')
            self.assertEqual(len(self.app.screen_stack),2)
            self.assertIsInstance(self.app.screen,StatisticsScreen)
            self.assertFalse(any(method in ('merge_preview','close_pr') for method,_ in self.api.calls))


if __name__=='__main__': unittest.main()
