from pathlib import Path
import tempfile
import unittest

from work_tracker.storage import SQLiteStorage


PR = 'https://github.com/acme/service/pull/42'


class EditPreconditionTests(unittest.TestCase):
    def test_stale_description_is_rejected_atomically_and_confirmed_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStorage(Path(tmp)/'db.sqlite3')
            store.apply_intent('register', 'register_pr', {'url': PR, 'description': 'Original'}, {})
            store.apply_intent('agent', 'update_pr', {'url': PR, 'description': 'Agent edit'}, {})
            with self.assertRaisesRegex(ValueError, 'Metadata changed'):
                store.apply_intent('ui', 'update_pr', {'url': PR, 'description': 'Stale UI edit',
                                                      'expected': {'description': 'Original'}}, {'client': 'tui'})
            self.assertEqual(store.export_registry()[PR]['description'], 'Agent edit')
            payload = {'url': PR, 'description': 'Resolved UI edit', 'expected': {'description': 'Agent edit'}}
            self.assertFalse(store.apply_intent('ui', 'update_pr', payload, {'client': 'tui'})['duplicate'])
            self.assertTrue(store.apply_intent('ui', 'update_pr', payload, {'client': 'tui'})['duplicate'])
            self.assertEqual(store.export_registry()[PR]['description'], 'Resolved UI edit')

    def test_classification_preconditions_cannot_overwrite_concurrent_agent_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStorage(Path(tmp)/'db.sqlite3')
            store.apply_intent('register', 'register_pr', {'url': PR, 'project': 'Platform', 'workstream': 'Access'}, {})
            task = store.list_prs()[0]['task_id']
            store.apply_intent('agent', 'update_pr', {'url': PR, 'project': 'Platform', 'workstream': 'Storage'}, {})
            with self.assertRaisesRegex(ValueError, 'Metadata changed'):
                store.apply_intent('ui', 'update_pr', {'url': PR, 'project': 'Other', 'workstream': 'New',
                                                      'expected': {'project_name': 'Platform', 'workstream_name': 'Access'}}, {})
            row = store.list_prs()[0]
            self.assertEqual((row['project_name'], row['workstream_name'], row['task_id']), ('Platform', 'Storage', task))
            with self.assertRaises(ValueError):
                store.apply_intent('facts', 'update_pr', {'url': PR, 'description': 'Bad', 'expected': {'github': {}}}, {})


if __name__ == '__main__':
    unittest.main()
