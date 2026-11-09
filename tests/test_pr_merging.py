from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import work_activity as activity
from work_tracker.service import Application
from work_tracker.storage import SQLiteStorage


PR = 'https://github.com/acme/service/pull/42'
HEAD = 'a'*40
OTHER = 'b'*40


class MergeGitHub:
    def __init__(self):
        self.remote = {'html_url': PR, 'title': '[bold]Literal title', 'state': 'open', 'draft': False,
                       'head': {'sha': HEAD}, 'mergeable': True, 'mergeable_state': 'clean',
                       'merged_at': None, 'closed_at': None, 'updated_at': None}
        self.repo = {'full_name': 'acme/service', 'allow_merge_commit': True,
                     'allow_squash_merge': True, 'allow_rebase_merge': True}
        self.calls = []
        self.lose_response = False
        self.fail_get = False
        self.fail_after_merge = False
        self.race_head = False

    def __call__(self, args):
        self.calls.append(list(args))
        method, endpoint = args[3:5]
        if method == 'GET':
            if self.fail_get or self.fail_after_merge and self.remote['merged_at']:
                raise activity.CollectionError('Private authentication diagnostics')
            return deepcopy(self.repo if endpoint == 'repos/acme/service' else self.remote)
        assert method == 'PUT' and endpoint == 'repos/acme/service/pulls/42/merge', args
        if self.race_head:
            self.remote['head']['sha'] = OTHER
        if 'sha='+self.remote['head']['sha'] not in args:
            return {'merged': False, 'message': 'Head changed'}
        self.remote.update(state='closed', merged_at='2026-11-05T18:00:00Z', closed_at='2026-11-05T18:00:00Z')
        if self.lose_response:
            raise activity.CollectionError('Private authentication diagnostics')
        return {'merged': True, 'sha': 'c'*40, 'message': 'Pull Request successfully merged'}


class MergePRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = MergeGitHub()
        self.app = Application(self.root, run=self.github)
        self.app.dispatch('intent', {'key': 'register', 'kind': 'register_pr',
                                    'payload': {'url': PR, 'description': 'Keep this metadata'}})
        self.params = {'url': PR, 'key': 'merge-one', 'head_sha': HEAD, 'method': 'squash', 'confirmed': True}

    def merge(self, **changes):
        return self.app.dispatch('merge_pr', {**self.params, **changes})

    def puts(self):
        return [args for args in self.github.calls if args[3] == 'PUT']

    def test_preview_is_read_only_and_returns_fresh_head_and_allowed_methods(self):
        self.github.repo['allow_merge_commit'] = False
        value = self.app.dispatch('merge_preview', {'url': PR})
        self.assertEqual(value['head_sha'], HEAD)
        self.assertEqual(value['methods'], ['squash', 'rebase'])
        self.assertEqual(self.puts(), [])
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pr_merge_requests').fetchone()[0], 0)
        with self.assertRaises(ValueError): self.app.dispatch('merge_preview', {'url': PR.replace('/42', '/99')})

    def test_confirmation_exact_head_and_safe_method_are_required_and_intents_cannot_merge(self):
        for params in ({'url': PR}, {**self.params, 'confirmed': False}, {**self.params, 'head_sha': '--admin'},
                       {**self.params, 'method': '--delete-branch'}, {**self.params, 'auto': True},
                       {**self.params, 'url': PR.replace('/42', '/99')}):
            with self.subTest(params=params), self.assertRaises(ValueError): self.app.dispatch('merge_pr', params)
        with self.assertRaises(ValueError):
            self.app.dispatch('intent', {'key': 'agent-merge', 'kind': 'merge_pr', 'payload': {'url': PR}})
        self.assertEqual(self.github.calls, [])

    def test_confirmed_merge_uses_cas_and_changes_no_branch_task_or_curated_metadata(self):
        result = self.merge()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.puts(), [['gh', 'api', '--method', 'PUT', 'repos/acme/service/pulls/42/merge',
                                      '--raw-field', 'sha='+HEAD, '--raw-field', 'merge_method=squash']])
        value = self.app.storage.list_prs(state=None)[0]
        self.assertEqual(value['github']['state'], 'merged')
        self.assertEqual(value['description'], 'Keep this metadata')
        self.assertEqual(self.app.storage.list_prs(), [])
        with self.app.storage._connection(read=True) as db:
            self.assertEqual(db.execute('SELECT closed FROM tasks').fetchone()[0], 0)
        self.assertEqual(activity.read_json(self.root/'prs.json', {})[PR]['github']['state'], 'merged')

    def test_repeated_request_never_replays_put_and_changed_payload_is_rejected(self):
        self.merge()
        count = len(self.github.calls)
        self.assertEqual(self.merge()['status'], 'succeeded')
        self.assertEqual(len(self.github.calls), count)
        with self.assertRaises(ValueError): self.merge(method='rebase')
        with self.assertRaises(ValueError): self.merge(head_sha=OTHER)

    def test_changed_head_draft_unknown_or_blocked_readiness_and_disallowed_method_send_no_put(self):
        for change in ({'head': {'sha': OTHER}}, {'draft': True}, {'mergeable': None},
                       {'mergeable_state': 'blocked'}, {'mergeable_state': 'behind'}, {'mergeable': False}):
            with self.subTest(change=change):
                original = deepcopy(self.github.remote)
                self.github.remote.update(change)
                result = self.merge(key='case-'+str(len(self.github.calls)))
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(self.puts(), [])
                self.github.remote = original
        self.github.repo['allow_squash_merge'] = False
        self.assertEqual(self.merge(key='disallowed')['status'], 'failed')
        self.assertEqual(self.puts(), [])

    def test_head_change_during_put_is_guarded_by_github_sha_precondition(self):
        self.github.race_head = True
        self.assertEqual(self.merge()['status'], 'failed')
        self.assertIsNone(self.github.remote['merged_at'])
        self.assertEqual(len(self.puts()), 1)

    def test_already_merged_or_closed_pr_never_sends_a_merge(self):
        self.github.remote.update(state='closed')
        self.assertEqual(self.merge()['status'], 'failed')
        self.github.remote['merged_at'] = '2026-11-05T18:00:00Z'
        self.assertEqual(self.merge(key='already-merged')['status'], 'already_merged')
        self.assertEqual(self.puts(), [])

    def test_lost_merge_response_is_not_replayed_and_read_only_check_resolves_it(self):
        self.github.lose_response = True
        result = self.merge()
        self.assertEqual(result['status'], 'uncertain')
        self.assertNotIn('Private authentication', result['message'])
        self.assertEqual(self.merge()['status'], 'uncertain')
        app = Application(self.root, run=self.github)
        self.assertEqual(app.dispatch('merge_pr', self.params)['status'], 'uncertain')
        with self.assertRaises(ValueError): app.dispatch('merge_pr', {**self.params, 'key': 'new-key'})
        checked = app.dispatch('merge_result', {'key': self.params['key'], 'check_github': True})
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(len(self.puts()), 1)

    def test_follow_up_read_failure_does_not_undo_a_definitive_merge_receipt(self):
        self.github.fail_after_merge = True
        self.assertEqual(self.merge()['status'], 'succeeded')
        self.assertEqual(self.app.storage.list_prs(state=None)[0]['github']['state'], 'merged')
        self.assertEqual(len(self.puts()), 1)

    def test_preflight_failure_is_safe_to_retry_only_with_new_confirmation(self):
        self.github.fail_get = True
        value = self.merge()
        self.assertEqual(value['status'], 'failed')
        self.assertNotIn('Private authentication', value['message'])
        self.assertEqual(self.puts(), [])
        self.github.fail_get = False
        self.assertEqual(self.merge(key='fresh-confirmation')['status'], 'succeeded')

    def test_close_and_merge_requests_exclude_each_other(self):
        self.app.storage.begin_merge('pending-merge', PR, HEAD, 'squash')
        with self.assertRaises(ValueError): self.app.storage.begin_close('close', PR, '')
        self.app.storage.finish_merge('pending-merge', 'failed', 'No write', 'preflight')
        self.app.storage.begin_close('pending-close', PR, '')
        with self.assertRaises(ValueError): self.merge()
        self.assertEqual(self.github.calls, [])

    def test_interrupted_merge_is_recovered_and_exposed_for_read_only_resume(self):
        self.app.storage.begin_merge('interrupted', PR, HEAD, 'squash')
        app = Application(self.root, run=self.github)
        value = app.dispatch('get_pr', {'url': PR})['merge_request']
        self.assertEqual(value['status'], 'uncertain')
        self.assertEqual(value['head_sha'], HEAD)
        with self.assertRaises(ValueError): app.dispatch('merge_pr', self.params)
        self.assertEqual(self.github.calls, [])

    def test_schema_three_upgrade_preserves_existing_data_and_pending_closes(self):
        self.app.storage.set_pr_sort({'field': 'opened', 'descending': True})
        self.app.storage.begin_close('existing-close', PR, 'Keep this comment')
        before = self.app.storage.export_registry()
        with self.app.storage._transaction() as db:
            db.execute('DROP TABLE pr_merge_requests')
            db.execute('PRAGMA user_version=3')
        migrated = SQLiteStorage(self.root/'tracker.sqlite3')
        self.assertEqual(migrated.export_registry(), before)
        self.assertEqual(migrated.close_request('existing-close')['comment'], 'Keep this comment')
        self.assertEqual(migrated.get_pr_sort(), {'field': 'opened', 'descending': True})
        with migrated._connection(read=True) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)


if __name__ == '__main__':
    unittest.main()
