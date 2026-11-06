from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from work_tracker.herdr_sessions import HerdrSessions, read_output
from work_tracker.service import Application
from work_tracker.session_actions import SessionActions


REF = {'tool': 'fx', 'path': '/tmp/workspace', 'sessionId': '-Saved123', 'machine': 'test.local'}
PR = 'https://github.com/acme/service/pull/42'
HOME = str(Path(tempfile.gettempdir()).resolve() / 'herdr-test-home')


def pane(reference=REF, pane_id='w1:p1', reported=True):
    value = {'pane_id': pane_id, 'agent': reference['tool'], 'cwd': reference['path']}
    if reported:
        value['agent_session'] = {'kind': 'id', 'agent': reference['tool'], 'value': reference['sessionId']}
    return value


class Native:
    def __init__(self, panes=None):
        self.sessions = [{'name': 'default', 'running': True}]
        self.panes = {'default': panes if panes is not None else [pane()]}
        self.calls = []
        self.focused = None
        self.files = f'p12\nf4\nn{HOME}/.fx/sessions/v2/-Saved123/lock\nf5\nn{HOME}/.fx/sessions/v2/-Saved123/log.jsonl\n'
        self.snapshots = 0
        self.change = False
        self.fail = False
        self.process_cwd = REF['path']

    def __call__(self, argv, timeout, env=None, allow_empty=False):
        self.calls.append(argv)
        if self.fail:
            raise subprocess.TimeoutExpired(argv, timeout)
        if argv[0] == '/bin/lsof':
            return self.files
        assert not any(key.startswith('HERDR_') for key in env)
        assert 0 < timeout <= 2
        if argv[1:4] == ['session', 'list', '--json']:
            return json.dumps({'sessions': self.sessions})
        assert argv[1] == '--session'
        name, args = argv[2], argv[3:]
        if args == ['api', 'snapshot']:
            self.snapshots += 1
            values = deepcopy(self.panes[name])
            if self.change and self.snapshots > 1:
                values[0]['agent_session']['value'] = 'Replaced123'
            return json.dumps({'result': {'snapshot': {'agents': values, 'focused_pane_id': self.focused}}})
        if args[:2] == ['pane', 'process-info']:
            return json.dumps({'result': {'process_info': {'pane_id': args[-1], 'foreground_processes': [
                {'pid': 12, 'argv0': '/bin/fx', 'argv': ['fx'], 'cwd': self.process_cwd}]}}})
        assert args[:2] == ['agent', 'focus'], args
        self.focused = args[2]
        # Captured CLI contract: agent focus returns agent_info, not pane_focused.
        agent = next(value for value in self.panes[name] if value['pane_id'] == args[2])
        receipt = {key: value for key, value in agent.items() if key != 'agent_session'}
        return json.dumps({'result': {'type': 'agent_info', 'agent': {**receipt, 'focused': True}}})


