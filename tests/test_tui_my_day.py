from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import Button,DataTable,Input,Select,Static,TabbedContent

from test_tui import FakeAPI,PR
from test_tui_view_controls import settle
from work_tracker.service import Application
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_day import MyDayScreen
from work_tracker.tui_day_forms import DayItemForm


class DayAPI(FakeAPI):
    def __init__(self,root):
        super().__init__()
        self.now=datetime(2026,11,6,18,tzinfo=timezone.utc)
        self.backend=Application(root,run=mock.Mock(),clock=lambda:self.now)
        self.lose_receipt=False
        self.counter=0
        self.backend.dispatch('intent',{'key':'pr-fixture','kind':'register_pr','payload':{'url':PR,'project':'Alpha'}})

    def day(self): return self.backend.dispatch('my_day',{})

    def add(self,label='Outcome',kind='goal'):
        self.counter+=1
        return self.backend.dispatch('day_intent',{'key':'fixture-'+str(self.counter),'action':'add',
            'payload':{'day':self.day()['today'],'kind':kind,'text':label},'source':{'client':'fixture'}})

    def __call__(self,root,method,params=None):
        if method not in ('my_day','day_targets','day_intent'):
            return super().__call__(root,method,params)
        self.calls.append((method,deepcopy(params or {})))
        value=self.backend.dispatch(method,params or {})
        if method=='day_intent' and self.lose_receipt:
            self.lose_receipt=False
            raise RuntimeError('Response lost after commit')
        return value


class MyDayTUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api=DayAPI(self.temp.name)
        self.app=WorkTrackerApp(Path('/tmp/not-a-day-datastore'),rpc=self.api,poll_interval=0)

    async def quick(self,pilot,label,kind='goal'):
        await pilot.press('a' if kind=='goal' else 'w')
        await settle(self.app,pilot)
        self.assertIsInstance(self.app.screen,DayItemForm)
        self.app.screen.query_one('#day-label',Input).value=label
        await pilot.press('ctrl+enter')
        await settle(self.app,pilot)

    async def test_my_day_is_the_default_home_and_pr_hotkeys_do_not_leak(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,MyDayScreen)
            await pilot.press('m','x')
            self.assertFalse(any(method in ('merge_preview','close_pr') for method,_ in self.api.calls))
            await pilot.press('f4')
            await settle(self.app,pilot)
            self.assertEqual(len(self.app.screen_stack),1)
            self.assertTrue(self.app.query('#prs'))
            await pilot.press('f4')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,MyDayScreen)
            self.api.backend.run.assert_not_called()

    async def test_quick_add_and_done_change_only_the_daily_item(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await self.quick(pilot,'[bold]Daily outcome')
            self.assertIsInstance(self.app.screen,MyDayScreen)
            table=self.app.screen.query_one('#day-goals',DataTable)
            self.assertEqual(table.row_count,1)
            self.assertIn('[bold]Daily outcome',table.get_row_at(0)[1].plain)
            table.focus()
            await pilot.press('space')
            await settle(self.app,pilot)
            self.assertTrue(self.api.day()['goals'][0]['done'])
            self.assertFalse(self.api.day()['goals'][0]['target']['closed'])
            self.assertLessEqual(self.app.screen.query_one('#day-actions').region.bottom,24)

    async def test_awareness_stays_on_the_next_day_until_explicit_dismissal(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await self.quick(pilot,'Watch the rollout',kind='awareness')
            self.api.now+=timedelta(days=1)
            self.app.screen.action_reload()
            await settle(self.app,pilot)
            self.app.screen.query_one('#day-tabs',TabbedContent).active='day-awareness-pane'
            await pilot.pause()
            self.app.screen.query_one('#day-awareness',DataTable).focus()
            await pilot.press('delete')
            await settle(self.app,pilot)
            self.assertEqual(self.api.day()['awareness'],[])
            self.assertEqual(self.api.day()['goals'],[])

    async def test_carry_forward_is_explicit_and_completion_does_not_change_yesterday(self):
        self.api.add('Carry this outcome')
        previous=self.api.day()['day']
        self.api.now+=timedelta(days=1)
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            self.assertEqual(self.api.day()['goals'],[])
            self.app.screen.query_one('#day-tabs',TabbedContent).active='day-carry-pane'
            await pilot.pause()
            self.app.screen.query_one('#day-carry',DataTable).focus()
            await pilot.press('c')
            await settle(self.app,pilot)
            self.assertEqual(len(self.api.day()['goals']),1)
            self.app.screen.query_one('#day-tabs',TabbedContent).active='day-goals-pane'
            await pilot.pause()
            self.app.screen.query_one('#day-goals',DataTable).focus()
            await pilot.press('space')
            await settle(self.app,pilot)
            self.assertTrue(self.api.day()['goals'][0]['done'])
            self.assertFalse(self.api.backend.dispatch('my_day',{'day':previous})['goals'][0]['done'])

    async def test_lost_add_response_retries_the_identical_payload_without_duplicate_tasks(self):
        self.api.lose_receipt=True
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await self.quick(pilot,'Only one quick Task')
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertTrue(self.app.screen.query_one('#day-label',Input).disabled)
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,MyDayScreen)
            writes=[params for method,params in self.api.calls if method=='day_intent']
            self.assertEqual(writes[0],writes[1])
            self.assertEqual(len(self.api.day()['goals']),1)
            with self.api.backend.storage._connection(read=True) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],2) # PR Task plus quick Task

    async def test_uncertain_done_action_uses_retry_without_toggling_the_goal_twice(self):
        self.api.add()
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            self.api.lose_receipt=True
            self.app.screen.query_one('#day-goals',DataTable).focus()
            await pilot.press('space')
            await settle(self.app,pilot)
            self.assertIsNotNone(self.app.screen.pending)
            self.assertTrue(self.api.day()['goals'][0]['done'])
            await pilot.click('#day-retry')
            await settle(self.app,pilot)
            self.assertIsNone(self.app.screen.pending)
            writes=[params for method,params in self.api.calls if method=='day_intent']
            self.assertEqual(writes[0],writes[1])
            self.assertTrue(self.api.day()['goals'][0]['done'])

    async def test_stale_completion_reloads_instead_of_overwriting_another_client(self):
        self.api.add()
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            item=self.api.day()['goals'][0]
            self.api.backend.dispatch('day_intent',{'key':'another-client','action':'set_done',
                'payload':{'day':self.api.day()['today'],'item_id':item['id'],'expected_version':item['version'],'done':True},'source':{}})
            self.app.screen.query_one('#day-goals',DataTable).focus()
            await pilot.press('space')
            await settle(self.app,pilot)
            self.assertTrue(self.api.day()['goals'][0]['done'])
            self.assertIsNone(self.app.screen.pending)
            self.assertTrue(self.app.screen.data['goals'][0]['done'])

    async def test_pr_list_shortcut_links_the_existing_task_and_preserves_pr_metadata(self):
        before=self.api.backend.storage.export_registry()
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('f4')
            await settle(self.app,pilot)
            await pilot.press('a')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertEqual(self.app.screen.pr_url,PR)
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertEqual(len(self.app.screen_stack),1)
            item=self.api.day()['goals'][0]
            self.assertEqual(item['target']['prs'][0]['url'],PR)
            self.assertEqual(self.api.backend.storage.export_registry(),before)

    async def test_midnight_invalidates_an_unsent_form_instead_of_adding_to_the_wrong_day(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('a')
            await settle(self.app,pilot)
            self.app.screen.query_one('#day-label',Input).value='Old-day draft'
            self.api.now+=timedelta(days=1)
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertIn('calendar day changed',str(self.app.screen.query_one('#day-form-message',Static).render()))
            self.assertEqual(self.api.day()['goals'],[])

    async def test_target_picker_links_an_existing_workstream_and_day_history_is_read_only(self):
        async with self.app.run_test(size=(120,36)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('a')
            await settle(self.app,pilot)
            self.app.screen.query_one('#day-target-kind',Select).value='workstream'
            await settle(self.app,pilot)
            target=next(iter(self.app.screen.targets))
            self.app.screen.query_one('#day-target',Select).value=target
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertEqual(self.api.day()['goals'][0]['target_kind'],'workstream')
            self.app.screen.change_day(-1)
            await settle(self.app,pilot)
            self.assertFalse(self.app.screen.data['editable'])
            self.assertTrue(self.app.screen.query_one('#day-add',Button).disabled)
            self.assertTrue(self.app.screen.query_one('#day-toggle',Button).disabled)


if __name__=='__main__': unittest.main()
