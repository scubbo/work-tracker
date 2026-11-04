from pathlib import Path
import tempfile
import unittest

import work_activity as activity
from work_tracker.github import fetch_batch, fetch_pr, open_inventory
from work_tracker.service import Application


PR = 'https://github.com/acme/service/pull/42'
OTHER = 'https://github.com/acme/service/pull/43'


def node(url=PR, state='OPEN', reviews=True):
    result = {'url': url, 'state': state, 'author': {'login': 'jack'}, 'title': 'Example',
              'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'reviewDecision': 'APPROVED',
              'commits': {'nodes': [{'commit': {'statusCheckRollup': {'state': 'SUCCESS'}}}]}}
    if reviews:
        result['latestOpinionatedReviews'] = {'pageInfo': {'hasNextPage': False}, 'nodes': [
            {'id': 'review1', 'state': 'APPROVED', 'submittedAt': '2026-11-03T18:00:00Z',
             'author': {'login': 'reviewer', '__typename': 'User'}}]}
    return result


class GitHubTests(unittest.TestCase):
    def test_account_inventory_fetches_count_only_not_all_open_details(self):
        calls = []
        def run(args):
            calls.append(args)
            if args == ['gh', 'api', 'user']:
                return {'login': 'jack'}
            return {'total_count': 1950, 'items': [{'html_url': OTHER}]}
        inventory = open_inventory(run)
        self.assertEqual(inventory['total_open'], 1950)
        self.assertEqual(len(calls), 2)
        self.assertIn('per_page=1', calls[1])
        self.assertNotIn('--paginate', calls[1])
        self.assertIn('github.com/pulls?', inventory['github_url'])

    def test_incomplete_count_is_not_presented_as_a_complete_total(self):
        def run(args):
            return {'login': 'jack'} if args[-1] == 'user' else {'total_count': 950, 'incomplete_results': True}
        with self.assertRaises(activity.CollectionError):
            open_inventory(run)

    def test_review_pages_and_read_only_fallback_commands(self):
        calls = []
        def run(args):
            calls.append(args)
            if args[1:3] == ['pr', 'view']:
                return {'url': PR, 'state': 'OPEN', 'author': {'login': 'jack'}}
            return [[{'id': 1}], [{'id': 2}]]
        detail, reviews = fetch_pr(PR, run)
        self.assertEqual(detail['state'], 'open')
        self.assertEqual(len(reviews), 2)
        self.assertIn('--paginate', calls[1])
        self.assertFalse(any(word in {'create', 'edit', 'merge', 'close', 'PATCH', 'DELETE'} for args in calls for word in args))

    def test_batch_uses_query_variables_and_skips_reviews_for_known_closed_prs(self):
        calls = []
        def run(args):
            calls.append(args)
            return {'data': {'p0': {'pullRequest': node()}, 'p1': {'pullRequest': node(OTHER, 'CLOSED', False)}}}
        values = fetch_batch([PR, OTHER], run, {PR})
        self.assertEqual(len(calls), 1)
        query = next(value for value in calls[0] if value.startswith('query='))
        self.assertTrue(query.startswith('query=query WorkTrackerPRs'))
        self.assertNotIn('mutation', query)
        self.assertEqual(query.count('latestOpinionatedReviews'), 1)
        self.assertIn('n0=42', calls[0])
        self.assertIn('n1=43', calls[0])
        self.assertEqual(values[PR][1][0]['user']['type'], 'User')
        self.assertEqual(values[OTHER][0]['state'], 'closed')

    def test_batch_falls_back_to_complete_reviews_when_review_connection_is_capped(self):
        partial = node()
        partial['latestOpinionatedReviews']['pageInfo']['hasNextPage'] = True
        calls = []
        def run(args):
            calls.append(args)
            if args[1:3] == ['api', 'graphql']:
                return {'data': {'p0': {'pullRequest': partial}}}
            if args[1:3] == ['pr', 'view']:
                return {'url': PR, 'state': 'OPEN', 'author': {'login': 'jack'}}
            return [[{'id': 1}], [{'id': 2}]]
        values = fetch_batch([PR], run)
        self.assertEqual(len(values[PR][1]), 2)
        self.assertTrue(any('--paginate' in args for args in calls))

    def test_refresh_only_ingested_prs_never_enrolls_account_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fail = False
            queried = []
            def run(args):
                if args == ['gh', 'api', 'user']:
                    return {'login': 'jack'}
                if 'search/issues' in args:
                    return {'total_count': 950, 'items': [{'html_url': OTHER}]}
                queried.append(args)
                if fail:
                    raise activity.CollectionError('Unavailable')
                return {'data': {'p0': {'pullRequest': node()}}}
            app = Application(Path(tmp), run=run)
            app.dispatch('intent', {'key': 'tracked', 'kind': 'register_pr', 'payload': {'url': PR}})
            self.assertTrue(app.refresh_prs({})['ok'])
            self.assertEqual(app.storage.list_prs()[0]['review_state'], 'Ready to Merge')
            self.assertEqual(len(app.storage.list_prs()), 1)
            self.assertTrue(all('n0=42' in args and 'n1=43' not in args for args in queried))
            self.assertEqual(app.storage.inventory()['untracked_open_estimate'], 949)
            fail = True
            self.assertFalse(app.refresh_prs({})['ok'])
            self.assertEqual(app.storage.list_prs()[0]['review_state'], 'Blocked')
        with tempfile.TemporaryDirectory() as tmp:
            queried.clear()
            app = Application(Path(tmp), run=run)
            self.assertTrue(app.refresh_prs({})['ok'])
            self.assertEqual(app.storage.list_prs(), [])
            self.assertEqual(queried, [])
            self.assertEqual(app.storage.inventory()['total_open'], 950)


if __name__ == '__main__':
    unittest.main()
