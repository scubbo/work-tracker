from pathlib import Path
import tempfile
import unittest

from work_tracker.service import Application
from work_tracker.storage import SQLiteStorage


class MetadataCatalogTests(unittest.TestCase):
    def test_catalog_includes_empty_and_closed_hierarchy_without_enrolling_prs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStorage(Path(tmp)/'db.sqlite3')
            with store._transaction() as db:
                stream = store._workstream(db, 'Historical', 'Research')
                db.execute('UPDATE workstreams SET closed=1 WHERE id=?', (stream,))
                db.execute("UPDATE projects SET closed=1 WHERE name='Historical'")
                db.execute("INSERT INTO projects(id,name) VALUES('empty-project','Empty project')")
            catalog = store.catalog()
            projects = {p['name']: p for p in catalog['projects']}
            self.assertIn('Unclassified', projects)
            self.assertEqual(projects['Empty project']['workstreams'], [])
            self.assertTrue(projects['Historical']['closed'])
            self.assertEqual(projects['Historical']['workstreams'][0]['name'], 'Research')
            self.assertTrue(projects['Historical']['workstreams'][0]['closed'])
            self.assertEqual(store.list_prs(state=None), [])
            self.assertEqual(catalog, store.catalog())

    def test_application_catalog_is_read_only_and_does_not_query_external_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            def no_external_calls(args):
                raise AssertionError('Catalog lookup must remain local')
            app = Application(Path(tmp), run=no_external_calls)
            catalog = app.dispatch('catalog', {})
            self.assertEqual(catalog['projects'][0]['name'], 'Unclassified')
            self.assertEqual(app.storage.export_registry(), {})
            with self.assertRaises(ValueError):
                app.dispatch('catalog', {'create': 'Not allowed'})
            self.assertEqual(catalog, app.dispatch('catalog', {}))


if __name__ == '__main__':
    unittest.main()
