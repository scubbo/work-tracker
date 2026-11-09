from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io,json,os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from work_tracker import cli
from work_tracker.service import Application
from work_tracker.gateway import Gateway,NoRedirect
from work_tracker.followup import build_report,filters


PR='https://github.com/acme/service/pull/42'
THREAD='https://acme.slack.com/archives/C123/p1793390000000000'


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=mock.patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.now=datetime(2026,11,20,18,tzinfo=timezone.utc)
        self.native=mock.Mock(return_value=[])
        self.app=Application(self.temp.name,clock=lambda:self.now,run=self.native)
        for job in self.app.storage.jobs():
            if job['name']!='followup':
                name=job.pop('name');job['next_run']='2030-01-01T00:00:00Z';self.app.storage.save_job(name,job)
        self.counter=0

    def register(self,number=42,state='Awaiting review'):
        url=PR.replace('/42','/'+str(number))
        self.app.dispatch('intent',{'key':'pr-'+str(number),'kind':'register_pr','payload':{'url':url,'project':'Alpha','workstream':'Flow'}})
        self.app.storage.observe(url,{'state':'open','title':'Synthetic change '+str(number),'body':'PRIVATE_BODY_MUST_NOT_BE_SENT_INITIALLY',
            'isDraft':False,'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','createdAt':'2026-11-01T00:00:00Z'},state,'Cached evidence')
        with self.app.storage._connection() as db:
            db.execute('UPDATE prs SET observed_at=?,state_first_seen_at=? WHERE url=?',(self.now.isoformat(),(self.now-timedelta(days=10)).isoformat(),url));db.commit()
        return next(item for item in self.app.storage.list_prs(state=None) if item['url']==url)

    def read(self,**params):return self.app.dispatch('followup',params)

    def guide(self,task,**params):
        current=self.app.dispatch('followup_task',{'task_id':task})
        return self.app.dispatch('followup_task',{'task_id':task,'expected_version':current['version'],**params})

    def configure(self):self.app.dispatch('followup_key',{'key':'synthetic-test-value'})

    def transport(self,mode='clarify',request_context=None):
        frames=[]
        def call(payload,key):
            frame=json.loads(payload['messages'][-1]['content']);frames.append(frame)
            identifier=frame['candidates'][0]['id']
            response={'mode':mode,'candidate_ids':[identifier],'reason':'Based on the supplied evidence','question':'Which outcome matters?' if mode=='clarify' else '', 'context_requests':[]}
            if request_context and len(frames)==1:
                response.update(mode='context',reason='Need the selected discussion to resolve a blocker',context_requests=[{'candidate_id':identifier,'source':request_context}])
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(response)}}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
        self.app.followup.gateway.transport=mock.Mock(side_effect=call)
        return frames

    def analyze(self,key='analysis',**scope):
        self.app.dispatch('followup_analyze',{'key':key,**scope});self.app.scheduler.step()
        return self.app.dispatch('followup_runs',{'key':key})

    def test_local_advice_without_key_or_external_reads_and_neutral_competitors(self):
        self.register();self.register(43,'Ready to Merge');self.native.reset_mock()
        value=self.read()
        self.assertEqual(value['next']['mode'],'clarify');self.assertEqual(len(value['candidates']),2)
        self.assertFalse(value['gateway']['configured']);self.native.assert_not_called()
        self.assertEqual(len(value['stale']),2)
        self.assertIn('does not prove human inactivity',value['stale'][0]['signals'][0])

    def test_single_fresh_merge_candidate_is_advice_not_execution_and_stale_observation_downgrades(self):
        row=self.register(state='Ready to Merge')
        value=self.read();self.assertEqual(value['next']['mode'],'recommend')
        self.assertEqual(value['candidates'][0]['kind'],'merge')
        self.now+=timedelta(hours=1)
        self.assertEqual(self.read()['candidates'][0]['kind'],'refresh')
        self.native.assert_not_called()

    def test_dependencies_versions_cycles_snooze_and_scope_are_explicit(self):
        first=self.register();second=self.register(43)
        before=self.app.storage.export_registry();plan=self.app.dispatch('my_day',{})
        value=self.guide(first['task_id'],dependencies=[second['task_id']],next_action='Open the next PR',after_task='Book a conversation',waiting_on='Known collaborator')
        self.assertEqual(next(item for item in self.read()['candidates'] if item['task_id']==first['task_id'])['kind'],'dependency')
        with self.assertRaises(ValueError):self.guide(second['task_id'],dependencies=[first['task_id']])
        with self.assertRaises(ValueError):self.guide(first['task_id'],dependencies=['invented'])
        with self.assertRaises(ValueError):self.app.dispatch('followup_task',{'task_id':first['task_id'],'expected_version':0,'next_action':'Stale write'})
        duplicate=self.app.dispatch('followup_task',{'task_id':first['task_id'],'expected_version':0,'next_action':'Open the next PR'})
        self.assertTrue(duplicate['duplicate'])
        self.guide(first['task_id'],revisit_after='2026-11-25')
        self.assertEqual(len(self.read()['candidates']),1)
        self.assertEqual(self.read()['snoozed_count'],1)
        self.assertEqual(self.read(project='Other')['candidates'],[])
        self.assertEqual(self.app.storage.export_registry(),before);self.assertEqual(self.app.dispatch('my_day',{}),plan)

    def test_pending_operation_remains_visible_and_overrides_other_advice(self):
        row=self.register(state='Ready to Merge')
        self.app.storage.begin_close('pending-close',PR,'')
        self.assertEqual(self.read()['candidates'][0]['kind'],'check_outcome')
        self.app.storage.observe(PR,{'state':'closed'},'Blocked','Closed')
        self.assertEqual(self.read()['candidates'][0]['kind'],'check_outcome')

    def test_follow_on_after_merged_pr_asks_about_task_completion_instead_of_assuming_it(self):
        row=self.register();self.guide(row['task_id'],after_task='Prepare the next PR')
        self.app.storage.observe(PR,{'state':'merged'},'Blocked','Merged')
        value=self.read()['candidates'][0]
        self.assertEqual(value['kind'],'clarify_completion')
        self.assertIn('Confirm whether this Task is complete',value['action'])
        self.assertFalse(self.app.storage.followup_snapshot(self.now)['tasks'][0]['closed'])

    def test_task_without_pr_is_retained_and_closed_pr_defaults_do_not_flood_advice(self):
        row=self.register()
        self.app.storage.observe(PR,{'state':'merged'},'Blocked','Merged')
        self.assertEqual(self.read()['candidates'],[])
        day=self.app.dispatch('my_day',{})
        self.app.dispatch('day_intent',{'key':'task','action':'add','payload':{'day':day['today'],'kind':'goal','text':'No PR outcome'},'source':{'client':'test'}})
        self.assertEqual(self.read()['candidates'][0]['kind'],'clarify_task')
        self.assertEqual(self.read()['unknown_age_count'],0)

    def test_key_is_private_redacted_and_masked_cli_never_prints_it(self):
        self.configure();path=Path(self.temp.name)/'gateway.key'
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        status=self.read()['gateway'];self.assertNotIn('synthetic-test-value',json.dumps(status))
        path.chmod(0o644);self.assertFalse(self.read()['gateway']['configured'])
        with mock.patch.object(cli,'request',side_effect=lambda root,method,params=None:self.app.dispatch(method,params or {})),mock.patch('sys.stdin.isatty',return_value=True),mock.patch('getpass.getpass',return_value='second-synthetic-value'),mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'follow-up','key']),0)
            self.assertNotIn('second-synthetic-value',output.getvalue())
        self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_cloud_analysis_is_manual_only_and_key_retries_are_idempotent(self):
        self.register();self.configure();frames=self.transport('recommend')
        self.app.scheduler.step();self.assertEqual(frames,[])
        run=self.analyze()
        self.assertEqual(run['status'],'succeeded');self.assertEqual(len(frames),1)
        self.assertIn('ai_report_key',self.read())
        again=self.app.dispatch('followup_analyze',{'key':'analysis'})
        self.assertTrue(again['duplicate']);self.app.scheduler.step();self.assertEqual(len(frames),1)
        self.now+=timedelta(days=5);self.app.scheduler.step();self.assertEqual(len(frames),1)
        with self.assertRaises(ValueError):self.app.dispatch('followup_analyze',{'key':'analysis','project':'Other'})

    def test_gateway_credential_is_redacted_if_it_appears_in_user_metadata(self):
        row=self.register();self.configure();self.guide(row['task_id'],next_action='Do not disclose synthetic-test-value')
        frames=self.transport('clarify');self.analyze()
        self.assertNotIn('synthetic-test-value',json.dumps(frames))
        self.assertIn('REDACTED_GATEWAY_KEY',json.dumps(frames))

    def test_existing_receipt_is_readable_after_the_key_is_removed(self):
        self.register();self.configure();frames=self.transport('recommend');self.analyze()
        (Path(self.temp.name)/'gateway.key').unlink()
        receipt=self.app.dispatch('followup_analyze',{'key':'analysis'})
        self.assertTrue(receipt['duplicate']);self.assertEqual(len(frames),1)

    def test_progressive_calls_keep_the_original_model_and_key_if_configuration_changes(self):
        self.register();self.configure();frames=self.transport('clarify',request_context='task')
        transport=self.app.followup.gateway.transport
        original=transport.side_effect;seen=[]
        def changing(payload,key):
            seen.append((payload['model'],key))
            response=original(payload,key)
            self.app.followup.gateway.configure_model({'model':'openai/other-model'})
            self.app.followup.gateway.configure_key({'key':'rotated-synthetic-value'})
            return response
        transport.side_effect=changing
        result=self.analyze()['result']
        self.assertEqual(seen,[('openai/gpt-4.1-mini','synthetic-test-value')]*2)
        self.assertEqual(result['model'],'openai/gpt-4.1-mini')

    def test_progressive_context_excludes_bodies_notes_paths_and_sessions_initially(self):
        row=self.register();self.configure();frames=self.transport('clarify',request_context='task')
        self.guide(row['task_id'],next_action='Choose next concrete step')
        run=self.analyze()
        first=json.dumps(frames[0])
        self.assertNotIn('PRIVATE_BODY_MUST_NOT_BE_SENT_INITIALLY',first)
        self.assertNotIn('agent-sessions',first);self.assertNotIn('source-session',first)
        self.assertEqual(frames[0]['context'],[])
        self.assertEqual(len(frames),2);self.assertEqual(len(frames[1]['context']),1)
        self.assertEqual(run['result']['usage'],{'model_calls':2,'context_reads':1,'input_bytes':run['result']['usage']['input_bytes'],'total_tokens':30})

    def test_only_known_candidate_and_sources_can_be_requested_and_never_tools_or_commands(self):
        self.register();self.configure()
        payload={'mode':'context','candidate_ids':['invented'],'reason':'Run shell','question':'','context_requests':[{'candidate_id':'invented','source':'shell'}]}
        self.app.followup.gateway.transport=lambda *args:{'choices':[{'message':{'content':json.dumps(payload)}}]}
        run=self.analyze()
        self.assertEqual(run['status'],'failed');self.native.assert_not_called()
        self.assertNotIn('Run shell',json.dumps(run))

    def test_linked_slack_context_is_bounded_to_existing_thread_and_no_posting(self):
        row=self.register();self.configure();frames=self.transport('clarify',request_context='slack')
        self.app.dispatch('intent',{'key':'thread','kind':'attach_slack','payload':{'url':PR,'thread':THREAD}})
        self.native.reset_mock();self.native.return_value={'success':True,'data':{'messages':[{'text':'Bounded excerpt','user':'U123','ts':'1793390000.000000'}]}}
        run=self.analyze()
        self.native.assert_called_once_with(['slack-cli','messages','thread','C123','1793390000.000000','--limit','10','--json'])
        self.assertEqual(run['result']['usage']['context_reads'],1)
        self.assertIn('Bounded excerpt',frames[1]['context'][0]['excerpt'])

    def test_failed_or_interrupted_analysis_is_not_replayed_on_schedule_or_restart(self):
        self.register();self.configure()
        self.app.followup.gateway.transport=mock.Mock(side_effect=RuntimeError('PRIVATE_PROVIDER_BODY'))
        run=self.analyze();self.assertEqual(run['status'],'failed')
        self.assertNotIn('PRIVATE_PROVIDER_BODY',json.dumps(run))
        self.now+=timedelta(days=1);self.app.scheduler.step()
        self.app.followup.gateway.transport.assert_called_once()
        self.app.dispatch('followup_analyze',{'key':'queued'})
        resumed=Application(self.temp.name,clock=lambda:self.now,run=self.native)
        self.assertEqual(resumed.dispatch('followup_runs',{'key':'queued'})['status'],'interrupted')
        with mock.patch.object(resumed.followup.gateway,'decide') as decide:
            resumed.scheduler.step();decide.assert_not_called()
        self.assertTrue(resumed.dispatch('followup_analyze',{'key':'queued'})['duplicate'])

    def test_missing_key_does_not_queue_or_spend_and_model_is_configurable(self):
        self.register()
        with self.assertRaises(ValueError):self.app.dispatch('followup_analyze',{'key':'missing'})
        self.assertIsNone(self.app.dispatch('followup_runs',{'key':'missing'}))
        self.app.dispatch('followup_model',{'model':'openai/gpt-4.1-mini'})
        self.assertEqual(self.read()['gateway']['model'],'openai/gpt-4.1-mini')
        with self.assertRaises(ValueError):self.app.dispatch('followup_model',{'model':'https://evil.example'})

    def test_changed_facts_invalidate_cached_ai_report_and_do_not_run_another_model(self):
        row=self.register();self.configure();frames=self.transport('recommend');self.analyze()
        self.guide(row['task_id'],next_action='Different action')
        self.assertNotIn('ai_report_key',self.read())
        self.assertEqual(len(frames),1)
        self.assertEqual(self.read()['candidates'][0]['action'],'Different action')

    def test_competitors_beyond_shortlist_cannot_be_reported_as_unique_winner(self):
        for number in range(42,67):self.register(number)
        self.configure();self.transport('recommend')
        result=self.analyze()['result']
        self.assertEqual(result['next']['mode'],'clarify');self.assertLessEqual(result['coverage']['shown'],24);self.assertEqual(result['coverage']['total'],25)

    def test_http_contract_uses_fixed_endpoint_bearer_auth_schema_and_bounded_response(self):
        response=mock.MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"choices": []}'
        opener=mock.Mock();opener.open.return_value=response
        with mock.patch('work_tracker.gateway.request.build_opener',return_value=opener):
            self.assertEqual(Gateway.post({'model':'openai/gpt-4.1-mini'},'synthetic-token'),{'choices':[]})
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://ai-gateway.vercel.sh/v1/chat/completions')
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],25)
        response.read.assert_called_once_with(256001)

    def test_metadata_invalid_values_are_atomic_and_do_not_touch_other_entities(self):
        row=self.register();before=self.app.dispatch('followup_task',{'task_id':row['task_id']})
        for fields in ({'next_action':'bad'+chr(27)+'text'},{'revisit_after':'not-a-date'},{'dependencies':'not-a-list'},{'waiting_on':None}):
            with self.assertRaises(ValueError):self.guide(row['task_id'],**fields)
        self.assertEqual(self.app.dispatch('followup_task',{'task_id':row['task_id']}),before)

    def test_unsupported_response_and_redirect_are_refused_without_echoing_secrets(self):
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,None,None,None,None)
        self.register();self.configure();self.app.followup.gateway.transport=lambda *args:{'choices':[]}
        self.assertEqual(self.analyze()['status'],'failed')

    def test_cli_reads_and_task_edits_use_rpc_without_local_datastore_fallback(self):
        row=self.register()
        with mock.patch.object(cli,'request',side_effect=lambda root,method,params=None:self.app.dispatch(method,params or {})),mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'follow-up']),0)
            self.assertIn('What should I do next?',output.getvalue())
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'follow-up','task',row['task_id'],'--next-action','Discuss the blocker']),0)
            self.assertEqual(self.read()['candidates'][0]['action'],'Discuss the blocker')


if __name__=='__main__':unittest.main()