class HerdrSessionTests(unittest.TestCase):
    def adapter(self, native):
        return HerdrSessions(machine='test.local', home=HOME, which=lambda name: '/bin/'+name, run=native)

    def assert_no_focus(self, native):
        self.assertFalse(any('focus' in call for call in native.calls))

    def test_reported_exact_identity_focuses_and_confirms_the_explicit_local_session(self):
        for tool, sid in (('fx', '-Saved123'), ('opencode', 'ses_Saved123'), ('claude', 'uuid-123')):
            with self.subTest(tool=tool):
                reference = {**REF, 'tool': tool, 'sessionId': sid}
                native = Native([pane(reference)])
                with mock.patch.dict(os.environ, {'HERDR_SOCKET_PATH': '/unrelated/remote.sock', 'HERDR_PANE_ID': 'other'}):
                    value = self.adapter(native).activate(reference)
                self.assertEqual(value['status'], 'activated')
                self.assertIn(['/bin/herdr', '--session', 'default', 'agent', 'focus', 'w1:p1'], native.calls)
                self.assertFalse(any(call[0] == '/bin/lsof' for call in native.calls))

    def test_wrong_or_unfocused_native_receipts_do_not_claim_success(self):
        receipts = [
            {'type': 'agent_info', 'agent': {'pane_id': 'w9:p9', 'agent': 'fx', 'focused': True}},
            {'type': 'agent_info', 'agent': {'pane_id': 'w1:p1', 'agent': 'fx', 'focused': False}},
            {'type': 'agent_info', 'agent': {'pane_id': 'w1:p1', 'agent': 'fx', 'focused': 'true'}},
            {'type': 'agent_info', 'agent': {'pane_id': 'w1:p1', 'agent': 'opencode', 'focused': True}},
            {'type': 'agent_info', 'agent': None},
            {'type': 'pane_focused', 'pane_id': 'w1:p1'},
        ]
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                native = Native()
                def run(argv, **kwargs):
                    text = native(argv, **kwargs)
                    return json.dumps({'result': receipt}) if argv[3:5] == ['agent', 'focus'] else text
                adapter = self.adapter(native)
                adapter.run = run
                self.assertEqual(adapter.activate(REF)['status'], 'unavailable')
                self.assertEqual(sum('focus' in call for call in native.calls), 1)

    def test_valid_receipt_still_requires_the_target_to_be_selected(self):
        native = Native()
        def run(argv, **kwargs):
            value = json.loads(native(argv, **kwargs))
            if 'focus' not in argv and native.focused and argv[-2:] == ['api', 'snapshot']:
                value['result']['snapshot']['focused_pane_id'] = 'w9:p9'
            return json.dumps(value)
        adapter = self.adapter(native)
        adapter.run = run
        self.assertEqual(adapter.activate(REF)['status'], 'unavailable')
        self.assertEqual(sum('focus' in call for call in native.calls), 1)

    def test_fx_with_plain_argv_matches_two_session_files_in_one_live_process(self):
        native = Native([pane(reported=False)])
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'activated')
        self.assertEqual(sum(call[0] == '/bin/lsof' for call in native.calls), 2)
        self.assertFalse(any('resume' in call or 'send-keys' in call or 'run' in call for call in native.calls))

    def test_fx_instances_and_child_commands_do_not_require_the_process_cwd_to_match_every_loaded_session(self):
        native = Native([pane(reported=False)])
        native.process_cwd = '/tmp/another-instance'
        native.panes['default'][0]['foreground_cwd'] = '/tmp/child-command'
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'activated')

    def test_untrusted_native_handles_cannot_become_cli_options_or_shell_text(self):
        for pane_id in ('--other-option', 'w1:p1;touch nope', 'w1:p1\n'):
            native = Native([pane(pane_id=pane_id)])
            self.assertEqual(self.adapter(native).activate(REF)['status'], 'unavailable')
            self.assert_no_focus(native)
        native = Native()
        native.sessions[0]['name'] = '--remote'
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'unavailable')
        self.assert_no_focus(native)

    def test_partial_files_cross_process_files_and_titles_are_not_identity_proof(self):
        for files in (f'p12\nn{HOME}/.fx/sessions/v2/-Saved123/log.jsonl\n',
                      f'p12\nn{HOME}/.fx/sessions/v2/-Saved123/lock\np13\nn{HOME}/.fx/sessions/v2/-Saved123/log.jsonl\n',
                      f'p12\nn{HOME}/.fx/sessions/v2/index.lock\n', ''):
            with self.subTest(files=files):
                native = Native([pane(reported=False)])
                native.panes['default'][0]['terminal_title'] = '-Saved123'
                native.files = files
                self.assertEqual(self.adapter(native).activate(REF)['status'], 'not_found')
                self.assert_no_focus(native)

    def test_wrong_tool_workspace_or_explicit_session_prevents_focus(self):
        for update in ({'agent': 'opencode'}, {'cwd': '/tmp/different'},
                       {'agent_session': {'kind': 'id', 'agent': 'fx', 'value': 'Different123'}}):
            native = Native([{**pane(), **update}])
            self.assertEqual(self.adapter(native).activate(REF)['status'], 'not_found')
            self.assert_no_focus(native)

    def test_remote_incomplete_and_unsafe_references_never_run_a_command(self):
        native = Native()
        for update in ({'machine': 'another.local'}, {'sessionId': ''}, {'sessionId': '../escape'},
                       {'sessionId': 'last'}, {'sessionId': 'abc; touch nope'}, {'path': 'relative'},
                       {'tool': 'fx\ncommand'}, {'machine': 'test.local\n'}):
            self.assertEqual(self.adapter(native).activate({**REF, **update})['status'], 'unavailable')
        self.assertEqual(native.calls, [])

    def test_scans_every_running_named_session_and_ignores_stopped_sessions(self):
        native = Native([])
        native.sessions.extend([{'name': 'work', 'running': True}, {'name': 'stopped', 'running': False}])
        native.panes['work'] = [pane()]
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'activated')
        self.assertIn(['/bin/herdr', '--session', 'work', 'agent', 'focus', 'w1:p1'], native.calls)
        self.assertFalse(any('stopped' in call for call in native.calls))

    def test_ambiguous_sessions_and_changed_occupants_never_focus_arbitrarily(self):
        native = Native([pane(), pane(pane_id='w1:p2')])
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'ambiguous')
        self.assert_no_focus(native)
        native = Native()
        native.change = True
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'not_found')
        self.assert_no_focus(native)

    def test_failures_and_missing_tools_do_not_claim_activation(self):
        native = Native()
        native.fail = True
        self.assertEqual(self.adapter(native).activate(REF)['status'], 'unavailable')
        self.assert_no_focus(native)
        adapter = HerdrSessions(machine='test.local', which=lambda name: None, run=native)
        self.assertEqual(adapter.activate(REF)['status'], 'unavailable')
        native = Native([pane(reported=False)])
        adapter = self.adapter(native)
        adapter.which = lambda name: None if name == 'lsof' else '/bin/herdr'
        result = adapter.activate(REF)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('cannot find lsof', result['reason'])
        self.assert_no_focus(native)

    def test_daemon_path_missing_system_sbin_is_not_reported_as_an_absent_pane(self):
        for path, expected in (('/usr/bin:/bin', 'unavailable'), ('/usr/bin:/bin:/usr/sbin:/sbin', 'found')):
            with self.subTest(path=path), mock.patch.dict(os.environ, {'PATH': path}):
                native = Native([pane(reported=False)])
                adapter = self.adapter(native)
                adapter.which = lambda name: None if name == 'lsof' and '/usr/sbin' not in os.environ['PATH'].split(':') else '/bin/'+name
                result = adapter.find(REF)
                self.assertEqual(result['status'], expected)
                if expected == 'unavailable':
                    self.assertIn('PATH', result['reason'])
                self.assert_no_focus(native)

    def test_read_only_discovery_never_focuses(self):
        native = Native()
        self.assertEqual(self.adapter(native).find(REF)['status'], 'found')
        self.assert_no_focus(native)

    def test_rpc_requires_an_attached_exact_reference_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(root)
            native = Native()
            app.herdr_sessions = self.adapter(native)
            for key, kind, payload in (
                ('register', 'register_pr', {'url': PR}),
                ('attach', 'attach_session', {'url': PR, 'session': REF})):
                app.dispatch('intent', {'key': key, 'kind': kind, 'payload': payload})
            before = deepcopy(app.storage.export_registry())
            for params in ({'url': PR, 'reference': {**REF, 'sessionId': 'Other'}},
                           {'url': PR, 'reference': REF, 'command': 'arbitrary shell'},
                           {'url': PR, 'reference': {**REF, 'command': 'arbitrary shell'}}):
                for method in ('activate_session_pane', 'find_session_pane'):
                    with self.assertRaises(ValueError): app.dispatch(method, params)
            self.assertEqual(native.calls, [])
            self.assertEqual(app.dispatch('find_session_pane', {'url': PR, 'reference': REF})['status'], 'found')
            self.assert_no_focus(native)
            for _ in range(2):
                self.assertEqual(app.dispatch('activate_session_pane', {'url': PR, 'reference': REF})['status'], 'activated')
            self.assertEqual(app.storage.export_registry(), before)

    def test_projection_exposes_local_activation_separately_from_resume_support(self):
        actions = SessionActions(machine='test.local', which=lambda name: '/bin/'+name, is_directory=lambda path: False)
        value = actions.action(REF)
        self.assertTrue(value['can_activate_pane'])
        self.assertEqual(value['status'], 'missing_workspace')
        self.assertFalse(actions.action({**REF, 'machine': 'remote.local'})['can_activate_pane'])

    def test_native_capture_is_bounded_and_never_uses_a_shell(self):
        def run(argv, **kwargs):
            self.assertNotIn('shell', kwargs)
            self.assertEqual(kwargs['timeout'], 2)
            kwargs['stdout'].write(b'{"sessions":[]}')
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch('work_tracker.herdr_sessions.subprocess.run', side_effect=run):
            self.assertEqual(json.loads(read_output(['/bin/herdr', 'session', 'list', '--json'], 2)), {'sessions': []})


if __name__ == '__main__':
    unittest.main()
