from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import work_activity as activity
from work_tracker.service import Application
from work_tracker.state_age import state_age
from work_tracker.storage import SQLiteStorage


PR = 'https://github.com/acme/service/pull/42'
T1 = '2026-11-04T10:00:00Z'
T2 = '2026-11-04T12:00:00Z'
T3 = '2026-11-04T15:00:00Z'


class StateAgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStorage(Path(self.tmp.name)/'tracker.sqlite3')
        self.store.apply_intent('register', 'register_pr', {'url': PR}, {})

    def observe(self, at, state='Awaiting review', lifecycle='open', **kwargs):
        with mock.patch('work_tracker.storage.now_iso', return_value=at):
            return self.store.observe(PR, {'state': lifecycle, 'updatedAt': at}, state, 'Reason', **kwargs)

    def row(self):
        return self.store.list_prs(state=None)[0]

    def test_first_observation_is_lower_bound_and_only_a_state_transition_resets_the_clock(self):
        self.assertEqual(state_age(self.row()), 'Unknown')
        self.observe(T1)
        self.assertIsNone(self.row()['state_changed_at'])
        self.assertEqual(self.row()['state_first_seen_at'], T1)
        self.observe(T2)
        self.assertEqual(self.row()['state_first_seen_at'], T1)
        self.assertEqual(state_age(self.row(), activity.parse_time(T2)), '≥2h 0m')
        self.observe(T3, 'Ready to Merge')
        self.assertEqual(self.row()['state_changed_at'], T3)
        self.observe('2026-11-04T15:30:00Z', 'Ready to Merge')
        self.assertEqual(self.row()['state_changed_at'], T3)
        self.assertEqual(state_age(self.row(), activity.parse_time('2026-11-04T16:00:00Z')), '1h 0m')

    def test_stale_observation_hides_age_without_resetting_confirmed_history(self):
        self.observe(T1)
        self.observe(T2, 'Changes requested')
        self.store.observation_error(PR, 'Unavailable')
        self.assertEqual(state_age(self.row()), 'Unknown')
        self.assertIsNone(self.row()['state_changed_at'])
        self.observe(T3, 'Changes requested')
        self.assertEqual(self.row()['state_changed_at'], T2)

    def test_old_refresh_and_archive_cannot_reopen_a_concurrently_closed_pr(self):
        self.observe(T1)
        version = self.row()['observation_version']
        self.observe(T2, lifecycle='closed')
        self.assertFalse(self.observe(T3, expected_version=version))
        self.store.observation_error(PR, 'Old error', expected_version=version)
        self.store.merge_archive([{'url': PR, 'state': 'open', 'title': 'Stale archive'}], [], [],
                                 expected_versions={PR: version})
        self.assertEqual(self.row()['github']['state'], 'closed')
        self.assertIsNone(self.row()['refresh_error'])

    def test_bad_missing_and_future_dates_are_unknown(self):
        now = activity.parse_time(T2)
        for value in (None, '', 'bad', '2026-11-04T10:00:00', T3):
            with self.subTest(value=value):
                self.assertEqual(state_age({'state_changed_at': value}, now), 'Unknown')
        self.assertEqual(state_age({'state_changed_at': '2026-11-01T10:00:00Z'}, now), '3d 2h')
        self.assertEqual(state_age({'state_changed_at': T2}, now), '<1m')

    def test_schema_two_upgrade_retains_records_without_inventing_state_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'legacy.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('''
                    CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT UNIQUE,closed INTEGER DEFAULT 0);
                    CREATE TABLE workstreams(id TEXT PRIMARY KEY,project_id TEXT,name TEXT,closed INTEGER DEFAULT 0,UNIQUE(project_id,name));
                    CREATE TABLE tasks(id TEXT PRIMARY KEY,workstream_id TEXT,title TEXT,closed INTEGER DEFAULT 0);
                    CREATE TABLE prs(url TEXT PRIMARY KEY,task_id TEXT,metadata TEXT,github TEXT DEFAULT '{}',
                        review_state TEXT DEFAULT 'Awaiting review',reason TEXT DEFAULT '',observed_at TEXT,refresh_error TEXT);
                    INSERT INTO projects(id,name) VALUES('p','Existing');
                    INSERT INTO workstreams(id,project_id,name) VALUES('w','p','Stream');
                    INSERT INTO tasks(id,workstream_id,title) VALUES('t','w','Existing task');
                    PRAGMA user_version=2;
                ''')
                db.execute('INSERT INTO prs(url,task_id,metadata,github,observed_at) VALUES(?,?,?,?,?)',
                           (PR, 't', json.dumps({'url': PR, 'description': 'Curated'}), '{"state":"open"}', T1))
            migrated = SQLiteStorage(path)
            value = migrated.list_prs()[0]
            self.assertEqual(value['description'], 'Curated')
            self.assertEqual(value['project_name'], 'Existing')
            self.assertIsNone(value['state_changed_at'])
            self.assertIsNone(value['state_first_seen_at'])
            with migrated._connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)


class FakeGitHub:
    def __init__(self):
        self.remote = {'html_url': PR, 'state': 'open', 'updated_at': T1, 'closed_at': None, 'merged_at': None}
        self.calls, self.comments = [], []
        self.fail = None

    def __call__(self, args):
        self.calls.append(list(args))
        method = args[args.index('--method')+1]
        if method == 'POST':
            self.comments.append(args[args.index('--raw-field')+1][5:])
        if self.fail == method:
            raise activity.CollectionError('Hidden private CLI error')
        if method == 'PATCH':
            self.remote.update(state='closed', updated_at=T2, closed_at=T2)
        return {'id': 1} if method == 'POST' else deepcopy(self.remote)


class ClosePRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.github = FakeGitHub()
        self.app = Application(self.root, run=self.github)
        self.app.dispatch('intent', {'key': 'register', 'kind': 'register_pr', 'payload': {'url': PR}})
        self.params = {'key': 'close-one', 'url': PR, 'comment': 'Superseded.\nThanks for the review.', 'confirmed': True}

    def close(self, **overrides):
        return self.app.dispatch('close_pr', {**self.params, **overrides})

    def test_confirmed_close_posts_literal_comment_and_changes_no_branch_or_task(self):
        text = '--delete-branch\n$(touch /tmp/not-executed) [bold]literal[/bold]'
        result = self.close(comment=text)
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('comment', result)
        self.assertEqual(self.github.comments, [text])
        self.assertEqual([args[3] for args in self.github.calls], ['GET', 'POST', 'PATCH'])
        self.assertEqual(self.github.calls[-1][-1], 'state=closed')
        self.assertEqual(self.app.storage.list_prs(), [])
        self.assertEqual(self.app.storage.list_prs(state=None)[0]['github']['state'], 'closed')
        with self.app.storage._connection() as db:
            self.assertEqual(db.execute('SELECT closed FROM tasks').fetchone()[0], 0)
        self.assertEqual(activity.read_json(self.root/'prs.json', {})[PR]['github']['state'], 'closed')

    def test_empty_comment_closes_without_posting(self):
        self.assertEqual(self.close(comment='   ')['status'], 'succeeded')
        self.assertEqual(self.github.comments, [])
        self.assertEqual(len(self.github.calls), 2)

    def test_same_key_never_replays_mutations_and_changed_payload_is_rejected(self):
        self.close()
        self.assertEqual(self.close()['status'], 'succeeded')
        self.assertEqual(len(self.github.calls), 3)
        with self.assertRaises(ValueError):
            self.close(comment='Changed')

    def test_missing_confirmation_untracked_pr_and_agent_intent_cannot_close(self):
        for params in ({'url': PR}, {**self.params, 'confirmed': False},
                       {**self.params, 'url': PR.replace('/42', '/99')}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.app.dispatch('close_pr', params)
        with self.assertRaises(ValueError):
            self.app.dispatch('intent', {'key': 'agent-close', 'kind': 'close_pr', 'payload': {'url': PR}})
        self.assertEqual(self.github.calls, [])

    def test_already_merged_pr_is_not_commented_or_closed_again(self):
        self.github.remote.update(state='closed', merged_at=T1, closed_at=T1)
        self.assertEqual(self.close()['status'], 'already_closed')
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.comments, [])
        self.assertEqual(self.app.storage.list_prs(state=None)[0]['github']['state'], 'merged')

    def test_preflight_failure_sends_no_write_and_a_fresh_confirmation_can_retry(self):
        self.github.fail = 'GET'
        self.assertEqual(self.close()['status'], 'failed')
        self.assertEqual(self.github.comments, [])
        self.github.fail = None
        self.assertEqual(self.close(key='new-confirmation')['status'], 'succeeded')

    def test_uncertain_comment_or_close_is_not_replayed_even_after_restart(self):
        self.github.fail = 'POST'
        result = self.close()
        self.assertEqual(result['status'], 'uncertain')
        self.assertNotIn('Hidden private CLI error', result['message'])
        self.assertEqual(self.close()['status'], 'uncertain')
        self.assertEqual(len(self.github.comments), 1)
        app = Application(self.root, run=self.github)
        self.assertEqual(app.dispatch('close_pr', self.params)['status'], 'uncertain')
        with self.assertRaises(ValueError):
            app.dispatch('close_pr', {**self.params, 'key': 'different-key'})
        self.assertEqual(len(self.github.comments), 1)

    def test_uncertain_request_is_available_for_reopening_the_same_dialog(self):
        self.github.fail = 'POST'
        self.close()
        pending = self.app.storage.list_prs()[0]['close_request']
        self.assertEqual(pending['key'], self.params['key'])
        self.assertEqual(pending['status'], 'uncertain')
        self.assertEqual(pending['comment'], self.params['comment'])

    def test_failed_patch_does_not_repeat_an_acknowledged_comment(self):
        self.github.fail = 'PATCH'
        self.assertEqual(self.close()['status'], 'uncertain')
        self.assertEqual(self.close()['status'], 'uncertain')
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(len(self.github.calls), 3)
        result = self.app.dispatch('close_result', {'key': self.params['key'], 'check_github': True})
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(self.github.calls[-1][3], 'GET')

    def test_interrupted_request_is_recovered_without_replay_and_can_be_checked_read_only(self):
        self.app.storage.begin_close('interrupted', PR, '')
        app = Application(self.root, run=self.github)
        self.assertEqual(app.dispatch('close_result', {'key': 'interrupted'})['status'], 'uncertain')
        self.assertEqual(self.github.calls, [])
        self.github.remote.update(state='closed', closed_at=T2, updated_at=T2)
        result = app.dispatch('close_result', {'key': 'interrupted', 'check_github': True})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([args[3] for args in self.github.calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
