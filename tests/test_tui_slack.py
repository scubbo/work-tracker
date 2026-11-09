from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import Button,Input,Select,Static,TextArea

from test_tui import FakeAPI,PR,THREAD,record
from test_tui_view_controls import settle
from work_tracker.service import Application
from work_tracker.tui import DetailScreen,WorkTrackerApp
from work_tracker.tui_slack import SlackThreadScreen


SECOND=THREAD.replace('/p1793390000000000','/p1793390000000001')+'?thread_ts=1793390000.000001&cid=C123'


class SlackThreadTests(unittest.IsolatedAsyncioTestCase):
    def app(self,api,opened=None,opener=None,start_view='prs'):
        return WorkTrackerApp(Path('/tmp/not-a-slack-datastore'),rpc=api,poll_interval=0,
                              opener=opener or (opened if opened is not None else []).append,start_view=start_view)

    async def test_single_thread_opens_with_l_and_the_visible_toolbar_button_without_writes(self):
        api,opened=FakeAPI(),[]
        before=deepcopy(api.rows)
        app=self.app(api,opened)
        async with app.run_test(size=(80,24)) as pilot:
            await settle(app,pilot)
            button=app.query_one('#open-slack-thread',Button)
            self.assertFalse(button.disabled)
            self.assertLessEqual(button.region.right,80)
            await pilot.press('l')
            await pilot.click('#open-slack-thread')
            self.assertEqual(opened,[THREAD,THREAD])
            self.assertEqual(len(app.screen_stack),1)
            self.assertEqual(api.rows,before)
            self.assertFalse(any(method in ('intent','refresh','collect') for method,_ in api.calls))

    async def test_no_thread_disables_button_and_explains_how_to_add_one(self):
        api,opened=FakeAPI([record(43)]),[]
        app=self.app(api,opened)
        async with app.run_test(size=(100,30)) as pilot:
            await settle(app,pilot)
            self.assertTrue(app.query_one('#open-slack-thread',Button).disabled)
            with mock.patch.object(app,'notify') as notify:
                await pilot.press('l')
                self.assertIn('Press t',notify.call_args.args[0])
            self.assertEqual(opened,[])

    async def test_multiple_threads_require_choice_and_preserve_original_pr_and_permalink_query(self):
        api,opened=FakeAPI(),[]
        api.rows[0]['slack-threads'].append({'url':SECOND,'description':'[bold]Review / discussion'})
        app=self.app(api,opened)
        async with app.run_test(size=(100,30)) as pilot:
            await settle(app,pilot)
            await pilot.press('l')
            await pilot.pause()
            self.assertIsInstance(app.screen,SlackThreadScreen)
            self.assertEqual(opened,[])
            self.assertEqual(app.screen.record['url'],PR)
            app.selected_url=api.rows[1]['url']
            api.rows[0]['slack-threads']=[]
            app.request_load()
            await settle(app,pilot)
            self.assertEqual(app.screen.record['url'],PR)
            app.screen.query_one('#slack-thread-choice',Select).value=SECOND
            await pilot.press('ctrl+enter')
            await pilot.pause()
            self.assertEqual(opened,[SECOND])
            self.assertEqual(len(app.screen_stack),1)

    async def test_chooser_cancel_or_missing_selection_never_opens_an_arbitrary_thread(self):
        api,opened=FakeAPI(),[]
        api.rows[0]['slack-threads'].append({'url':SECOND})
        app=self.app(api,opened)
        async with app.run_test(size=(80,24)) as pilot:
            await settle(app,pilot)
            await pilot.press('l')
            await pilot.pause()
            await pilot.click('#slack-thread-open')
            self.assertEqual(opened,[])
            self.assertIsInstance(app.screen,SlackThreadScreen)
            await pilot.press('escape')
            self.assertEqual(opened,[])

    async def test_offline_cached_reference_can_open_but_typing_l_or_a_modal_cannot_open_hidden_pr(self):
        api,opened=FakeAPI(),[]
        app=self.app(api,opened)
        async with app.run_test(size=(120,36)) as pilot:
            await settle(app,pilot)
            await pilot.press('/','l')
            self.assertEqual(app.query_one('#search',Input).value,'l')
            self.assertEqual(opened,[])
            app.action_clear_filters();app.action_table()
            await pilot.press('e')
            await settle(app,pilot)
            app.screen.query_one('#form-description',TextArea).focus()
            await pilot.press('l')
            self.assertEqual(opened,[])
            await pilot.press('escape')
            api.fail_read=True
            app.request_load()
            await settle(app,pilot)
            self.assertFalse(app.connected)
            app.action_table()
            await pilot.press('l')
            self.assertEqual(opened,[THREAD])

    async def test_details_l_uses_its_own_pr_not_a_changed_main_selection(self):
        api,opened=FakeAPI(),[]
        app=self.app(api,opened)
        async with app.run_test(size=(100,30)) as pilot:
            await settle(app,pilot)
            await pilot.press('enter')
            await pilot.pause()
            self.assertIsInstance(app.screen,DetailScreen)
            app.selected_url=api.rows[1]['url']
            await pilot.press('l')
            self.assertEqual(opened,[THREAD])

    async def test_invalid_url_control_characters_and_failed_link_handler_are_not_success(self):
        api,opened=FakeAPI(),[]
        app=self.app(api,opened)
        async with app.run_test(size=(100,30)) as pilot:
            await settle(app,pilot)
            for url in ('file:///etc/passwd','javascript:alert(1)','https://acme.slack.com.evil.example/archives/C123/p123',
                        'https://acme.slack.com/archives/C123/p123\n'):
                api.rows[0]['slack-threads']=[{'url':url}]
                app.request_load()
                await settle(app,pilot)
                await pilot.press('l')
            self.assertEqual(opened,[])
            api.rows[0]['slack-threads']=[{'url':THREAD},{'url':SECOND}]
            app.request_load()
            await settle(app,pilot)
            app.opener=mock.Mock(return_value=False)
            await pilot.press('l')
            await pilot.pause()
            app.screen.query_one('#slack-thread-choice',Select).value=THREAD
            await pilot.click('#slack-thread-open')
            self.assertIsInstance(app.screen,SlackThreadScreen)
            app.opener.assert_called_once_with(THREAD)

    async def test_my_day_does_not_open_a_hidden_pr_slack_thread(self):
        from test_tui_my_day import DayAPI
        with tempfile.TemporaryDirectory() as root:
            api,opened=DayAPI(root),[]
            app=self.app(api,opened,start_view='day')
            async with app.run_test(size=(100,30)) as pilot:
                await settle(app,pilot)
                await pilot.press('l')
                self.assertEqual(opened,[])
                self.assertEqual(len(app.screen_stack),2)


class SlackMetadataTests(unittest.TestCase):
    def test_existing_metadata_is_canonical_persistent_and_never_reads_or_posts_to_slack(self):
        with tempfile.TemporaryDirectory() as root:
            run=mock.Mock()
            app=Application(root,run=run)
            app.dispatch('intent',{'key':'register','kind':'register_pr','payload':{'url':PR}})
            payload={'url':PR,'thread':THREAD,'description':'Review, discussion, or ping context'}
            app.dispatch('intent',{'key':'attach','kind':'attach_slack','payload':payload})
            app.dispatch('intent',{'key':'attach','kind':'attach_slack','payload':payload})
            restored=Application(root,run=run)
            value=restored.dispatch('get_pr',{'url':PR})
            self.assertEqual(value['slack-threads'],[{'url':THREAD,'description':'Review, discussion, or ping context'}])
            self.assertNotIn('slack-thread',value)
            run.assert_not_called()


if __name__=='__main__':unittest.main()
