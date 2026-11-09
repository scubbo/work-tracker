from copy import deepcopy
from pathlib import Path
import os,tempfile,unittest
from unittest import mock

from textual.widgets import Button,DataTable,Input,Static,TabbedContent,TextArea,Select
from test_tui_my_day import DayAPI
from test_tui_view_controls import settle
from work_tracker.tui import WorkTrackerApp
from work_tracker.tui_followup import FollowupScreen,TaskFollowupForm


class FollowAPI(DayAPI):
    def __init__(self,root):
        super().__init__(root);self.fail_followup=False;self.lose_followup=False
    def __call__(self,root,method,params=None):
        if method.startswith('followup'):
            self.calls.append((method,deepcopy(params or {})))
            if self.fail_followup:raise RuntimeError('Connection unavailable')
            value=self.backend.dispatch(method,params or {})
            if self.lose_followup and method=='followup_task' and 'expected_version' in (params or {}):
                self.lose_followup=False;raise RuntimeError('Lost response')
            return value
        return super().__call__(root,method,params)


class FollowupUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=mock.patch.dict(os.environ,{'AI_GATEWAY_API_KEY':'','VERCEL_AI_GATEWAY_API_KEY':''});self.env.start();self.addCleanup(self.env.stop)
        self.api=FollowAPI(self.temp.name)
        self.app=WorkTrackerApp(Path('/tmp/not-followup-client-data'),rpc=self.api,poll_interval=0,opener=mock.Mock())

    async def open(self,pilot):
        await settle(self.app,pilot);await pilot.press('f5');await settle(self.app,pilot)
        self.assertIsInstance(self.app.screen,FollowupScreen)

    async def test_f5_from_my_day_is_read_only_and_missing_key_does_not_fake_ai(self):
        before=self.api.day()
        async with self.app.run_test(size=(80,24)) as pilot:
            await self.open(pilot)
            self.assertEqual(self.app.screen.query_one('#followup-next',DataTable).row_count,1)
            self.assertTrue(self.app.screen.query_one('#followup-analyze',Button).disabled)
            self.assertIn('key missing',str(self.app.screen.query_one('#followup-status',Static).render()))
            self.assertLessEqual(self.app.screen.query_one('#followup-back',Button).region.bottom,24)
            self.assertFalse(any(method=='followup_analyze' for method,_ in self.api.calls))
            self.assertEqual(self.api.day(),before)
            await pilot.press('escape')
            self.assertEqual(len(self.app.screen_stack),2)

    async def test_f5_from_pr_list_and_modal_keys_do_not_act_on_hidden_selection(self):
        async with self.app.run_test(size=(120,36)) as pilot:
            await settle(self.app,pilot);await pilot.press('f4');await settle(self.app,pilot)
            await pilot.press('f5');await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,FollowupScreen)
            await pilot.press('m','x','p','l','a')
            self.app.opener.assert_not_called()
            self.assertFalse(any(method in ('merge_preview','close_pr','day_intent','intent') for method,_ in self.api.calls))

    async def test_task_metadata_editor_changes_only_followup_and_retry_preserves_frozen_values(self):
        before=self.api.backend.storage.export_registry();plan=self.api.day()
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.open(pilot)
            await pilot.click('#followup-edit');await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,TaskFollowupForm)
            self.app.screen.query_one('#followup-next-action',TextArea).load_text('Discuss the next change\nwith the reviewer')
            self.app.screen.query_one('#followup-waiting-on',Input).value='Known reviewer'
            self.api.lose_followup=True
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            frozen=deepcopy(self.app.screen.pending)
            self.app.screen.query_one('#followup-next-action',TextArea).load_text('Must not replace the frozen request')
            await pilot.press('ctrl+enter');await settle(self.app,pilot)
            self.assertIsInstance(self.app.screen,FollowupScreen)
            writes=[params for method,params in self.api.calls if method=='followup_task' and 'expected_version' in params]
            self.assertEqual(writes,[frozen,frozen])
            self.assertIn('Discuss the next change',str(self.app.screen.query_one('#followup-advice',Static).render()))
            self.assertEqual(self.api.backend.storage.export_registry(),before)
            self.assertEqual(self.api.day(),plan)

    async def test_scope_filters_and_cracks_tab_use_local_facts_only(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.open(pilot)
            self.app.screen.query_one('#followup-tabs',TabbedContent).active='followup-stale-pane'
            await pilot.pause()
            self.assertEqual(self.app.screen.active_table(),'followup-stale')
            self.app.screen.query_one('#followup-project',Select).value='Alpha'
            await settle(self.app,pilot)
            reads=[params for method,params in self.api.calls if method=='followup']
            self.assertEqual(reads[-1]['project'],'Alpha')
            self.assertFalse(any(method=='followup_analyze' for method,_ in self.api.calls))

    async def test_ai_button_queues_explicitly_and_status_reads_never_run_a_model(self):
        import json
        self.api.backend.dispatch('followup_key',{'key':'synthetic-ui-test-value'})
        self.api.backend.dispatch('intent',{'key':'second-pr','kind':'register_pr','payload':{'url':'https://github.com/acme/service/pull/43','project':'Alpha'}})
        calls=[]
        def transport(payload,key):
            frame=json.loads(payload['messages'][-1]['content']);calls.append(frame)
            response={'mode':'clarify','candidate_ids':[frame['candidates'][-1]['id']],'reason':'Interpretation only','question':'Which outcome matters?', 'context_requests':[]}
            return {'choices':[{'message':{'content':json.dumps(response)}}],'usage':{'total_tokens':12}}
        self.api.backend.followup.gateway.transport=transport
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.open(pilot)
            self.assertEqual(calls,[])
            await pilot.click('#followup-analyze');await settle(self.app,pilot)
            self.assertEqual(calls,[])
            self.assertTrue(self.app.screen.query_one('#followup-analyze',Button).disabled)
            for job in self.api.backend.storage.jobs():
                if job['name']!='followup':
                    name=job.pop('name');job['next_run']='2030-01-01T00:00:00Z';self.api.backend.storage.save_job(name,job)
            self.api.backend.scheduler.step()
            self.app.screen.action_reload();await settle(self.app,pilot)
            self.assertEqual(len(calls),1)
            self.assertEqual(self.app.screen.selected()['id'],calls[0]['candidates'][-1]['id'])
            self.assertIn('AI interpretation',str(self.app.screen.query_one('#followup-advice',Static).render()))
            self.assertIn('Which outcome matters?',str(self.app.screen.query_one('#followup-advice',Static).render()))
            self.app.screen.action_reload();await settle(self.app,pilot)
            self.assertEqual(len(calls),1)

    async def test_offline_read_retains_visible_facts_without_silent_remote_processing(self):
        async with self.app.run_test(size=(100,32)) as pilot:
            await self.open(pilot)
            count=self.app.screen.query_one('#followup-next',DataTable).row_count
            self.api.fail_followup=True;self.app.screen.action_reload();await settle(self.app,pilot)
            self.assertEqual(self.app.screen.query_one('#followup-next',DataTable).row_count,count)
            self.assertIn('unavailable',str(self.app.screen.query_one('#followup-status',Static).render()))
            self.assertFalse(any(method=='followup_analyze' for method,_ in self.api.calls))


if __name__=='__main__':unittest.main()
