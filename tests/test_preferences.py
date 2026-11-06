from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from work_tracker.preferences import DEFAULT_PR_SORT, PR_SORT_FIELDS
from work_tracker.service import Application
from work_tracker.storage import SQLiteStorage


PR = 'https://github.com/acme/service/pull/42'


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application(self.root)

    def test_missing_preference_preserves_the_default_url_order(self):
        value = self.app.dispatch('get_pr_sort', {})
        self.assertEqual(value, DEFAULT_PR_SORT)
        value['field'] = 'title'
        self.assertEqual(self.app.dispatch('get_pr_sort', {}), DEFAULT_PR_SORT)
        with self.assertRaises(ValueError):
            self.app.dispatch('get_pr_sort', {'field': 'title'})

    def test_sort_survives_application_and_storage_restart_for_every_column(self):
        for field in PR_SORT_FIELDS:
            for descending in (False, True):
                with self.subTest(field=field, descending=descending):
                    value = {'field': field, 'descending': descending}
                    self.assertEqual(self.app.dispatch('set_pr_sort', value), value)
                    reopened = Application(self.root)
                    self.assertEqual(reopened.dispatch('get_pr_sort', {}), value)
                    self.assertEqual(SQLiteStorage(self.root/'tracker.sqlite3').get_pr_sort(), value)

    def test_invalid_writes_are_rejected_atomically(self):
        saved = {'field': 'opened', 'descending': True}
        self.app.dispatch('set_pr_sort', saved)
        invalid = [{}, {'field': 'title'}, {'field': 'unknown', 'descending': False},
                   {'field': 'pr', 'descending': 1}, {'field': 'pr', 'descending': 'false'},
                   {'field': ['pr'], 'descending': False},
                   {'field': 'pr', 'descending': False, 'command': 'not allowed'}, []]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.app.dispatch('set_pr_sort', value)
                self.assertEqual(self.app.dispatch('get_pr_sort', {}), saved)

    def test_sort_does_not_change_pr_data_jobs_configuration_or_compatibility_export(self):
        self.app.dispatch('intent', {'key': 'fixture', 'kind': 'register_pr',
                                     'payload': {'url': PR, 'description': 'Keep curated metadata'}})
        before = deepcopy(self.app.storage.export_registry())
        config = deepcopy(self.app.config)
        jobs = self.app.storage.jobs()
        mirror = (self.root/'prs.json').read_bytes()
        for _ in range(2):
            self.app.dispatch('set_pr_sort', {'field': 'state-age', 'descending': True})
        self.assertEqual(self.app.storage.export_registry(), before)
        self.assertEqual(self.app.config, config)
        self.assertEqual(self.app.storage.jobs(), jobs)
        self.assertEqual((self.root/'prs.json').read_bytes(), mirror)
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT count(*) FROM settings WHERE key='tui_pr_sort'").fetchone()[0], 1)
            self.assertTrue(db.execute("SELECT 1 FROM settings WHERE key='legacy_imported'").fetchone())

    def test_preference_is_local_to_the_selected_application_state_directory(self):
        self.app.dispatch('set_pr_sort', {'field': 'title', 'descending': True})
        other = Application(self.root/'other-state')
        self.assertEqual(other.dispatch('get_pr_sort', {}), DEFAULT_PR_SORT)


if __name__ == '__main__':
    unittest.main()
