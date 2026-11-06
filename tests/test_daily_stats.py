from datetime import datetime, timedelta, timezone
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import work_activity as activity
from work_tracker import cli
from work_tracker.daily_stats import day_window
from work_tracker.scheduler import Scheduler, next_daily
from work_tracker.service import Application, DEFAULT_CONFIG
from work_tracker.storage import SQLiteStorage


class DailyStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'
        time.tzset()
        self.addCleanup(self.restore_zone)
        self.now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        self.store = SQLiteStorage(Path(self.temp.name)/'tracker.sqlite3')

    def restore_zone(self):
        if self.old_tz is None: os.environ.pop('TZ', None)
        else: os.environ['TZ'] = self.old_tz
        time.tzset()

    def pr(self, number, project=None, state='open', opened='2026-11-01T00:00:00Z', closed=None):
        url = f'https://github.com/acme/service/pull/{number}'
        payload = {'url': url}
        if project: payload['project'] = project
        self.store.apply_intent(f'register-{number}', 'register_pr', payload, {})
        self.observe(url, state, opened, closed)
        return url

    def observe(self, url, state, opened, closed=None):
        github = {'state': state, 'createdAt': opened, 'mergedAt': closed if state == 'merged' else None,
                  'closedAt': closed if state in ('closed', 'merged') else None}
        with mock.patch('work_tracker.storage.now_iso', return_value=activity.iso(self.now)):
            self.store.observe(url, github, 'Awaiting review', 'Fixture')

    def capture(self):
        return self.store.capture_statistics(self.now)

    def rows(self, day='2026-11-05', project='*'):
        return self.store.statistics({'since': day, 'until': day, 'project': project}, self.now)['items']

    def overall(self, day='2026-11-05'):
        return self.rows(day, None)[0]

    def test_daily_cohort_overall_projects_unallocated_and_zero_projects(self):
        self.pr(1, 'Alpha', 'merged', '2026-11-01T00:00:00Z', '2026-11-05T10:00:00Z')
        self.pr(2, None, 'closed', '2026-11-04T11:00:00Z', '2026-11-05T11:00:00Z')
        self.pr(3, 'Alpha', 'open', '2026-10-01T00:00:00Z')
        self.capture()
        rows = {row['project']: row for row in self.rows()}
        self.assertEqual(set(rows), {'Overall', 'Alpha', 'Unallocated'})
        overall = rows['Overall']
        self.assertEqual((overall['total_tracked'], overall['closed_count'], overall['merged_count']), (3, 1, 1))
        self.assertEqual(overall['mean_closure_seconds'], (381600+86400)/2)
        self.assertEqual(overall['p90_closure_seconds'], 381600)
        self.assertEqual(overall['oldest_open_age_seconds'], 35.5*86400)
        self.assertEqual(rows['Unallocated']['total_tracked'], 1)
        self.assertEqual(rows['Unallocated']['mean_closure_seconds'], 86400)
        self.assertEqual(sum(row['total_tracked'] for name,row in rows.items() if name!='Overall'), overall['total_tracked'])

    def test_zero_closures_have_no_mean_or_p90_and_unallocated_is_kept_at_zero(self):
        self.pr(1, 'Alpha')
        self.capture()
        row = self.overall()
        self.assertEqual(row['completed_count'], 0)
        self.assertIsNone(row['mean_closure_seconds'])
        self.assertIsNone(row['p90_closure_seconds'])
        self.assertEqual(self.rows(project='Unallocated')[0]['total_tracked'], 0)

    def test_p90_is_nearest_rank_of_that_days_closures_not_all_historical_closures(self):
        closed = datetime(2026,11,5,10,tzinfo=timezone.utc)
        for number in range(1,11):
            self.pr(number, state='closed', opened=activity.iso(closed-timedelta(seconds=number)), closed=activity.iso(closed))
        self.pr(20, state='merged', opened='2020-01-01T00:00:00Z', closed='2026-11-04T10:00:00Z')
        self.capture()
        row = self.overall()
        self.assertEqual(row['closure_sample_count'], 10)
        self.assertEqual(row['mean_closure_seconds'], 5.5)
        self.assertEqual(row['p90_closure_seconds'], 9)

    def test_bad_future_and_missing_dates_are_excluded_with_explicit_coverage(self):
        self.pr(1, opened='bad')
        self.pr(2, opened='2027-01-01T00:00:00Z')
        self.pr(3, opened='2026-11-01T00:00:00Z')
        self.pr(4, state='closed', opened=None, closed='2026-11-05T10:00:00Z')
        self.pr(5, state='closed', closed=None)
        self.capture()
        row = self.overall()
        self.assertEqual(row['missing_open_date_count'], 2)
        self.assertFalse(row['oldest_open_complete'])
        self.assertEqual(row['closed_count'], 1)
        self.assertEqual(row['unknown_closure_time_count'], 1)
        self.assertEqual(row['missing_close_date_count'], 1)
        self.assertIsNone(row['mean_closure_seconds'])

    def test_history_preserves_membership_and_project_assignment_after_reclassification(self):
        url = self.pr(1, 'Alpha')
        self.capture()
        self.now += timedelta(days=1)
        self.store.apply_intent('move', 'update_pr', {'url': url, 'project': 'Beta'}, {})
        self.pr(2, 'Beta')
        self.capture()
        previous = {row['project']: row for row in self.rows()}
        self.assertEqual(previous['Alpha']['total_tracked'], 1)
        self.assertEqual(previous['Overall']['total_tracked'], 1)
        self.assertNotIn('Beta', previous)
        today = {row['project']: row for row in self.rows('2026-11-06')}
        self.assertEqual(today['Alpha']['total_tracked'], 0)
        self.assertEqual(today['Beta']['total_tracked'], 2)

    def test_late_closure_observation_corrects_the_right_day_without_retagging_it(self):
        url = self.pr(1, 'Alpha')
        self.capture()
        self.now += timedelta(days=1)
        self.store.apply_intent('move', 'update_pr', {'url': url, 'project': 'Beta'}, {})
        self.observe(url, 'merged', '2026-11-01T00:00:00Z', '2026-11-05T23:00:00Z')
        self.capture()
        previous = {row['project']: row for row in self.rows()}
        self.assertEqual(previous['Alpha']['merged_count'], 1)
        self.assertEqual(previous['Overall']['open_count'], 0)
        self.assertNotIn('Beta', previous)
        self.assertEqual(self.overall('2026-11-06')['merged_count'], 0)

    def test_observed_closures_survive_reopening_and_repeated_capture_is_idempotent(self):
        url = self.pr(1, state='closed', closed='2026-11-05T11:00:00Z')
        self.capture(); self.capture()
        self.assertEqual(self.overall()['closed_count'], 1)
        self.now += timedelta(days=1)
        self.observe(url, 'open', '2026-11-01T00:00:00Z')
        self.capture()
        self.assertEqual(self.overall('2026-11-06')['open_count'], 1)
        self.now += timedelta(days=1)
        self.observe(url, 'merged', '2026-11-01T00:00:00Z', '2026-11-07T11:00:00Z')
        self.capture()
        self.assertEqual(self.overall()['closed_count'], 1)
        self.assertEqual(self.overall('2026-11-07')['merged_count'], 1)

    def test_missed_days_are_gaps_not_invented_inventory_and_clock_rollback_does_not_rewrite_history(self):
        self.pr(1)
        self.capture()
        self.now += timedelta(days=3)
        self.capture()
        result = self.store.statistics({'since':'2026-11-05','until':'2026-11-08'},self.now)
        self.assertEqual(result['missing_days'], ['2026-11-06','2026-11-07'])
        self.assertEqual(result['total'], 2)
        before = deepcopy(self.rows())
        self.now -= timedelta(days=3)
        self.assertIn('skipped',self.capture())
        self.assertEqual(self.rows(),before)

    def test_one_pr_contributes_its_latest_closure_only_once_per_day(self):
        url=self.pr(1,state='closed',closed='2026-11-05T10:00:00Z')
        self.capture()
        self.observe(url,'open','2026-11-01T00:00:00Z')
        self.capture()
        self.observe(url,'merged','2026-11-01T00:00:00Z','2026-11-05T11:00:00Z')
        self.capture()
        row=self.overall()
        self.assertEqual((row['closed_count'],row['merged_count'],row['closure_sample_count']),(0,1,1))

    def test_stale_observations_are_not_reported_as_complete_zero_activity(self):
        url=self.pr(1)
        self.store.observation_error(url,'Source unavailable')
        self.capture()
        row=self.overall()
        self.assertEqual(row['closed_count'],0)
        self.assertFalse(row['closure_counts_complete'])
        self.assertEqual(row['stale_pr_count'],1)
        self.assertIsNone(row['mean_closure_seconds'])

    def test_clock_rollback_inside_one_day_does_not_replace_a_newer_snapshot(self):
        self.pr(1)
        self.capture()
        before=self.rows()
        self.now-=timedelta(minutes=10)
        self.assertIn('skipped',self.capture())
        self.assertEqual(self.rows(),before)

    def test_midnight_is_exclusive_and_dst_windows_are_calendar_days(self):
        self.pr(1, state='merged', closed='2026-11-06T00:00:00Z')
        self.capture()
        self.assertEqual(self.overall()['merged_count'],0)
        os.environ['TZ']='America/Los_Angeles'; time.tzset()
        spring=day_window(datetime(2026,3,8,20,tzinfo=timezone.utc))
        fall=day_window(datetime(2026,11,1,20,tzinfo=timezone.utc))
        parse=lambda value: datetime.fromisoformat(value.replace('Z','+00:00'))
        self.assertEqual((parse(spring['window_end'])-parse(spring['window_start'])).total_seconds(),23*3600)
        self.assertEqual((parse(fall['window_end'])-parse(fall['window_start'])).total_seconds(),25*3600)

    def test_migration_and_capturing_do_not_modify_pr_metadata_jobs_or_preferences(self):
        self.pr(1)
        self.store.set_pr_sort({'field':'opened','descending':True})
        before=self.store.export_registry()
        jobs=self.store.jobs()
        with self.store._transaction() as db:
            for table in ('stats_metrics','stats_members','stats_closures','stats_days'): db.execute('DROP TABLE '+table)
            db.execute('PRAGMA user_version=4')
        self.store=SQLiteStorage(Path(self.temp.name)/'tracker.sqlite3')
        self.capture()
        self.assertEqual(self.store.export_registry(),before)
        self.assertEqual(self.store.jobs(),jobs)
        self.assertEqual(self.store.get_pr_sort(),{'field':'opened','descending':True})
        restored=SQLiteStorage(Path(self.temp.name)/'tracker.sqlite3')
        self.assertEqual(restored.statistics({'since':'2026-11-05','until':'2026-11-05'},self.now)['total'],1)

    def test_query_validation_and_aggregation_never_run_external_commands(self):
        self.capture()
        for params in ({'since':'not-a-day'},{'days':True},{'days':0},{'limit':5001},{'offset':-1},
                       {'since':'2026-11-07','until':'2026-11-05'},{'shell':'anything'}):
            with self.subTest(params=params),self.assertRaises(ValueError): self.store.statistics(params,self.now)
        with mock.patch('work_activity.run_json') as run:
            self.store.statistics({},self.now)
            self.capture()
        run.assert_not_called()

    def test_daily_job_catches_up_without_creating_missing_days_or_duplicate_slots(self):
        self.pr(1)
        scheduler=Scheduler(self.store,{'daily_stats':lambda params:self.capture()},dict(DEFAULT_CONFIG),clock=lambda:self.now)
        scheduler.step(); scheduler.step()
        job=next(job for job in self.store.jobs() if job['name']=='daily_stats')
        self.assertEqual(job['completed_seq'],1)
        self.assertEqual(job['next_run'],'2026-11-06T00:05:00Z')
        self.now += timedelta(days=3)
        scheduler.step()
        self.assertEqual(self.store.statistics({'since':'2026-11-05','until':'2026-11-08'},self.now)['total'],2)

    def test_failed_statistics_job_retains_rows_exposes_failure_and_retries(self):
        self.pr(1)
        self.capture()
        before=self.rows()
        def fail(params): raise ValueError('Do not expose private payloads')
        scheduler=Scheduler(self.store,{'daily_stats':fail},dict(DEFAULT_CONFIG),clock=lambda:self.now)
        scheduler.step()
        history=self.store.statistics({'days':1},self.now)
        self.assertIn('ValueError',history['collection']['last_error'])
        self.assertNotIn('private payloads',history['collection']['last_error'])
        self.assertEqual(self.rows(),before)
        self.now+=timedelta(seconds=60)
        scheduler.callbacks['daily_stats']=lambda params:self.capture()
        scheduler.step()
        self.assertIsNone(self.store.statistics({'days':1},self.now)['collection']['last_error'])

    def test_service_updates_on_intent_and_cli_reads_without_mutating(self):
        app=Application(Path(self.temp.name)/'application',clock=lambda:self.now,run=mock.Mock())
        app.dispatch('intent',{'key':'fixture','kind':'register_pr','payload':{'url':'https://github.com/acme/service/pull/42'}})
        value=app.dispatch('statistics',{'project':'*','days':1})
        self.assertEqual({row['project'] for row in value['items']},{'Overall','Unallocated'})
        app.run.assert_not_called()
        with mock.patch('work_tracker.cli.request',return_value=value) as request,mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['stats','--all-projects','--days','1','--json']),0)
            self.assertEqual(json.loads(output.getvalue())['total'],2)
            self.assertEqual(request.call_args.args[1],'statistics')
            self.assertEqual(request.call_args.args[2]['project'],'*')
        queued=app.dispatch('capture_statistics',{})
        self.assertEqual(queued['name'],'daily_stats')
        with mock.patch('work_tracker.cli.request',return_value=queued),mock.patch('work_tracker.cli.wait_for_job',return_value={'ok':True}) as wait,mock.patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(cli.main(['stats','--capture','--wait']),0)
            wait.assert_called_once()


if __name__=='__main__': unittest.main()
