from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io,json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from work_tracker import cli
from work_tracker.service import Application
from work_tracker.storage import SQLiteStorage


PR='https://github.com/acme/service/pull/42'


class MyDayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,11,6,18,tzinfo=timezone.utc)
        self.run=mock.Mock()
        self.app=Application(self.temp.name,clock=lambda:self.now,run=self.run)
        self.counter=0

    def day(self,day=None):
        return self.app.dispatch('my_day',{'day':day} if day else {})

    def edit(self,action,payload,key=None):
        self.counter+=1
        return self.app.dispatch('day_intent',{'key':key or str(self.counter),'action':action,
            'payload':{'day':self.day()['today'],**payload},'source':{'client':'test'}})

    def add(self,label='Outcome',kind='goal',key=None):
        return self.edit('add',{'kind':kind,'text':label},key)

    def change(self,action,item,**payload):
        return self.edit(action,{'item_id':item['id'],'expected_version':item['version'],**payload})

    def test_quick_add_is_a_task_in_default_hierarchy_and_done_only_affects_the_day_item(self):
        result=self.add('Finish a small outcome')
        item=self.day()['goals'][0]
        self.assertEqual(result['item_id'],item['id'])
        self.assertEqual(item['target']['project_name'],'Unclassified')
        self.assertEqual(item['target']['workstream_name'],'Unclassified')
        self.assertEqual(item['target']['pr_count'],0)
        self.change('set_done',item,done=True)
        done=self.day()['goals'][0]
        self.assertTrue(done['done'])
        self.assertFalse(done['target']['closed'])
        self.assertEqual(self.day()['done_count'],1)
        self.assertEqual(self.app.storage.list_prs(state=None),[])
        self.run.assert_not_called()

    def test_task_workstream_and_pr_linking_preserve_target_identity_without_duplicate_pr_goals(self):
        self.app.dispatch('intent',{'key':'pr','kind':'register_pr','payload':{'url':PR,'project':'Alpha','workstream':'Flow'}})
        before=deepcopy(self.app.storage.export_registry())
        row=self.app.dispatch('get_pr',{'url':PR})
        first=self.edit('add',{'kind':'goal','target_kind':'pr','url':PR})
        second=self.edit('add',{'kind':'goal','target_kind':'task','target_id':row['task_id']})
        self.assertEqual(first['item_id'],second['item_id'])
        self.assertEqual(len(self.day()['goals']),1)
        self.assertEqual(self.day()['goals'][0]['target']['prs'][0]['url'],PR)
        targets=self.app.dispatch('day_targets',{'kind':'workstream','search':'Alpha'})['items']
        self.edit('add',{'kind':'goal','target_kind':'workstream','target_id':targets[0]['id'],'text':'Workstream outcome'})
        self.assertEqual(len(self.day()['goals']),2)
        self.assertEqual(self.app.storage.export_registry(),before)
        self.run.assert_not_called()

    def test_awareness_persists_until_dismissed_and_remains_in_previous_day_history(self):
        self.add('Keep an eye on rollout',kind='awareness')
        previous=self.day()['day']
        self.now+=timedelta(days=2)
        item=self.day()['awareness'][0]
        self.assertEqual(self.day()['goals'],[])
        self.change('dismiss',item)
        self.assertEqual(self.day()['awareness'],[])
        self.assertEqual(len(self.day(previous)['awareness']),1)
        with self.assertRaises(ValueError): self.change('set_done',item,done=True)

    def test_carry_is_explicit_preserves_previous_day_and_reuses_the_same_task(self):
        self.add()
        old_day=self.day()['day']
        original=self.day()['goals'][0]
        self.now+=timedelta(days=1)
        self.assertEqual(self.day()['goals'],[])
        candidate=self.day()['carry_candidates'][0]
        self.change('carry',candidate)
        current=self.day()['goals'][0]
        self.assertNotEqual(current['id'],original['id'])
        self.assertEqual(current['target_id'],original['target_id'])
        self.assertEqual(current['carried_from'],original['id'])
        self.assertFalse(self.day(old_day)['goals'][0]['done'])
        self.assertEqual(self.day()['carry_candidates'],[])
        self.change('set_done',current,done=True)
        self.now+=timedelta(days=1)
        self.assertEqual(self.day()['carry_candidates'],[])

    def test_latest_prior_day_offers_unfinished_goals_after_a_gap_without_copying(self):
        self.add()
        self.now+=timedelta(days=4)
        self.assertEqual(len(self.day()['carry_candidates']),1)
        self.assertEqual(self.day()['goals'],[])
        self.assertFalse(self.day('2026-11-07')['editable'])

    def test_retries_are_idempotent_and_changed_payload_reuse_is_rejected(self):
        first=self.add('One Task',key='retry')
        second=self.add('One Task',key='retry')
        self.assertEqual(first['item_id'],second['item_id'])
        self.assertTrue(second['duplicate'])
        with self.assertRaises(ValueError): self.add('Different Task',key='retry')
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

    def test_exact_retry_after_midnight_returns_original_receipt_but_new_old_day_writes_fail(self):
        original_day=self.day()['day']
        self.add('Before midnight',key='midnight')
        self.now+=timedelta(days=1)
        replay=self.edit('add',{'day':original_day,'kind':'goal','text':'Before midnight'},key='midnight')
        self.assertTrue(replay['duplicate'])
        with self.assertRaises(ValueError): self.edit('add',{'day':original_day,'kind':'goal','text':'New old entry'})
        self.assertEqual(self.day()['goals'],[])

    def test_conflicts_and_stale_historical_edits_do_not_overwrite(self):
        self.add()
        original=self.day()['goals'][0]
        self.change('edit',original,text='Revised day label')
        with self.assertRaises(ValueError): self.change('set_done',original,done=True)
        current=self.day()['goals'][0]
        self.assertEqual(current['label'],'Revised day label')
        self.assertEqual(current['target']['title'],'Outcome')
        self.now+=timedelta(days=1)
        with self.assertRaises(ValueError): self.change('edit',current,text='Rewrite history')

    def test_removal_and_restore_do_not_delete_a_task_or_resurrect_old_carry_candidates(self):
        self.add()
        item=self.day()['goals'][0]
        self.change('remove',item)
        self.assertEqual(self.day()['goals'],[])
        self.edit('add',{'kind':'goal','target_kind':'task','target_id':item['target_id']})
        self.assertEqual(self.day()['goals'][0]['id'],item['id'])
        self.change('remove',self.day()['goals'][0])
        self.now+=timedelta(days=1)
        self.assertEqual(self.day()['carry_candidates'],[])
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

    def test_validation_rejects_unsupported_actions_fields_targets_and_missing_preconditions(self):
        for action,payload in [('merge_pr',{}),('add',{'kind':'goal','text':'x','shell':'anything'}),
                               ('add',{'kind':'goal','target_kind':'task','target_id':'missing'}),
                               ('add',{'kind':'goal','target_kind':'pr','url':PR}),
                               ('add',{'kind':'goal','text':'\x1bcontrol'})]:
            with self.subTest(action=action,payload=payload),self.assertRaises(ValueError): self.edit(action,payload)
        self.add()
        item=self.day()['goals'][0]
        with self.assertRaises(ValueError): self.edit('set_done',{'item_id':item['id'],'done':True})
        with self.assertRaises(ValueError): self.change('set_done',item,done='true')
        with self.assertRaises(ValueError): self.app.dispatch('my_day',{'day':'invalid'})
        with self.assertRaises(ValueError): self.app.dispatch('day_targets',{'limit':True})
        self.run.assert_not_called()

    def test_schema_five_upgrade_and_restart_preserve_plan_stats_preferences_and_pr_data(self):
        self.app.dispatch('intent',{'key':'pr','kind':'register_pr','payload':{'url':PR}})
        self.app.storage.set_pr_sort({'field':'opened','descending':True})
        before=self.app.storage.export_registry()
        with self.app.storage._transaction() as db:
            db.execute('DROP TABLE my_day_intents');db.execute('DROP TABLE my_day_items');db.execute('PRAGMA user_version=5')
        store=SQLiteStorage(Path(self.temp.name)/'tracker.sqlite3')
        self.assertEqual(store.export_registry(),before)
        self.assertEqual(store.get_pr_sort(),{'field':'opened','descending':True})
        self.assertGreater(store.statistics({'days':1},self.now)['total'],0)
        self.add('Retain this plan')
        app=Application(self.temp.name,clock=lambda:self.now,run=self.run)
        self.assertEqual(app.dispatch('my_day',{})['goals'][0]['label'],'Retain this plan')

    def test_cli_add_done_history_and_targets_use_only_the_supported_api(self):
        def rpc(root,method,params=None): return self.app.dispatch(method,params or {})
        with mock.patch('work_tracker.cli.request',side_effect=rpc),mock.patch('sys.stdout',new_callable=io.StringIO) as output,mock.patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(cli.main(['day','add','CLI outcome','--key','cli-add']),0)
            result=json.loads(output.getvalue());output.seek(0);output.truncate()
            self.assertEqual(cli.main(['day','done',result['item_id'],'--key','cli-done']),0)
            self.assertTrue(self.day()['goals'][0]['done'])
            output.seek(0);output.truncate()
            self.assertEqual(cli.main(['day','--json']),0)
            self.assertEqual(json.loads(output.getvalue())['done_count'],1)
            output.seek(0);output.truncate()
            self.assertEqual(cli.main(['day','targets','--search','CLI']),0)
            self.assertEqual(json.loads(output.getvalue())['total'],1)
        self.run.assert_not_called()


if __name__=='__main__': unittest.main()
