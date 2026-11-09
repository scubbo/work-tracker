from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import Button,DataTable,Static,TabbedContent
from test_tui_my_day import DayAPI
from test_tui_view_controls import settle
from work_tracker.tui import WorkTrackerApp


class CarryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.api=DayAPI(self.temp.name)
        self.api.add('Prior unfinished outcome')
        self.previous=self.api.day()['day']
        self.original=deepcopy(self.api.day()['goals'][0])
        self.api.now+=timedelta(days=3)
        self.api.add('Already planned today')
        self.app=WorkTrackerApp(Path('/tmp/not-a-carry-datastore'),rpc=self.api,poll_interval=0)

    def writes(self): return [params for method,params in self.api.calls if method=='day_intent']

    async def assert_revealed(self,pilot):
        await settle(self.app,pilot)
        self.assertEqual(self.app.screen.active_table(),'day-goals')
        item=self.app.screen.selected_item()
        self.assertEqual(item['carried_from'],self.original['id'])
        self.assertEqual(item['label'],self.original['label'])
        self.assertEqual(self.app.screen.query_one('#day-goals',DataTable).cursor_row,1)
        self.assertIsNone(self.app.screen.fixed_day)
        self.assertEqual(self.api.backend.dispatch('my_day',{'day':self.previous})['goals'],[self.original])

    async def test_mouse_carry_from_goals_opens_choices_then_reveals_the_exact_new_goal(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            button=self.app.screen.query_one('#day-carry-action',Button)
            self.assertFalse(button.disabled);self.assertLessEqual(button.region.right,80)
            await pilot.click('#day-carry-action');await settle(self.app,pilot)
            self.assertEqual(self.app.screen.active_table(),'day-carry')
            self.assertEqual(self.writes(),[])
            self.assertEqual(len(self.api.day()['goals']),1)
            await pilot.click('#day-carry-action')
            await self.assert_revealed(pilot)
            self.assertEqual(len(self.writes()),1)
            self.assertEqual(len(self.api.day()['goals']),2)

    async def test_c_from_goals_no_longer_silently_does_nothing(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('c');await settle(self.app,pilot)
            self.assertEqual(self.app.screen.active_table(),'day-carry')
            self.assertEqual(self.writes(),[])
            await pilot.press('c')
            await self.assert_revealed(pilot)

    async def test_c_from_history_opens_today_choices_without_mutating_history(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            self.app.screen.change_day(-3);await settle(self.app,pilot)
            self.assertFalse(self.app.screen.data['editable'])
            await pilot.press('c');await settle(self.app,pilot)
            self.assertTrue(self.app.screen.data['editable'])
            self.assertEqual(self.app.screen.active_table(),'day-carry')
            self.assertEqual(self.app.screen.selected_item()['id'],self.original['id'])
            self.assertEqual(self.writes(),[])
            await pilot.press('c')
            await self.assert_revealed(pilot)

    async def test_empty_carry_list_explains_why_no_goal_is_added(self):
        item=self.original
        self.api.backend.dispatch('day_intent',{'key':'finish-prior','action':'carry','source':{'client':'test'},
            'payload':{'day':self.api.day()['today'],'item_id':item['id'],'expected_version':item['version']}})
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('c');await settle(self.app,pilot)
            self.assertIn('No unfinished previous goals',str(self.app.screen.query_one('#day-detail',Static).render()))
            with mock.patch.object(self.app,'notify') as notify:
                await pilot.press('c')
                self.assertIn('No unfinished previous goals',notify.call_args.args[0])
            self.assertEqual(self.writes(),[])

    async def test_unknown_carry_receipt_retries_with_ctrl_r_once_and_reveals_committed_goal(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('c');await settle(self.app,pilot)
            self.api.lose_receipt=True
            await pilot.press('c');await settle(self.app,pilot)
            pending=deepcopy(self.app.screen.pending)
            self.assertIsNotNone(pending)
            self.app.screen.action_reload();await settle(self.app,pilot)
            self.assertEqual(self.writes(),[pending])
            with mock.patch.object(self.app,'notify') as notify:
                await pilot.press('c')
                self.assertIn('pending request',notify.call_args.args[0])
            await pilot.press('ctrl+r')
            await self.assert_revealed(pilot)
            self.assertEqual(self.writes(),[pending,pending])
            self.assertEqual(len(self.api.day()['goals']),2)

    async def test_offline_shortcut_explains_reconnection_without_writes(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            self.app.screen.connected=False;self.app.screen.update_buttons()
            self.assertTrue(self.app.screen.query_one('#day-carry-action',Button).disabled)
            with mock.patch.object(self.app,'notify') as notify:
                await pilot.press('c')
                self.assertIn('Reconnect',notify.call_args.args[0])
            self.assertEqual(self.writes(),[])

    async def test_midnight_retry_reveals_receipt_day_instead_of_hiding_the_copied_goal(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('c');await settle(self.app,pilot)
            self.api.lose_receipt=True
            await pilot.press('c');await settle(self.app,pilot)
            receipt_day=self.api.day()['today']
            self.api.now+=timedelta(days=1)
            await pilot.press('ctrl+r');await settle(self.app,pilot)
            self.assertEqual(self.app.screen.active_table(),'day-goals')
            self.assertEqual(self.app.screen.data['day'],receipt_day)
            self.assertFalse(self.app.screen.data['editable'])
            self.assertEqual(self.app.screen.selected_item()['carried_from'],self.original['id'])
            self.assertEqual(len(self.writes()),2)


if __name__=='__main__':unittest.main()
