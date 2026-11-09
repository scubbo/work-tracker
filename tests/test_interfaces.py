import importlib.util
import json
import select
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from work_tracker.client import request


ROOT = Path(__file__).resolve().parents[1]
PR = 'https://github.com/acme/service/pull/42'


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wt-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([sys.executable, str(ROOT/'tracker.py'), '--state-dir', str(self.root),
                                         'serve', '--no-jobs'], stdout=self.stdout, stderr=self.stderr)
        for _ in range(100):
            try:
                request(self.root, 'status')
                return
            except RuntimeError:
                if self.process.poll() is not None:
                    break
                time.sleep(0.05)
        self.stderr.seek(0)
        self.fail(f'Application did not start: {self.stderr.read().decode()}')

    def tearDown(self):
        if self.process.poll() is None:
            try:
                request(self.root, 'stop')
                self.process.wait(timeout=5)
            except (RuntimeError, subprocess.TimeoutExpired):
                self.process.terminate()
                self.process.wait(timeout=5)
        self.stdout.close()
        self.stderr.close()
        self.temp.cleanup()

    def test_metadata_catalog_is_available_through_the_application_socket(self):
        request(self.root, 'intent', {'key': 'catalog-fixture', 'kind': 'register_pr',
                                     'payload': {'url': PR, 'project': 'Platform', 'workstream': 'Access'}})
        before = request(self.root, 'status')['prs']
        catalog = request(self.root, 'catalog')
        project = next(p for p in catalog['projects'] if p['name'] == 'Platform')
        self.assertEqual([w['name'] for w in project['workstreams']], ['Access'])
        self.assertEqual(request(self.root, 'status')['prs'], before)
        with self.assertRaises(ValueError):
            request(self.root, 'catalog', {'create': 'Not allowed'})

    def test_single_owner_socket_permissions_and_rpc_intents(self):
        duplicate = subprocess.run([sys.executable, str(ROOT/'tracker.py'), '--state-dir', str(self.root),
                                    'serve', '--no-jobs'], capture_output=True, text=True)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn('already owns', duplicate.stderr)
        self.assertEqual((self.root/'app.sock').stat().st_mode & 0o777, 0o600)
        result = request(self.root, 'intent', {'key': 'register', 'kind': 'register_pr',
                                              'payload': {'url': PR}, 'source': {'client': 'test'}})
        self.assertFalse(result['duplicate'])
        self.assertEqual(request(self.root, 'list_prs')['total'], 1)
        with self.assertRaises(ValueError):
            request(self.root, 'intent', {'key': 'spoof', 'kind': 'update_pr',
                                          'payload': {'url': PR, 'state': 'merged'}})

    def test_my_day_roundtrip_uses_the_application_socket_without_creating_a_pr(self):
        view=request(self.root,'my_day')
        payload={'day':view['today'],'kind':'goal','text':'Socket day outcome','project':'Alpha','workstream':'Flow','notes':'Plan\n\nNext step'}
        params={'key':'day-socket','action':'add','payload':payload,'source':{'client':'test'}}
        first=request(self.root,'day_intent',params)
        self.assertTrue(request(self.root,'day_intent',params)['duplicate'])
        item=request(self.root,'my_day')['goals'][0]
        self.assertEqual(item['id'],first['item_id'])
        request(self.root,'day_intent',{'key':'done-socket','action':'set_done',
            'payload':{'day':view['today'],'item_id':item['id'],'expected_version':item['version'],'done':True},'source':{'client':'test'}})
        completed=request(self.root,'my_day')['goals'][0]
        self.assertTrue(completed['done'])
        request(self.root,'day_intent',{'key':'classify-socket','action':'edit','payload':{'day':view['today'],
            'item_id':completed['id'],'expected_version':completed['version'],'project':'Beta','workstream':'Review',
            'expected_workstream_id':completed['target']['workstream_id']},'source':{'client':'test'}})
        classified=request(self.root,'my_day')['goals'][0]
        self.assertEqual((classified['target']['project_name'],classified['target']['workstream_name']),('Beta','Review'))
        self.assertTrue(classified['done'])
        self.assertEqual(classified['notes'],'Plan\n\nNext step')
        request(self.root,'day_intent',{'key':'notes-socket','action':'edit','payload':{'day':view['today'],
            'item_id':classified['id'],'expected_version':classified['version'],'notes':'Updated\nnotes'},'source':{'client':'test'}})
        self.assertEqual(request(self.root,'my_day')['goals'][0]['notes'],'Updated\nnotes')
        self.assertFalse(classified['target']['closed'])
        self.assertEqual(request(self.root,'status')['prs'],0)
        self.assertEqual(request(self.root,'day_targets',{'search':'Socket day outcome'})['total'],1)
        with self.assertRaises(ValueError): request(self.root,'day_intent',{**params,'shell':'anything'})

    def test_daily_statistics_are_readable_and_snapshot_jobs_belong_to_the_application(self):
        request(self.root,'intent',{'key':'stats-fixture','kind':'register_pr','payload':{'url':PR,'project':'Alpha'}})
        before=request(self.root,'get_pr',{'url':PR})
        value=request(self.root,'statistics',{'project':'*','days':1})
        self.assertEqual({row['project'] for row in value['items']},{'Overall','Alpha','Unallocated'})
        self.assertEqual(next(row for row in value['items'] if row['project']=='Overall')['total_tracked'],1)
        self.assertEqual(next(row for row in value['items'] if row['project']=='Unallocated')['total_tracked'],0)
        self.assertEqual(request(self.root,'get_pr',{'url':PR}),before)
        queued=request(self.root,'capture_statistics')
        self.assertEqual(queued['name'],'daily_stats')
        with self.assertRaises(ValueError): request(self.root,'statistics',{'shell':'anything'})
        with self.assertRaises(ValueError): request(self.root,'capture_statistics',{'project':'Alpha'})

    def test_merge_confirmation_boundary_is_enforced_through_the_real_socket(self):
        request(self.root, 'intent', {'key': 'merge-fixture', 'kind': 'register_pr', 'payload': {'url': PR}})
        self.assertIn('confirmed_pr_merge', request(self.root, 'status')['github_write_operations'])
        for method, params in (
            ('merge_pr', {'key': 'unconfirmed', 'url': PR, 'method': 'squash', 'head_sha': 'a'*40, 'confirmed': False}),
            ('merge_preview', {'url': PR.replace('/42', '/99')}),
            ('merge_result', {'key': 'unknown-merge'}),
            ('intent', {'key': 'forbidden-merge', 'kind': 'merge_pr', 'payload': {'url': PR}})):
            with self.subTest(method=method), self.assertRaises(ValueError):
                request(self.root, method, params)
        self.assertIsNone(request(self.root, 'get_pr', {'url': PR})['merge_request'])

    def test_legacy_cli_routes_to_application_and_does_not_fall_back_when_offline(self):
        args = [sys.executable, str(ROOT/'work_activity.py'), '--state-dir', str(self.root),
                'pr', 'set', PR, '--description', 'Intent from legacy CLI']
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(request(self.root, 'get_pr', {'url': PR})['description'], 'Intent from legacy CLI')
        before = (self.root/'prs.json').read_bytes()
        request(self.root, 'stop')
        self.process.wait(timeout=5)
        offline = subprocess.run(args + ['--project', 'Must not be written'], capture_output=True, text=True)
        self.assertNotEqual(offline.returncode, 0)
        self.assertIn('application unavailable', offline.stderr)
        self.assertEqual((self.root/'prs.json').read_bytes(), before)
        (self.root/'application.json').unlink()
        missing_marker = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(missing_marker.returncode, 0)
        self.assertEqual((self.root/'prs.json').read_bytes(), before)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install requirements.lock for MCP protocol tests')
    def test_official_sdk_stdio_roundtrip_and_intent_boundary(self):
        import anyio
        from mcp import Client, StdioServerParameters
        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                                            args=[str(ROOT/'tracker.py'), '--state-dir', str(self.root), 'mcp'])
            async with Client(params) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertIn('submit_pr', names)
                self.assertNotIn('create_github_pr', names)
                self.assertNotIn('close_pr', names)
                self.assertNotIn('close_result', names)
                self.assertNotIn('merge_pr', names)
                self.assertNotIn('merge_result', names)
                self.assertNotIn('merge_preview', names)
                self.assertNotIn('my_day', names)
                self.assertNotIn('day_intent', names)
                self.assertNotIn('day_targets', names)
                self.assertEqual(len(names), 8)
                payload = {'url': PR, 'intent_key': 'from-agent', 'description': 'Why this matters'}
                result = await client.call_tool('submit_pr', payload)
                self.assertFalse(result.is_error, result.content)
                self.assertFalse(result.structured_content['duplicate'])
                again = await client.call_tool('submit_pr', payload)
                self.assertTrue(again.structured_content['duplicate'])
                session = await client.call_tool('attach_agent_session', {
                    'url': PR, 'intent_key': 'session', 'tool': 'test-agent',
                    'path': '/work/tree', 'sessionId': 'explicit-session-id', 'machine': 'test-host'})
                self.assertFalse(session.is_error, session.content)
                forbidden = await client.call_tool('update_pr', {
                    'url': PR, 'intent_key': 'bad', 'changes': {'github': {'state': 'merged'}}})
                self.assertTrue(forbidden.is_error)
                value = await client.call_tool('get_pr', {'url': PR, 'verify_sessions': True})
                self.assertEqual(value.structured_content['pr']['tracking-outcomes']['pr_registration'], 'registered')
                self.assertEqual(value.structured_content['pr']['tracking-outcomes']['usable_session_actions'], 0)
                self.assertEqual(value.structured_content['pr']['agent-session-actions'][0]['status'], 'unsupported')
                self.assertEqual(value.structured_content['pr']['description'], 'Why this matters')
                self.assertEqual(value.structured_content['pr']['agent-sessions'][0]['sessionId'], 'explicit-session-id')
                self.assertEqual(value.structured_content['pr']['github'], {})
        anyio.run(exercise)
        self.assertIsNone(self.process.poll())
        self.assertGreater(request(self.root, 'status')['pid'], 0)

    @unittest.skipUnless(importlib.util.find_spec('textual'), 'Install requirements.lock for Textual tests')
    def test_sort_choice_survives_tui_restart_through_the_real_application_socket(self):
        import asyncio
        from textual.widgets import Select
        from work_tracker.tui import WorkTrackerApp
        request(self.root, 'intent', {'key': 'sort-fixture', 'kind': 'register_pr', 'payload': {'url': PR}})
        before = request(self.root, 'get_pr', {'url': PR})
        pid = request(self.root, 'status')['pid']
        async def exercise():
            first = WorkTrackerApp(self.root, poll_interval=0, start_view='prs')
            async with first.run_test(size=(100, 32)) as pilot:
                await first.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press('f2')
                await pilot.pause()
                first.screen.query_one('#sort-field', Select).value = 'opened'
                first.screen.query_one('#sort-direction', Select).value = True
                await pilot.click('#sort-apply')
                await pilot.press('q')
            self.assertEqual(request(self.root, 'get_pr_sort'), {'field': 'opened', 'descending': True})
            second = WorkTrackerApp(self.root, poll_interval=0, start_view='prs')
            async with second.run_test(size=(100, 32)) as pilot:
                await second.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual((second.sort_field, second.sort_descending), ('opened', True))
        asyncio.run(exercise())
        self.assertEqual(request(self.root, 'get_pr', {'url': PR}), before)
        self.assertEqual(request(self.root, 'status')['pid'], pid)
        with self.assertRaises(ValueError):
            request(self.root, 'set_pr_sort', {'field': 'unknown', 'descending': False})

    @unittest.skipUnless(importlib.util.find_spec('textual'), 'Install requirements.lock for Textual tests')
    def test_textual_editor_uses_the_real_application_socket_and_does_not_stop_it(self):
        import asyncio
        from textual.widgets import TextArea
        from work_tracker.tui import WorkTrackerApp
        request(self.root, 'intent', {'key': 'ui-fixture', 'kind': 'register_pr',
                                     'payload': {'url': PR, 'description': 'Before UI'}})
        pid = request(self.root, 'status')['pid']
        async def exercise():
            app = WorkTrackerApp(self.root, poll_interval=0, start_view='prs')
            async with app.run_test(size=(140, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(app.connected)
                await pilot.press('e')
                app.screen.query_one('#form-description', TextArea).load_text('Saved through the real socket')
                await pilot.press('ctrl+enter')
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(request(self.root, 'get_pr', {'url': PR})['description'], 'Saved through the real socket')
        asyncio.run(exercise())
        self.assertIsNone(self.process.poll())
        self.assertEqual(request(self.root, 'status')['pid'], pid)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install requirements.lock for MCP protocol tests')
    def test_legacy_mcp_initialize_and_tools_list(self):
        process = subprocess.Popen([sys.executable, str(ROOT/'tracker.py'), '--state-dir', str(self.root), 'mcp'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            def send(value):
                process.stdin.write(json.dumps(value)+'\n')
                process.stdin.flush()
            def receive():
                self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'MCP response timed out')
                return json.loads(process.stdout.readline())
            send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'legacy-test', 'version': '1'}}})
            initialized = receive()
            self.assertEqual(initialized['id'], 1)
            self.assertIn('result', initialized)
            send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            result = receive()
            self.assertEqual(result['id'], 2)
            self.assertIn('submit_pr', {tool['name'] for tool in result['result']['tools']})
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()


if __name__ == '__main__':
    unittest.main()
