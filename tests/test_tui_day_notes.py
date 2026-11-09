from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from textual.widgets import DataTable,Input,Static,TextArea
from test_tui_my_day import DayAPI
from test_tui_view_controls import settle
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_day import MyDayScreen
from work_tracker.tui_day_forms import DayItemDetails,DayItemForm


class GoalNotesTUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.api=DayAPI(self.temp.name)
        self.app=WorkTrackerApp(Path('/tmp/not-a-notes-datastore'),rpc=self.api,poll_interval=0)

    async def test_add_multiline_notes_enter_adds_line_and_details_show_literal_full_text(self):
        async with self.app.run_test(size=(80,24)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('a');await settle(self.app,pilot)
            self.app.screen.query_one('#day-label',Input).value='Daily outcome'
            notes=self.app.screen.query_one('#day-notes',TextArea)
            notes.load_text('[bold]First line');notes.move_cursor((0,len(notes.text)))
            notes.scroll_visible(animate=False);notes.focus()
            await pilot.press('enter','n','e','x','t')
            self.assertIsInstance(self.app.screen,DayItemForm)
            self.assertEqual(notes.text,'[bold]First line\nnext')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            item=self.api.day()['goals'][0]
            self.assertEqual(item['notes'],'[bold]First line\nnext')
            table=self.app.screen.query_one('#day-goals',DataTable)
            self.assertIn('[notes]',table.get_row_at(0)[1].plain)
            table.focus();await pilot.press('enter');await pilot.pause()
            self.assertIsInstance(self.app.screen,DayItemDetails)
            shown=self.app.screen.query_one('#day-notes-full',Static)
            self.assertEqual(str(shown.render()),item['notes'])
            await pilot.press('escape')
            self.assertIsInstance(self.app.screen,MyDayScreen)

    async def test_edit_notes_only_keeps_label_and_classification_then_can_clear_notes(self):
        self.api.add('Keep this label')
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            before=deepcopy(self.api.day()['goals'][0])
            await pilot.press('e');await settle(self.app,pilot)
            self.app.screen.query_one('#day-notes',TextArea).load_text('Two\nlines')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            item=self.api.day()['goals'][0]
            self.assertEqual(item['notes'],'Two\nlines')
            self.assertEqual(item['label'],before['label']);self.assertEqual(item['target'],before['target'])
            payload=[params['payload'] for method,params in self.api.calls if method=='day_intent'][-1]
            self.assertEqual(set(payload),{'day','item_id','expected_version','notes'})
            await pilot.press('e');await settle(self.app,pilot)
            self.assertEqual(self.app.screen.query_one('#day-notes',TextArea).text,'Two\nlines')
            self.app.screen.query_one('#day-notes',TextArea).load_text('')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            self.assertEqual(self.api.day()['goals'][0]['notes'],'')

    async def test_unknown_response_freezes_notes_and_retries_the_exact_request(self):
        self.api.add();self.api.lose_receipt=True
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('e');await settle(self.app,pilot)
            editor=self.app.screen.query_one('#day-notes',TextArea)
            editor.load_text('Save\nonce')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            self.assertTrue(editor.disabled)
            frozen=deepcopy(self.app.screen.pending)
            editor.load_text('Cannot replace the frozen request')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            requests=[params for method,params in self.api.calls if method=='day_intent']
            self.assertEqual(requests,[frozen,frozen])
            self.assertEqual(self.api.day()['goals'][0]['notes'],'Save\nonce')

    async def test_oversized_notes_keep_editable_draft_and_cancel_does_not_write(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            await pilot.press('a');await settle(self.app,pilot)
            self.app.screen.query_one('#day-label',Input).value='Draft'
            notes=self.app.screen.query_one('#day-notes',TextArea);notes.load_text('x'*20001)
            await pilot.press('ctrl+enter');await pilot.pause()
            self.assertIsNone(self.app.screen.pending);self.assertFalse(notes.disabled)
            self.assertIn('20,000',str(self.app.screen.query_one('#day-form-message',Static).render()))
            await pilot.press('escape')
            self.assertEqual(self.api.day()['goals'],[])

    async def test_history_and_offline_details_can_read_notes_but_cannot_edit_them(self):
        self.api.add()
        item=self.api.day()['goals'][0]
        self.api.backend.dispatch('day_intent',{'key':'fixture-notes','action':'edit','source':{'client':'test'},
            'payload':{'day':item['day'],'item_id':item['id'],'expected_version':item['version'],'notes':'Historical\nnotes'}})
        self.api.now+=timedelta(days=1)
        async with self.app.run_test(size=(100,32)) as pilot:
            await settle(self.app,pilot)
            self.app.screen.change_day(-1);await settle(self.app,pilot)
            self.assertFalse(self.app.screen.data['editable'])
            await pilot.press('e');self.assertIsInstance(self.app.screen,MyDayScreen)
            self.app.screen.connected=False
            self.app.screen.query_one('#day-goals',DataTable).focus()
            await pilot.press('enter');await pilot.pause()
            self.assertIsInstance(self.app.screen,DayItemDetails)
            self.assertEqual(str(self.app.screen.query_one('#day-notes-full',Static).render()),'Historical\nnotes')
            self.assertFalse(self.app.screen.query(TextArea))
            self.assertFalse(any(method=='day_intent' for method,_ in self.api.calls))


if __name__=='__main__':unittest.main()
