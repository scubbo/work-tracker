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
                value = await client.call_tool('get_pr', {'url': PR})
                self.assertEqual(value.structured_content['pr']['description'], 'Why this matters')
                self.assertEqual(value.structured_content['pr']['agent-sessions'][0]['sessionId'], 'explicit-session-id')
                self.assertEqual(value.structured_content['pr']['github'], {})
        anyio.run(exercise)
        self.assertIsNone(self.process.poll())
        self.assertGreater(request(self.root, 'status')['pid'], 0)

    @unittest.skipUnless(importlib.util.find_spec('textual'), 'Install requirements.lock for Textual tests')
    def test_textual_editor_uses_the_real_application_socket_and_does_not_stop_it(self):
        import asyncio
        from textual.widgets import TextArea
        from work_tracker.tui import WorkTrackerApp
        request(self.root, 'intent', {'key': 'ui-fixture', 'kind': 'register_pr',
                                     'payload': {'url': PR, 'description': 'Before UI'}})
        pid = request(self.root, 'status')['pid']
        async def exercise():
            app = WorkTrackerApp(self.root, poll_interval=0)
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
