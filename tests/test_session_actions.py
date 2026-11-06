from copy import deepcopy
import io
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

import work_activity as activity
from work_tracker import cli
from work_tracker.service import Application
from work_tracker.session_actions import SessionActions, inspect_session, references


PR = 'https://github.com/acme/service/pull/42'


class SessionActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace with 'quotes' and $(touch nope)"
        self.workspace.mkdir()
        self.session = {'tool': 'fx', 'path': str(self.workspace), 'sessionId': 'SavedSession123', 'machine': 'test.local'}
        self.inspect = mock.Mock(return_value={'id': 'SavedSession123'})
        self.actions = SessionActions(machine='test.local', which=lambda tool: '/bin/'+tool, inspect=self.inspect)

    def test_fx_action_requires_native_verification_and_preserves_workspace(self):
        pending = self.actions.action(self.session)
        self.assertEqual(pending['status'], 'unverified')
        self.assertFalse(pending['usable_locally'])
        self.inspect.assert_not_called()
        ready = self.actions.action(self.session, verify=True)
        self.assertEqual(ready['status'], 'ready')
        self.assertTrue(ready['usable_locally'])
        self.assertTrue(ready['verified_at'])
        self.assertEqual(shlex.split(ready['command']), ['cd', '--', str(self.workspace), '&&', 'fx', '--resume', 'SavedSession123'])
        self.inspect.assert_called_once_with(['/bin/fx', 'session', '--id', 'SavedSession123', '--json'], str(self.workspace))
        self.assertEqual(self.actions.action(self.session)['status'], 'ready')

    def test_fx_exact_ids_include_leading_hyphens_and_cannot_become_subcommands(self):
        for sid in ('-Saved123', '--Saved123', '_Saved123', 'resume', 'recover'):
            with self.subTest(sid=sid):
                self.inspect.reset_mock()
                self.inspect.return_value = {'id': sid}
                value = self.actions.action({**self.session, 'sessionId': sid}, verify=True)
                self.assertEqual(value['status'], 'ready')
                self.inspect.assert_called_once_with(['/bin/fx', 'session', '--id', sid, '--json'], str(self.workspace))
                resume = ['fx', 'session', 'resume', '--id', sid] if sid.startswith('-') else ['fx', '--resume', sid]
                self.assertEqual(shlex.split(value['command']), ['cd', '--', str(self.workspace), '&&', *resume])

    def test_other_machine_has_instructions_not_a_local_action_and_never_probes(self):
        value = self.actions.action({**self.session, 'machine': 'remote.local'}, verify=True)
        self.assertEqual(value['status'], 'other_machine')
        self.assertIn('remote.local', value['reason'])
        self.assertFalse(value['usable_locally'])
        self.assertFalse(value['can_verify'])
        self.assertIn('fx --resume SavedSession123', value['command'])
        self.inspect.assert_not_called()

    def test_unknown_tool_incomplete_identity_and_unsafe_arguments_have_no_action(self):
        cases = [({**self.session, 'tool': 'unknown-agent'}, 'unsupported'),
                 ({**self.session, 'sessionId': ''}, 'incomplete'),
                 ({**self.session, 'path': 'relative'}, 'unsafe'),
                 ({**self.session, 'sessionId': '--continue --auto'}, 'unsafe'),
                 ({**self.session, 'sessionId': 'last'}, 'unsafe'),
                 ({**self.session, 'sessionId': 'abc; touch /tmp/marker'}, 'unsafe'),
                 ({**self.session, 'machine': 'test.local\ncommand'}, 'unsafe')]
        for session, expected in cases:
            with self.subTest(session=session):
                value = self.actions.action(session, verify=True)
                self.assertEqual(value['status'], expected)
                self.assertIsNone(value['command'])
                self.assertFalse(value['usable_locally'])
        self.inspect.assert_not_called()

    def test_missing_workspace_and_missing_tool_are_distinct(self):
        value = self.actions.action({**self.session, 'path': str(self.workspace/'absent')}, verify=True)
        self.assertEqual(value['status'], 'missing_workspace')
        self.actions.which = lambda tool: None
        self.assertEqual(self.actions.action(self.session, verify=True)['status'], 'tool_unavailable')
        self.inspect.assert_not_called()

    def test_failed_or_wrong_native_lookup_is_not_a_usable_link(self):
        for response in ({'id': 'DifferentSession'}, None):
            self.inspect.return_value = response
            self.assertEqual(self.actions.action(self.session, verify=True)['status'], 'unavailable')
        self.inspect.side_effect = OSError('tool failed')
        self.assertFalse(self.actions.action(self.session, verify=True)['usable_locally'])

    def test_opencode_legacy_reference_ignores_stored_command_and_checks_native_workspace(self):
        old = {'id': 'ses_Example123', 'directory': str(self.workspace), 'machine': 'test.local',
               'command': 'arbitrary stored shell text must not run', 'method': 'manual'}
        record = {'source-session': old, 'agent-sessions': []}
        original = deepcopy(record)
        self.inspect.return_value = {'data': {'id': old['id'], 'location': {'directory': str(self.workspace)}}}
        value = self.actions.project(record, verify=True)
        action = value['agent-session-actions'][0]
        self.assertEqual(action['status'], 'ready')
        self.assertIn('opencode --session ses_Example123', action['command'])
        self.assertNotIn('arbitrary', action['command'])
        self.inspect.assert_called_once_with(['/bin/opencode', 'api', 'session.get', '--param', 'sessionID=ses_Example123'], str(self.workspace))
        self.assertEqual(record, original)
        self.inspect.return_value['data']['location']['directory'] = str(self.workspace/'other')
        self.assertEqual(self.actions.project(record, verify=True)['agent-session-actions'][0]['status'], 'unavailable')

    def test_legacy_and_generic_references_deduplicate_without_rewriting_provenance(self):
        old = {'id': 'ses_Example123', 'directory': str(self.workspace), 'machine': 'test.local'}
        generic = {'tool': 'opencode', 'sessionId': old['id'], 'path': old['directory'], 'machine': old['machine']}
        self.assertEqual(references({'agent-sessions': [generic, generic], 'source-session': old}), [generic])

    def test_api_receipts_and_reads_separate_registration_attachment_and_usable_action(self):
        app = Application(Path(self.temp.name)/'state')
        app.session_actions = self.actions
        register = app.dispatch('intent', {'key': 'register', 'kind': 'register_pr', 'payload': {'url': PR}})
        self.assertEqual(register['tracking-outcomes']['pr_registration'], 'registered')
        self.assertEqual(register['tracking-outcomes']['session_references'], 0)
        payload = {'url': PR, 'session': self.session}
        for key in ('attach', 'attach', 'attach-again'):
            attached = app.dispatch('intent', {'key': key, 'kind': 'attach_session', 'payload': payload})
            self.assertEqual(attached['session_reference'], 'attached')
            self.assertEqual(attached['tracking-outcomes']['session_references'], 1)
            self.assertEqual(attached['tracking-outcomes']['usable_session_actions'], 0)
        self.inspect.assert_not_called()
        verified = app.dispatch('get_pr', {'url': PR, 'verify_sessions': True})
        self.assertEqual(verified['tracking-outcomes']['usable_session_actions'], 1)
        self.assertEqual(verified['agent-sessions'], [self.session])
        exported = app.storage.export_registry()[PR]
        self.assertNotIn('agent-session-actions', exported)
        self.assertNotIn('tracking-outcomes', exported)
        self.assertEqual(app.dispatch('legacy', {'action': 'pr', 'pr_action': 'show', 'url': PR})['agent-session-actions'][0]['status'], 'ready')
        self.assertEqual(app.dispatch('list_prs', {})['items'][0]['agent-session-actions'][0]['status'], 'ready')
        with self.assertRaises(ValueError):
            app.dispatch('get_pr', {'url': PR, 'verify_sessions': 'yes'})

    def test_cli_reports_action_status_as_json(self):
        projected = self.actions.project({'url': PR, 'agent-sessions': [self.session]}, verify=True)
        with mock.patch.object(cli, 'request', return_value=projected) as request, mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['sessions', PR, '--verify']), 0)
            self.assertEqual(json.loads(output.getvalue())['agent-session-actions'][0]['status'], 'ready')
            self.assertEqual(request.call_args.args[2], {'url': PR, 'verify_sessions': True})

    def test_native_inspection_never_uses_shell_and_returns_only_json_to_resolver(self):
        def run(argv, **kwargs):
            self.assertEqual(kwargs['cwd'], str(self.workspace))
            self.assertNotIn('shell', kwargs)
            kwargs['stdout'].write(b'{"id":"SavedSession123"}')
            return mock.Mock(returncode=0)
        with mock.patch('work_tracker.session_actions.subprocess.run', side_effect=run):
            self.assertEqual(inspect_session(['/bin/fx', 'session', 'SavedSession123', '--json'], str(self.workspace)), {'id': 'SavedSession123'})


if __name__ == '__main__':
    unittest.main()
