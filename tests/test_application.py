from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import work_activity as activity
from work_tracker.github import review_status
from work_tracker.scheduler import Scheduler, next_daily
from work_tracker.service import Application, DEFAULT_CONFIG
from work_tracker.storage import SQLiteStorage


PR = 'https://github.com/acme/service/pull/42'
THREAD = 'https://acme.slack.com/archives/C123/p1793390000000000'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStorage(self.root/'tracker.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def intent(self, kind='register_pr', payload=None, key='one'):
        return self.store.apply_intent(key, kind, payload or {'url': PR}, {'client': 'test'})

    def test_import_preserves_metadata_without_guessing_hierarchy(self):
        record = {**activity.new_pr(PR), 'project': 'Existing label', 'description': 'Curated reason',
                  'slack-threads': [{'url': THREAD, 'description': 'Discussion'}],
                  'source-session': {'id': 'ses_known', 'directory': '/old/tree', 'method': 'manual'}}
        path = self.root/'prs.json'
        activity.write_json(path, {PR: record})
        before = path.read_bytes()
        self.assertEqual(self.store.import_legacy(path), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list((self.root/'backups').glob('*.json'))), 1)
        value = self.store.list_prs()[0]
        self.assertEqual(value['project_name'], 'Unclassified')
        self.assertEqual(value['project'], 'Existing label')
        self.assertEqual(value['description'], 'Curated reason')
        self.assertEqual(value['source-session'], record['source-session'])
        self.assertEqual(value['agent-sessions'][0]['sessionId'], 'ses_known')
        self.assertEqual(self.store.import_legacy(path), 0)

    def test_idempotent_intents_keep_task_identity_and_reject_key_reuse(self):
        self.assertFalse(self.intent()['duplicate'])
        task = self.store.list_prs()[0]['task_id']
        self.assertTrue(self.intent()['duplicate'])
        self.intent(key='two')
        self.assertEqual(self.store.list_prs()[0]['task_id'], task)
        with self.assertRaises(ValueError):
            self.intent(payload={'url': PR, 'description': 'different'})

    def test_facts_are_not_writable_and_failed_intents_are_atomic(self):
        for field in ('github', 'state', 'review_state', 'mergedAt', 'ci', 'shell'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.intent(payload={'url': PR, field: 'pretend'})
        with self.assertRaises(ValueError):
            self.intent(payload={'url': PR, 'project': 42})
        self.assertEqual(self.store.list_prs(), [])

    def test_multiple_agent_sessions_and_manual_slack_links(self):
        self.intent()
        for index, tool in enumerate(('fx', 'claude')):
            self.intent('attach_session', {'url': PR, 'session': {
                'tool': tool, 'path': '/work/tree', 'sessionId': f'known-{index}', 'machine': 'laptop'}}, str(index))
        self.intent('attach_slack', {'url': PR, 'thread': THREAD, 'description': 'Discussion'}, 'thread1')
        self.intent('attach_slack', {'url': PR, 'thread': THREAD}, 'thread2')
        value = self.store.list_prs()[0]
        self.assertEqual({s['tool'] for s in value['agent-sessions']}, {'fx', 'claude'})
        self.assertEqual(value['slack-threads'], [{'url': THREAD, 'description': 'Discussion'}])
        with self.assertRaises(ValueError):
            self.intent('attach_session', {'url': PR, 'session': {'tool': 'fx', 'sessionId': 'missing-path'}}, 'bad')

    def test_classification_and_observations_preserve_user_intent(self):
        self.intent(payload={'url': PR, 'description': 'Why', 'project': 'Platform', 'workstream': 'Access'})
        self.store.observe(PR, {'state': 'open', 'title': 'New title'}, 'Ready to Merge', 'Confirmed')
        self.store.merge_archive([{'url': PR, 'state': 'open', 'title': 'Archived title'}], [], [])
        value = self.store.list_prs(project='Platform', workstream='Access')[0]
        self.assertEqual(value['description'], 'Why')
        self.assertEqual(value['project_name'], 'Platform')
        self.store.observation_error(PR, 'Refresh failed')
        self.assertEqual(self.store.list_prs()[0]['review_state'], 'Blocked')
        self.assertIsNotNone(value['observed_at'])

    def test_list_uses_one_snapshot_when_pr_is_added_during_read(self):
        self.intent()
        original = self.store._registry
        def concurrent_insert(db):
            result = original(db)
            self.intent(payload={'url': PR.replace('/42', '/43')}, key='concurrent-pr')
            return result
        with mock.patch.object(self.store, '_registry', side_effect=concurrent_insert):
            values = self.store.list_prs()
        self.assertEqual(len(values), 1)
        self.assertEqual(len(self.store.list_prs()), 2)

    def test_concurrent_links_are_not_lost(self):
        self.intent()
        def link(index):
            return self.intent('attach_session', {'url': PR, 'session': {
                'tool': 'test', 'path': '/work', 'sessionId': str(index), 'machine': 'laptop'}}, f'link-{index}')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(link, range(20)))
        self.assertEqual(len(self.store.list_prs()[0]['agent-sessions']), 20)

    def test_observation_never_enrolls_an_untracked_pr(self):
        self.store.observe(PR, {'state': 'open'}, 'Ready to Merge', 'External observation')
        self.store.observation_error(PR, 'Unavailable')
        self.assertEqual(self.store.list_prs(), [])

    def test_accidental_backfill_is_quarantined_without_losing_facts_or_enrollments(self):
        auto = PR.replace('/42', '/43')
        explicit = PR.replace('/42', '/44')
        self.intent()
        self.intent(payload={'url': explicit}, key='agent-submitted')
        with self.store._transaction() as db:
            self.store._ensure_pr(db, auto, {**activity.new_pr(auto), 'github': {'title': 'Observed only', 'state': 'open'}})
        self.store.observe(auto, {'title': 'Observed only', 'state': 'open'}, 'Blocked', 'Observed')
        previous = next(value for value in self.store.list_prs() if value['url'] == auto)
        result = self.store.quarantine_backfill({PR})
        self.assertEqual(result['moved'], 1)
        self.assertEqual({value['url'] for value in self.store.list_prs()}, {PR, explicit})
        with self.store._connection() as db:
            cached = json.loads(db.execute('SELECT snapshot FROM untracked_cache WHERE url=?', (auto,)).fetchone()[0])
        self.assertEqual(json.loads(cached['pr']['github']), previous['github'])
        self.intent(payload={'url': auto}, key='now-explicitly-ingested')
        restored = next(value for value in self.store.list_prs() if value['url'] == auto)
        self.assertEqual(restored['task_id'], previous['task_id'])
        self.assertEqual(restored['github'], previous['github'])
        self.assertEqual(self.store.quarantine_backfill({PR})['moved'], 0)

    def test_database_is_private_and_unknown_schema_is_not_overwritten(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        with sqlite3.connect(self.store.path) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            SQLiteStorage(self.store.path)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.detail = {'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
                       'reviewDecision': 'APPROVED', 'isDraft': False, 'statusCheckRollup': []}

    def review(self, state, kind='User', login='reviewer', index=1):
        return {'id': index, 'state': state, 'user': {'type': kind, 'login': login}, 'submitted_at': str(index)}

    def test_automation_cannot_satisfy_human_review(self):
        for reviews in ([self.review('APPROVED', 'Bot')], [self.review('CHANGES_REQUESTED', 'Bot')],
                        [self.review('COMMENTED')], [self.review('APPROVED', login='ci[bot]')]):
            with self.subTest(reviews=reviews):
                self.assertEqual(review_status(self.detail, reviews)[0], 'Awaiting review')
        self.assertEqual(review_status(self.detail, [self.review('APPROVED')], ['reviewer'])[0], 'Awaiting review')

    def test_latest_human_opinion_and_dismissal(self):
        changes = self.review('CHANGES_REQUESTED')
        approval = self.review('APPROVED', index=2)
        self.assertEqual(review_status(self.detail, [changes])[0], 'Changes requested')
        self.assertEqual(review_status(self.detail, [changes, approval])[0], 'Ready to Merge')
        dismissed = self.review('DISMISSED', index=3)
        self.assertEqual(review_status(self.detail, [changes, approval, dismissed])[0], 'Awaiting review')

    def test_ci_codeowners_draft_and_unknown_readiness_block(self):
        variants = [{'reviewDecision': 'REVIEW_REQUIRED'}, {'isDraft': True},
                    {'mergeStateStatus': 'UNKNOWN'}, {'mergeable': 'CONFLICTING'},
                    {'statusCheckRollup': [{'state': 'FAILURE'}]},
                    {'statusCheckRollup': [{'status': 'IN_PROGRESS', 'conclusion': None}]}]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertEqual(review_status({**self.detail, **variant}, [self.review('APPROVED')])[0], 'Blocked')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStorage(Path(self.temp.name)/'db.sqlite3')
        self.now = datetime(2026, 11, 3, 9, 0).astimezone()
        self.calls = []
        self.callbacks = {name: self.callback(name) for name in ('archive', 'refresh_prs')}
        self.scheduler = Scheduler(self.store, self.callbacks, dict(DEFAULT_CONFIG), clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def callback(self, name):
        def run(params):
            self.calls.append((name, params))
            return {'ok': True}
        return run

    def test_daily_clock_catchup_and_no_duplicate_slot(self):
        self.scheduler.step()
        self.assertNotIn(('archive', {}), self.calls)
        self.now += timedelta(hours=1)
        self.scheduler.step()
        self.scheduler.step()
        self.assertEqual(self.calls.count(('archive', {})), 1)
        self.now += timedelta(days=3, hours=2)
        self.scheduler.step()
        self.assertEqual(self.calls.count(('archive', {})), 2)

    def test_manual_requests_are_coalesced_and_persistent(self):
        first = self.scheduler.queue('refresh_prs')
        second = self.scheduler.queue('refresh_prs')
        self.assertEqual(first['wait_for'], second['wait_for'])
        self.scheduler.step()
        job = next(j for j in self.store.jobs() if j['name'] == 'refresh_prs')
        self.assertEqual(job['completed_seq'], first['wait_for'])
        self.assertEqual(self.calls.count(('refresh_prs', {})), 1)

    def test_failure_retries_and_interrupted_job_recovers(self):
        self.scheduler.callbacks['refresh_prs'] = lambda params: {'ok': False, 'error': 'Unavailable'}
        self.scheduler.step()
        job = next(j for j in self.store.jobs() if j['name'] == 'refresh_prs')
        self.assertEqual(job['attempts'], 1)
        self.assertEqual(activity.parse_time(job['next_run']), self.now.astimezone(timezone.utc) + timedelta(seconds=60))
        job['status'] = 'running'
        self.store.save_job('refresh_prs', {k: v for k, v in job.items() if k != 'name'})
        recovered = Scheduler(self.store, {name: self.callback(name) for name in ('archive', 'refresh_prs')},
                              dict(DEFAULT_CONFIG), clock=lambda: self.now)
        recovered.step()
        self.assertIsNone(next(j for j in self.store.jobs() if j['name'] == 'refresh_prs')['last_error'])

    @unittest.skipUnless(hasattr(time, 'tzset'), 'POSIX local timezone support required')
    def test_timezone_change_recalculates_local_ten_oclock(self):
        original = os.environ.get('TZ')
        try:
            self.now = datetime(2026, 11, 3, 14, tzinfo=timezone.utc)
            os.environ['TZ'] = 'America/Los_Angeles'
            time.tzset()
            self.scheduler.step()
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            self.scheduler.step()
            archive = next(j for j in self.store.jobs() if j['name'] == 'archive')
            self.assertEqual(archive['next_run'], '2026-11-03T15:00:00Z')
        finally:
            if original is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = original
            time.tzset()

    @unittest.skipUnless(hasattr(time, 'tzset'), 'POSIX local timezone support required')
    def test_daily_time_tracks_dst_not_fixed_24_hour_intervals(self):
        original = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/Los_Angeles'
            time.tzset()
            self.assertEqual(next_daily(datetime(2026, 3, 7, 19, tzinfo=timezone.utc)), '2026-03-08T17:00:00Z')
            self.assertEqual(next_daily(datetime(2026, 10, 31, 19, tzinfo=timezone.utc)), '2026-11-01T18:00:00Z')
        finally:
            if original is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = original
            time.tzset()


class ApplicationTests(unittest.TestCase):
    def test_archive_collector_does_not_overwrite_application_owned_register(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = {PR: {**activity.new_pr(PR), 'description': 'Current intent'}}
            snapshot = {PR: {**activity.new_pr(PR), 'description': 'Capture-start snapshot'}}
            activity.write_json(root/'prs.json', current)
            before = (root/'prs.json').read_bytes()
            def github(since, until, run):
                return {'source': 'github', 'since': since, 'until': until, 'prs': []}
            manifest = activity.collect(root, '2026-11-03T18:00:00Z', ['github'],
                                        since='2026-11-02T18:00:00Z', collectors={'github': github},
                                        registry=snapshot, persist_registry=False)
            self.assertEqual((root/'prs.json').read_bytes(), before)
            self.assertEqual(activity.read_json(root/manifest['directory']/'prs.json', {})[PR]['description'],
                             'Capture-start snapshot')

    def test_future_marker_is_rejected_before_database_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            activity.write_json(root/'application.json', {'version': 99})
            with self.assertRaises(ValueError):
                Application(root)
            self.assertFalse((root/'tracker.sqlite3').exists())

    def test_archive_does_not_replace_concurrent_intent_with_old_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = Application(root)
            app.dispatch('intent', {'key': 'initial', 'kind': 'register_pr', 'payload': {'url': PR, 'description': 'Old'}})
            def collect(*args, **kwargs):
                self.assertFalse(kwargs['persist_registry'])
                app.dispatch('intent', {'key': 'new', 'kind': 'update_pr', 'payload': {'url': PR, 'description': 'New'}})
                activity.write_json(root/'snapshots/test/github.json', {'prs': [{'url': PR, 'state': 'open'}]})
                return {'directory': 'snapshots/test', 'sources': {'github': {'status': 'ok'}}}
            with mock.patch.object(activity, 'collect', side_effect=collect):
                self.assertTrue(app.archive({})['ok'])
            self.assertEqual(app.storage.export_registry()[PR]['description'], 'New')

    def test_application_owns_schedule_and_rejects_external_write_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Application(Path(tmp))
            self.assertEqual(app.dispatch('configure', {'archive_hour': 11})['archive_hour'], 11)
            self.assertFalse(app.dispatch('status', {})['github_writes'])
            for method, params in [('github_create', {}), ('refresh', {'shell': 'gh pr merge'})]:
                with self.assertRaises(ValueError):
                    app.dispatch(method, params)


if __name__ == '__main__':
    unittest.main()
