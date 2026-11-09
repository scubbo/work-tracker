from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from work_tracker import cli
from work_tracker.service import Application


PR='https://github.com/acme/service/pull/42'
NOTES='  Plan\n\n[bold]literal text\n\tKeep indentation  '


class GoalNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,11,9,18,tzinfo=timezone.utc)
        self.run=mock.Mock()
        self.app=Application(self.temp.name,clock=lambda:self.now,run=self.run)
        self.counter=0

    def day(self): return self.app.dispatch('my_day',{})

    def send(self,action,payload,key=None):
        self.counter+=1
        return self.app.dispatch('day_intent',{'key':key or str(self.counter),'action':action,
            'payload':{'day':self.day()['today'],**payload},'source':{'client':'test'}})

    def add(self,notes=NOTES):
        self.send('add',{'kind':'goal','text':'Daily outcome','notes':notes})
        return self.day()['goals'][-1]

    def edit(self,item,**payload):
        return self.send('edit',{'item_id':item['id'],'expected_version':item['version'],**payload})

    def test_multiline_notes_preserve_whitespace_and_survive_restart(self):
        item=self.add()
        self.assertEqual(item['notes'],NOTES)
        self.assertEqual(item['label'],'Daily outcome')
        self.assertEqual(item['target']['title'],'Daily outcome')
        self.app=Application(self.temp.name,clock=lambda:self.now,run=self.run)
        self.assertEqual(self.day()['goals'],[item])
        self.run.assert_not_called()

    def test_notes_only_edit_and_clear_preserve_target_label_prs_classification_and_completion(self):
        self.app.dispatch('intent',{'key':'register','kind':'register_pr','payload':{'url':PR,'project':'Alpha'}})
        self.send('add',{'kind':'goal','target_kind':'pr','url':PR,'text':'Ship outcome','notes':NOTES})
        before=self.day()['goals'][0];registry=deepcopy(self.app.storage.export_registry())
        self.edit(before,notes='Replacement\nNext step')
        after=self.day()['goals'][0]
        self.assertEqual({key:value for key,value in after.items() if key not in ('notes','version','updated_at')},
                         {key:value for key,value in before.items() if key not in ('notes','version','updated_at')})
        self.assertEqual(self.app.storage.export_registry(),registry)
        self.edit(after,notes='')
        self.assertEqual(self.day()['goals'][0]['notes'],'')
        self.run.assert_not_called()

    def test_label_or_classification_edit_does_not_clear_existing_notes(self):
        item=self.add()
        self.edit(item,text='Different daily label')
        item=self.day()['goals'][0]
        self.edit(item,project='Alpha',workstream='Flow',expected_workstream_id=item['target']['workstream_id'])
        self.assertEqual(self.day()['goals'][0]['notes'],NOTES)
        self.assertEqual(self.day()['goals'][0]['target']['title'],'Daily outcome')

    def test_invalid_notes_fail_atomically_before_task_creation_and_empty_notes_are_allowed(self):
        for invalid in (None,123,[],{},'x'*20001,'escape\x1b[31m','line\rreturn','hidden\u202e'):
            with self.subTest(value=type(invalid).__name__):
                with self.assertRaises(ValueError): self.add(invalid)
        self.assertEqual(self.day()['goals'],[])
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM my_day_intents').fetchone()[0],0)
        self.assertEqual(self.add('')['notes'],'')
        self.assertEqual(self.add('x'*20000)['notes'],'x'*20000)

    def test_stale_notes_edit_is_rejected_without_overwriting_newer_text(self):
        item=self.add()
        self.edit(item,notes='Other window')
        with self.assertRaises(ValueError): self.edit(item,notes='Stale window')
        self.assertEqual(self.day()['goals'][0]['notes'],'Other window')

    def test_identical_retry_returns_original_receipt_even_after_midnight(self):
        item=self.add()
        payload={'day':self.day()['today'],'item_id':item['id'],'expected_version':item['version'],'notes':'Saved once'}
        first=self.send('edit',payload,key='stable-notes')
        self.now+=timedelta(days=1)
        repeated=self.send('edit',payload,key='stable-notes')
        self.assertEqual(first['item_id'],repeated['item_id']);self.assertTrue(repeated['duplicate'])
        with self.assertRaises(ValueError): self.send('edit',payload,key='new-notes')

    def test_explicit_carry_copies_notes_without_changing_history_or_sharing_later_edits(self):
        original=self.add();old_day=self.day()['day']
        self.now+=timedelta(days=1)
        self.assertEqual(self.day()['goals'],[])
        candidate=self.day()['carry_candidates'][0]
        self.send('carry',{'item_id':candidate['id'],'expected_version':candidate['version']})
        carried=self.day()['goals'][0]
        self.assertEqual(carried['notes'],NOTES)
        self.edit(carried,notes='Today only')
        historical=self.app.dispatch('my_day',{'day':old_day})['goals'][0]
        self.assertEqual(historical,original)
        with self.assertRaises(ValueError): self.edit(original,notes='Rewrite yesterday')

    def test_readding_same_target_keeps_its_notes_and_reports_that_they_were_kept(self):
        item=self.add()
        result=self.send('add',{'kind':'goal','target_kind':'task','target_id':item['target_id'],'notes':'Do not overwrite'})
        self.assertEqual(len(self.day()['goals']),1)
        self.assertEqual(self.day()['goals'][0]['notes'],NOTES)
        self.assertIn('notes were kept',result['message'])

    def test_workstream_goals_support_notes_but_awareness_does_not(self):
        item=self.add()
        self.send('add',{'kind':'goal','target_kind':'workstream','target_id':item['target']['workstream_id'],'notes':NOTES})
        self.assertEqual(self.day()['goals'][1]['notes'],NOTES)
        with self.assertRaises(ValueError): self.send('add',{'kind':'awareness','text':'Watch','notes':NOTES})
        self.send('add',{'kind':'awareness','text':'Watch'})
        watch=self.day()['awareness'][0]
        with self.assertRaises(ValueError): self.edit(watch,notes=NOTES)

    def test_schema_six_migration_defaults_old_goals_to_empty_notes_and_preserves_other_data(self):
        item=self.add('')
        before=self.day();jobs=self.app.storage.jobs();catalog=self.app.dispatch('catalog',{})
        with self.app.storage._connection() as db:
            db.execute('ALTER TABLE my_day_items DROP COLUMN notes');db.execute('PRAGMA user_version=6');db.commit()
        self.app=Application(self.temp.name,clock=lambda:self.now,run=self.run)
        self.assertEqual(self.day(),before)
        self.assertEqual(self.app.storage.jobs(),jobs)
        self.assertEqual(self.app.dispatch('catalog',{}),catalog)
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
        self.edit(item,notes='After migration')
        self.assertEqual(self.day()['goals'][0]['notes'],'After migration')

    def test_cli_add_notes_only_edit_clear_and_multiline_display_use_rpc(self):
        def rpc(root,method,params=None): return self.app.dispatch(method,params or {})
        with mock.patch.object(cli,'request',side_effect=rpc),mock.patch('sys.stdout',new_callable=io.StringIO) as output,mock.patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'day','add','Outcome','--notes',NOTES,'--key','cli-add']),0)
            item=self.day()['goals'][0]
            self.assertEqual(item['notes'],NOTES)
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'day']),0)
            self.assertIn('    Notes:\n      Plan\n',output.getvalue())
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'day','edit',item['id'],'--notes','Replacement','--key','cli-edit']),0)
            self.assertEqual(self.day()['goals'][0]['notes'],'Replacement')
            self.assertEqual(cli.main(['--state-dir',self.temp.name,'day','edit',item['id'],'--notes','','--key','cli-clear']),0)
            self.assertEqual(self.day()['goals'][0]['notes'],'')
        self.run.assert_not_called()


if __name__=='__main__':unittest.main()
