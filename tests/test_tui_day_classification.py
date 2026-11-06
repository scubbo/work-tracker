from pathlib import Path
import tempfile
import unittest

from textual.widgets import DataTable,Input,Select,Static

from test_tui import PR
from test_tui_my_day import DayAPI
from test_tui_view_controls import settle
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_day import MyDayScreen
from work_tracker.tui_day_forms import DayItemForm


class DayClassificationTUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api=DayAPI(self.temp.name)
        self.app=WorkTrackerApp(Path('/tmp/no-classification-client-db'),rpc=self.api,poll_interval=0)

    async def form(self,pilot,key='a'):
        await settle(self.app,pilot)
        if key=='e': self.app.screen.query_one('#day-goals',DataTable).focus()
        await pilot.press(key)
        await settle(self.app,pilot)
        self.assertIsInstance(self.app.screen,DayItemForm)

    async def test_new_task_project_completion_and_workstream_are_saved_from_the_form(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await self.form(pilot)
            self.app.screen.query_one('#day-label',Input).value='Classified outcome'
            project=self.app.screen.query_one('#day-project',Input)
            project.value='al'
            project.focus()
            await pilot.pause()
            await pilot.press('right')
            self.assertEqual(project.value,'Alpha')
            self.app.screen.query_one('#day-workstream',Input).value='Review'
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,MyDayScreen)
            item=self.api.day()['goals'][0]
            self.assertEqual((item['target']['project_name'],item['target']['workstream_name']),('Alpha','Review'))
            self.assertFalse(item['target']['closed'])
            self.api.backend.run.assert_not_called()

    async def test_edit_assigns_an_existing_task_without_renaming_it_or_its_day_label(self):
        self.api.add('Keep this label')
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.form(pilot,'e')
            self.app.screen.query_one('#day-project',Input).value='Alpha'
            self.app.screen.query_one('#day-workstream',Input).value='Flow'
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            item=self.api.day()['goals'][0]
            self.assertEqual(item['label'],'Keep this label')
            self.assertEqual(item['target']['title'],'Keep this label')
            self.assertEqual((item['target']['project_name'],item['target']['workstream_name']),('Alpha','Flow'))
            request=next(params for method,params in self.api.calls if method=='day_intent')
            self.assertIn('expected_workstream_id',request['payload'])
            self.assertNotIn('text',request['payload'])

    async def test_task_classification_conflict_does_not_overwrite_another_metadata_editor(self):
        self.api.backend.dispatch('day_intent',{'key':'link-pr','action':'add',
            'payload':{'day':self.api.day()['today'],'kind':'goal','target_kind':'pr','url':PR},'source':{}})
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.form(pilot,'e')
            self.api.backend.dispatch('intent',{'key':'other-client','kind':'update_pr','payload':{'url':PR,'project':'Beta'}})
            self.app.screen.query_one('#day-project',Input).value='Gamma'
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertIn('classification changed',str(self.app.screen.query_one('#day-form-message',Static).render()))
            self.assertEqual(self.api.day()['goals'][0]['target']['project_name'],'Beta')

    async def test_lost_assignment_response_retries_the_same_target_precondition_and_key(self):
        self.api.add()
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.form(pilot,'e')
            self.app.screen.query_one('#day-project',Input).value='Alpha'
            self.api.lose_receipt=True
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertTrue(self.app.screen.query_one('#day-project',Input).disabled)
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,MyDayScreen)
            writes=[params for method,params in self.api.calls if method=='day_intent']
            self.assertEqual(writes[0],writes[1])
            self.assertEqual(self.api.day()['goals'][0]['target']['project_name'],'Alpha')

    async def test_existing_target_linking_inherits_classification_instead_of_moving_the_target(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.form(pilot)
            self.app.screen.query_one('#day-target-kind',Select).value='task'
            await settle(self.app,pilot)
            self.assertTrue(self.app.screen.query_one('#day-project',Input).disabled)
            self.assertTrue(self.app.screen.query_one('#day-workstream',Input).disabled)
            target=next(iter(self.app.screen.targets))
            self.app.screen.query_one('#day-target',Select).value=target
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertEqual(self.api.day()['goals'][0]['target']['project_name'],'Alpha')

    async def test_catalog_failure_allows_explicit_names_and_keeps_the_warning_visible(self):
        self.api.fail_catalog=True
        async with self.app.run_test(size=(80,24)) as pilot:
            await self.form(pilot)
            self.assertIn('unavailable',str(self.app.screen.query_one('#day-classification-status',Static).render()))
            self.app.screen.query_one('#day-label',Input).value='Manual names'
            self.app.screen.query_one('#day-project',Input).value='New Project'
            self.app.screen.query_one('#day-workstream',Input).value='New Stream'
            await pilot.press('ctrl+enter')
            await settle(self.app,pilot)
            self.assertEqual(self.api.day()['goals'][0]['target']['project_name'],'New Project')


if __name__=='__main__': unittest.main()
