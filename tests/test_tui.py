import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from work_tracker.preferences import DEFAULT_PR_SORT, validate_pr_sort

HAS_TEXTUAL = importlib.util.find_spec('textual') is not None
if HAS_TEXTUAL:
    from textual.widgets import Button, DataTable, Input, Select, Static, TextArea
    from work_tracker.tui import DetailScreen, LinksScreen, WorkTrackerApp, detail_text, opened_text, thread_button_id
    from work_tracker.tui_forms import IntentForm


PR = 'https://github.com/acme/service/pull/42'
THREAD = 'https://acme.slack.com/archives/C123/p1793390000000000'


def record(number=42, title='[bold]Credential flow[/bold]', state='Ready to Merge', project='Unclassified', workstream='Unclassified'):
    return {'url': f'https://github.com/acme/service/pull/{number}',
            'github': {'title': title, 'state': 'open', 'body': 'Raw body: not-a-tracker-search-term'},
            'description': 'Local intent', 'project': 'Preserved legacy label' if number == 42 else project,
            'project_name': project, 'workstream_name': workstream,
            'review_state': state, 'review_reason': 'Test observation', 'refresh_error': None,
            'observed_at': '2026-11-03T22:00:00Z', 'task_title': title, 'task_id': f'task-{number}',
            'slack-threads': [{'url': THREAD, 'description': 'Design'}] if number == 42 else [],
            'agent-sessions': [], 'related-slack-messages': [{'text': 'not-a-tracker-search-term'}]}


class FakeAPI:
    def __init__(self, rows=None, catalog_extra=None):
        self.rows = deepcopy(rows if rows is not None else [
            record(), record(43, 'Storage refactor', 'Changes requested', 'Platform', 'Storage'),
            record(44, 'Ready signal handling', 'Blocked', 'Platform', 'Access'),
            record(45, 'Awaiting docs', 'Awaiting review', 'Other', 'Docs')])
        self.calls = []
        self.receipts = {}
        self.fail_read = False
        self.fail_catalog = False
        self.fail_sort_read = False
        self.fail_sort_save = False
        self.sort_preference = dict(DEFAULT_PR_SORT)
        self.catalog_extra = deepcopy(catalog_extra or {})
        self.lose_next_receipt = False
        self.lock = threading.RLock()
        self.inventory = {'total_open': 950, 'tracked_open': len(self.rows),
                          'untracked_open_estimate': 950-len(self.rows), 'observed_at': '2026-11-03T22:00:00Z',
                          'github_url': 'https://github.com/pulls?q=is%3Apr+is%3Aopen+author%3Atest'}
        self.jobs = [{'name': 'refresh_prs', 'status': 'idle', 'last_finished': '2026-11-03T22:00:00Z', 'last_error': None}]

    def __call__(self, root, method, params=None):
        params = params or {}
        with self.lock:
            self.calls.append((method, deepcopy(params)))
            if self.fail_read and method in ('status', 'list_prs', 'get_pr_sort'):
                raise RuntimeError('Local application unavailable')
            if method == 'get_pr_sort':
                if self.fail_sort_read:
                    raise RuntimeError('Saved sort unavailable')
                return deepcopy(self.sort_preference)
            if method == 'set_pr_sort':
                if self.fail_sort_save:
                    raise RuntimeError('Sort save unavailable')
                self.sort_preference = validate_pr_sort(params)
                return deepcopy(self.sort_preference)
            if method == 'status':
                return {'prs': len(self.rows), 'jobs': deepcopy(self.jobs), 'inventory': deepcopy(self.inventory)}
            if method == 'list_prs':
                start = params.get('offset', 0)
                limit = params.get('limit', 50)
                state = params.get('state')
                rows = self.rows if state is None else [r for r in self.rows if r['github'].get('state', 'open') == state]
                return {'total': len(rows), 'items': deepcopy(rows[start:start+limit]), 'inventory': deepcopy(self.inventory)}
            if method == 'catalog':
                if self.fail_catalog:
                    raise RuntimeError('Known names unavailable')
                projects = {name: set(values) for name, values in self.catalog_extra.items()}
                for row in self.rows:
                    projects.setdefault(row['project_name'], set()).add(row['workstream_name'])
                return {'projects': [{'name': name, 'workstreams': [{'name': value} for value in sorted(values)]}
                                     for name, values in sorted(projects.items())]}
            if method == 'get_pr':
                return deepcopy(next((r for r in self.rows if r['url'] == params['url']), None))
            if method == 'refresh':
                self.jobs[0]['status'] = 'running'
                return {'name': 'refresh_prs', 'status': 'queued', 'wait_for': 1}
            if method != 'intent':
                raise AssertionError(f'Unexpected client operation: {method}')
            key, kind, payload = params['key'], params['kind'], params['payload']
            if key in self.receipts:
                self.assert_same(params, self.receipts[key])
                return {'key': key, 'duplicate': True, 'url': payload['url']}
            row = next((r for r in self.rows if r['url'] == payload['url']), None)
            if kind == 'register_pr':
                if row is None:
                    row = record(int(payload['url'].rsplit('/', 1)[1]), title=payload['url'], state='Awaiting review')
                    row['description'] = None
                    row['project'] = None
                    row['github'] = {}
                    row['observed_at'] = None
                    self.rows.append(row)
            elif row is None:
                raise ValueError('PR is not registered')
            if kind in ('register_pr', 'update_pr'):
                for name, value in payload.get('expected', {}).items():
                    if row.get(name) != value:
                        raise ValueError('Metadata changed while this editor was open. Reload before saving.')
                if 'description' in payload:
                    row['description'] = payload['description']
                if 'project' in payload:
                    row['project_name'] = row['project'] = payload['project']
                    row['workstream_name'] = payload.get('workstream') or 'Unclassified'
            elif kind == 'attach_session':
                if payload['session'] not in row['agent-sessions']:
                    row['agent-sessions'].append(deepcopy(payload['session']))
            elif kind == 'attach_slack':
                old = next((t for t in row['slack-threads'] if t['url'] == payload['thread']), None)
                if old is None:
                    old = {'url': payload['thread']}
                    row['slack-threads'].append(old)
                if 'description' in payload:
                    old['description'] = payload['description']
            elif kind == 'remove_slack':
                row['slack-threads'] = [t for t in row['slack-threads'] if t['url'] != payload['thread']]
            else:
                raise AssertionError(kind)
            self.receipts[key] = deepcopy(params)
            if self.lose_next_receipt:
                self.lose_next_receipt = False
                raise RuntimeError('Connection closed after commit')
            return {'key': key, 'duplicate': False, 'url': payload['url']}

    @staticmethod
    def assert_same(left, right):
        if left != right:
            raise ValueError('Intent key reused for different content')


@unittest.skipUnless(HAS_TEXTUAL, 'Install requirements.lock for Textual UI tests')
class OpenedDateTests(unittest.TestCase):
    def test_opened_date_uses_utc_and_rejects_missing_or_invalid_timestamps(self):
        values = ((None, 'Unknown'), ('', 'Unknown'), ('bad', 'Unknown'),
                  ('2026-11-03', 'Unknown'), (42, 'Unknown'),
                  ('2026-11-03T23:30:00-02:00', '2026-11-04'),
                  ('2026-11-03T10:30:00Z', '2026-11-03'))
        for value, expected in values:
            with self.subTest(value=value):
                self.assertEqual(opened_text({'github': {'createdAt': value}}), expected)
        self.assertEqual(opened_text({'github': {'createdAt': '2026-11-03T23:30:00-02:00'}}, full=True),
                         '2026-11-04T01:30:00Z')
        self.assertEqual(opened_text({}), 'Unknown')


@unittest.skipUnless(HAS_TEXTUAL, 'Install requirements.lock for Textual UI tests')
class TUITests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def app(self, api, **kwargs):
        return WorkTrackerApp(Path('/tmp/not-a-tui-datastore'), rpc=api, poll_interval=0, start_view='prs', **kwargs)

    async def test_initial_view_pages_all_tracked_records_without_starting_jobs_or_database(self):
        rows = [record()] + [record(1000+i, f'PR {i}') for i in range(449)]
        rows[-1]['description'] = 'needle beyond the first page'
        api = FakeAPI(rows)
        app = self.app(api)
        with mock.patch('sqlite3.connect', side_effect=AssertionError('A TUI must not open SQLite')):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(app, pilot)
                self.assertTrue(app.connected)
                self.assertEqual(app.query_one('#prs', DataTable).row_count, 450)
                self.assertEqual(app.visible_rows[0]['url'], min(row['url'] for row in rows))
                self.assertEqual(app.query_one('#prs', DataTable).get_row(PR)[2].plain, '[bold]Credential flow[/bold]')
                self.assertEqual({p['offset'] for m,p in api.calls if m == 'list_prs'}, {0, 200, 400})
                app.query_one('#search', Input).value = 'needle beyond'
                await pilot.pause()
                self.assertEqual(len(app.visible_rows), 1)
        self.assertFalse(any(m in ('intent', 'refresh', 'serve', 'stop') for m,p in api.calls))

    async def test_opened_column_uses_cached_metadata_and_updates_from_local_reads(self):
        api = FakeAPI()
        api.rows[0]['github']['createdAt'] = '2026-11-03T23:30:00-02:00'
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            table = app.query_one('#prs', DataTable)
            self.assertEqual(len(table.columns), 5)
            self.assertEqual(table.get_row_at(0)[4].plain, '2026-11-04')
            self.assertEqual(table.get_row_at(1)[4].plain, 'Unknown')
            self.assertIn('2026-11-04T01:30:00Z', detail_text(api.rows[0]).plain)
            api.rows[1]['github']['createdAt'] = '2026-10-12T00:00:00Z'
            app.request_load()
            await self.settle(app, pilot)
            self.assertEqual(table.get_row_at(1)[4].plain, '2026-10-12')
            api.fail_read = True
            app.request_load()
            await self.settle(app, pilot)
            self.assertEqual(table.get_row_at(0)[4].plain, '2026-11-04')
            self.assertFalse(any(method not in ('status', 'list_prs', 'get_pr_sort') for method, params in api.calls))

    async def test_opened_column_fits_a_narrow_scrolling_list(self):
        rows = [record(100+i) for i in range(60)]
        for row in rows:
            row['github']['createdAt'] = '2026-11-03T10:00:00Z'
        app = self.app(FakeAPI(rows))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            table = app.query_one('#prs', DataTable)
            self.assertEqual(table.get_row_at(0)[4].plain, '2026-11-03')
            self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width)

    async def test_search_keys_filters_and_metadata_scope(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('/', 'r', 'e', 'a', 'd', 'y')
            await pilot.pause()
            self.assertEqual(len(app.visible_rows), 1)
            self.assertEqual(app.visible_rows[0]['url'].rsplit('/', 1)[1], '44')
            self.assertFalse(any(m == 'refresh' for m,p in api.calls))
            app.query_one('#search', Input).value = 'not-a-tracker-search-term'
            await pilot.pause()
            self.assertEqual(app.visible_rows, [])
            app.action_clear_filters()
            app.query_one('#project-filter', Select).value = 'Platform'
            await pilot.pause()
            self.assertEqual(len(app.visible_rows), 2)
            app.query_one('#workstream-filter', Select).value = 'Access'
            await pilot.pause()
            self.assertEqual(len(app.visible_rows), 1)
            app.query_one('#review-filter', Select).value = 'Ready to Merge'
            await pilot.pause()
            self.assertEqual(app.visible_rows, [])
            app.action_clear_filters()
            await pilot.pause()
            self.assertEqual(len(app.visible_rows), 4)

    async def test_selection_survives_poll_and_project_links_scope_the_view(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            app.query_one('#prs', DataTable).move_cursor(row=2)
            await pilot.pause()
            selected = app.selected_url
            app.request_load()
            await self.settle(app, pilot)
            self.assertEqual(app.selected_url, selected)
            await pilot.press('enter')
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.click('#detail-workstream')
            await pilot.pause()
            self.assertEqual(app._project, 'Platform')
            self.assertEqual(app._workstream, 'Access')
            self.assertEqual(len(app.visible_rows), 1)

    async def test_full_detail_updates_live_and_retains_its_pr_when_it_leaves_open_view(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('enter')
            detail = app.screen
            api.rows[0]['review_state'] = 'Changes requested'
            new_thread = THREAD.replace('/p1793390000000000', '/p1793390000000002')
            api.rows[0]['slack-threads'].append({'url': new_thread})
            app.request_load()
            await self.settle(app, pilot)
            self.assertIs(app.screen, detail)
            self.assertEqual(detail.record['review_state'], 'Changes requested')
            self.assertTrue(detail.query('#' + thread_button_id(new_thread)))
            api.rows[0]['github']['state'] = 'merged'
            app.request_load()
            await self.settle(app, pilot)
            self.assertEqual(len(app.all_rows), 3)
            self.assertEqual(detail.record['url'], PR)
            self.assertEqual(detail.record['github']['state'], 'merged')
            self.assertTrue(any(m == 'get_pr' for m,p in api.calls))

    async def test_editor_can_close_while_known_names_are_still_loading(self):
        api = FakeAPI(catalog_extra={'Historical': ['Archive']})
        started, release = threading.Event(), threading.Event()
        def delayed(root, method, params):
            if method == 'catalog':
                started.set()
                release.wait(2)
            return api(root, method, params)
        app = WorkTrackerApp(Path('/tmp/no-tui-store'), rpc=delayed, poll_interval=0, start_view='prs')
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await pilot.press('escape')
            release.set()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, IntentForm)
            await pilot.press('e')
            await self.settle(app, pilot)
            self.assertIn('Historical', app.screen.project_choices)
            self.assertFalse(any(m == 'intent' for m,p in api.calls))

    async def test_metadata_completes_known_names_even_without_open_prs(self):
        api = FakeAPI(catalog_extra={'Longterm': ['Archive', 'Research']})
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            await self.settle(app, pilot)
            form = app.screen
            self.assertIn('Longterm', form.project_choices)
            project = form.query_one('#form-project', Input)
            project.value = 'lon'
            project.focus()
            project.cursor_position = len(project.value)
            await pilot.pause()
            await pilot.press('right')
            await pilot.pause()
            self.assertEqual(project.value, 'Longterm')
            workstream = form.query_one('#form-workstream', Input)
            workstream.value = 'res'
            workstream.focus()
            workstream.cursor_position = len(workstream.value)
            await pilot.pause()
            await pilot.press('right')
            await pilot.pause()
            self.assertEqual(workstream.value, 'Research')
            self.assertFalse(any(m == 'intent' for m,p in api.calls))
            self.assertEqual(sum(m == 'catalog' for m,p in api.calls), 1)

    async def test_workstream_completion_switches_context_without_old_ghost_or_draft_loss(self):
        api = FakeAPI(catalog_extra={'Other': ['Accounts']})
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            await self.settle(app, pilot)
            form = app.screen
            project = form.query_one('#form-project', Input)
            workstream = form.query_one('#form-workstream', Input)
            project.value = 'Platform'
            workstream.value = 'Ac'
            await pilot.pause()
            self.assertEqual(form.workstream_choices, ['Access', 'Storage'])
            project.value = 'Other'
            await pilot.pause()
            self.assertEqual(workstream.value, 'Ac')
            self.assertEqual(form.workstream_choices, ['Accounts', 'Docs'])
            workstream.focus()
            workstream.cursor_position = len(workstream.value)
            await pilot.press('right')
            await pilot.pause()
            self.assertEqual(workstream.value, 'Accounts')
            self.assertFalse(any(m == 'intent' for m,p in api.calls))

    async def test_metadata_reuses_known_casing_but_still_accepts_new_names(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            await self.settle(app, pilot)
            app.screen.query_one('#form-project', Input).value = 'platform'
            app.screen.query_one('#form-workstream', Input).value = 'storage'
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual((api.rows[0]['project_name'], api.rows[0]['workstream_name']), ('Platform', 'Storage'))
            await pilot.press('e')
            await self.settle(app, pilot)
            app.screen.query_one('#form-project', Input).value = 'New initiative'
            app.screen.query_one('#form-workstream', Input).value = 'First milestone'
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual((api.rows[0]['project_name'], api.rows[0]['workstream_name']), ('New initiative', 'First milestone'))

    async def test_catalog_failure_keeps_manual_entry_available_without_database_fallback(self):
        api = FakeAPI()
        api.fail_catalog = True
        app = self.app(api)
        with mock.patch('sqlite3.connect', side_effect=AssertionError('No UI database fallback')):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.settle(app, pilot)
                await pilot.press('e')
                await self.settle(app, pilot)
                self.assertFalse(app.screen.catalog_loading)
                self.assertFalse(app.screen.query_one('#form-save', Button).disabled)
                app.screen.query_one('#form-description', TextArea).load_text('Manual edit still works')
                await pilot.press('ctrl+enter')
                await self.settle(app, pilot)
                self.assertEqual(api.rows[0]['description'], 'Manual edit still works')

    async def test_edit_only_submits_changed_local_fields_and_preserves_legacy_label(self):
        api = FakeAPI()
        github = deepcopy(api.rows[0]['github'])
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            self.assertIsInstance(app.screen, IntentForm)
            app.screen.query_one('#form-description', TextArea).load_text('New local context')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, IntentForm)
            params = next(p for m,p in api.calls if m == 'intent')
            self.assertEqual(params['source'], {'client': 'tui'})
            self.assertEqual(params['payload'], {'url': PR, 'description': 'New local context', 'expected': {'description': 'Local intent'}})
            self.assertEqual(api.rows[0]['github'], github)
            self.assertEqual(api.rows[0]['project'], 'Preserved legacy label')

    async def test_conflicting_edit_is_not_silently_applied_and_poll_does_not_close_editor(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            api.rows[0]['description'] = 'Changed by another agent'
            app.request_load()
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, IntentForm)
            app.screen.query_one('#form-description', TextArea).load_text('My stale edit')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, IntentForm)
            self.assertEqual(api.rows[0]['description'], 'Changed by another agent')
            self.assertTrue(app.screen.query_one('#form-description', TextArea).disabled)
            await pilot.press('escape')
            await self.settle(app, pilot)
            self.assertEqual(app.selected_record['description'], 'Changed by another agent')

    async def test_unknown_save_receipt_retries_with_identical_intent_key(self):
        api = FakeAPI()
        api.lose_next_receipt = True
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            app.screen.query_one('#form-description', TextArea).load_text('Saved once')
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, IntentForm)
            first = [p for m,p in api.calls if m == 'intent'][0]
            await pilot.click('#form-save')
            await self.settle(app, pilot)
            attempts = [p for m,p in api.calls if m == 'intent']
            self.assertEqual(attempts, [first, first])
            self.assertEqual(len(api.receipts), 1)
            self.assertNotIsInstance(app.screen, IntentForm)

    async def test_registration_validation_and_explicit_local_ingestion(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('n')
            app.screen.query_one('#form-url', Input).value = 'file:///not-a-pr'
            await pilot.press('ctrl+enter')
            await pilot.pause()
            self.assertFalse(any(m == 'intent' for m,p in api.calls))
            app.screen.query_one('#form-url', Input).value = 'https://github.com/acme/service/pull/999'
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual(len(api.rows), 5)
            self.assertEqual(app.selected_url, 'https://github.com/acme/service/pull/999')
            self.assertEqual(app.selected_record['project_name'], 'Unclassified')
            self.assertFalse(any(m == 'refresh' for m,p in api.calls))

    async def test_links_sessions_and_safe_explicit_browser_actions(self):
        api = FakeAPI()
        opened = []
        app = self.app(api, opener=opened.append)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('s')
            await self.settle(app, pilot)
            await pilot.click('#sessions-add')
            await pilot.pause()
            for name,value in {'tool':'fx','path':'relative/path','sessionId':'known-id','machine':'my-laptop'}.items():
                app.screen.query_one(f'#form-{name}', Input).value = value
            await pilot.press('ctrl+enter')
            await pilot.pause()
            self.assertFalse(any(m == 'intent' for m,p in api.calls))
            app.screen.query_one('#form-path', Input).value = '/work/tree'
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual(api.rows[0]['agent-sessions'][0]['sessionId'], 'known-id')
            self.assertEqual(opened, [])
            await pilot.press('t')
            new_thread = THREAD.replace('/p1793390000000000','/p1793390000000001')
            app.screen.query_one('#form-thread', Input).value = new_thread
            app.screen.query_one('#form-thread-description', Input).value = 'Follow-up'
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            await pilot.press('enter')
            selector = '#' + thread_button_id(new_thread)
            app.screen.query_one(selector, Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click(selector)
            self.assertEqual(opened, [new_thread])
            await pilot.press('escape')
            await pilot.press('d')
            app.screen.query_one('#form-remove-thread', Select).value = new_thread
            await pilot.press('ctrl+enter')
            await self.settle(app, pilot)
            self.assertEqual(api.rows[0]['slack-threads'], [{'url': THREAD, 'description': 'Design'}])
            app.open_reference('file:///etc/passwd', 'slack')
            self.assertEqual(opened, [new_thread])

    async def test_mouse_link_menu_keeps_its_original_pr_target(self):
        api = FakeAPI()
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.click('#manage-links')
            self.assertIsInstance(app.screen, LinksScreen)
            app.selected_url = api.rows[1]['url']
            await pilot.click('#links-slack')
            await pilot.pause()
            self.assertIsInstance(app.screen, IntentForm)
            self.assertEqual(app.screen.record['url'], PR)
            await pilot.press('escape')
            await self.settle(app, pilot)
            self.assertFalse(any(m == 'intent' for m,p in api.calls))

    async def test_slow_view_reads_coalesce_without_parallel_reload_or_remote_refresh(self):
        api = FakeAPI()
        lock = threading.Lock()
        active = maximum = completed = 0
        def slow(root, method, params):
            nonlocal active, maximum, completed
            if method != 'list_prs':
                return api(root, method, params)
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.05)
                return api(root, method, params)
            finally:
                with lock:
                    active -= 1
                    completed += 1
        app = WorkTrackerApp(Path('/tmp/no-tui-store'), rpc=slow, poll_interval=0.01, start_view='prs')
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.35)
            self.assertTrue(app.connected)
            self.assertGreaterEqual(completed, 2)
            self.assertEqual(maximum, 1)
            self.assertFalse(any(m == 'refresh' for m,p in api.calls))

    async def test_refresh_is_explicit_while_periodic_view_poll_is_local_only(self):
        api = FakeAPI()
        app = WorkTrackerApp(Path('/tmp/no-tui-store'), rpc=api, poll_interval=0.08, start_view='prs')
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            await pilot.pause(0.2)
            self.assertGreaterEqual(sum(m == 'status' for m,p in api.calls), 2)
            self.assertFalse(any(m == 'refresh' for m,p in api.calls))
            await pilot.press('r')
            await pilot.pause(0.2)
            self.assertEqual(sum(m == 'refresh' for m,p in api.calls), 1)
            self.assertEqual(api.jobs[0]['status'], 'running')
        self.assertFalse(any(m in ('serve','stop','configure') for m,p in api.calls))

    async def test_offline_is_not_empty_and_retains_last_loaded_view(self):
        api = FakeAPI()
        api.fail_read = True
        app = self.app(api)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.settle(app, pilot)
            self.assertFalse(app.connected)
            self.assertFalse(app.loaded)
            self.assertTrue(app.query_one('#edit-pr', Button).disabled)
            api.fail_read = False
            app.request_load()
            await self.settle(app, pilot)
            self.assertEqual(len(app.all_rows), 4)
            api.fail_read = True
            app.request_load()
            await self.settle(app, pilot)
            self.assertFalse(app.connected)
            self.assertEqual(app.query_one('#prs', DataTable).row_count, 4)
            await pilot.press('n')
            self.assertNotIsInstance(app.screen, IntentForm)
        empty = self.app(FakeAPI([]))
        async with empty.run_test(size=(140,40)) as pilot:
            await self.settle(empty,pilot)
            self.assertTrue(empty.loaded)
            self.assertTrue(empty.connected)
            self.assertEqual(empty.all_rows, [])

    async def test_narrow_layout_and_full_detail_view(self):
        app = self.app(FakeAPI())
        async with app.run_test(size=(80, 28)) as pilot:
            await self.settle(app, pilot)
            self.assertTrue(app.screen.has_class('narrow'))
            self.assertFalse(app.query_one('#preview').display)
            for button in app.query('#toolbar Button'):
                self.assertLessEqual(button.region.right, 80)
            await pilot.click('#details-pr')
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press('escape')
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            self.assertTrue(app.query_one('#preview').display)


if __name__ == '__main__':
    unittest.main()
