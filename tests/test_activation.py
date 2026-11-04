from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from work_tracker.installation import activate, check_idle, prepare_python


class ActivationTests(unittest.TestCase):
    def runtime_fixture(self, home):
        python = home/'.local/lib/work-activity/runtime/bin/python'
        python.parent.mkdir(parents=True)
        python.touch()
        source = home/'source'
        source.mkdir()
        (source/'requirements.lock').write_text('mcp==2.3.0\n')
        return source

    def test_matching_installed_dependencies_need_no_registry_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.runtime_fixture(home)
            with mock.patch('work_tracker.installation.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                prepare_python(source, home)
            self.assertFalse(any('install' in call.args[0] for call in run.call_args_list))

    def test_dependency_cache_path_is_strictly_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.runtime_fixture(home)
            def run(args, **kwargs):
                audit = len(args) > 2 and 'importlib.metadata as m' in args[2]
                return subprocess.CompletedProcess(args, 1 if audit else 0)
            with mock.patch('work_tracker.installation.subprocess.run', side_effect=run) as calls, \
                    mock.patch('work_tracker.installation.shutil.which', return_value='/tools/uv'):
                prepare_python(source, home)
            installs = [call.args[0] for call in calls.call_args_list if 'install' in call.args[0]]
            self.assertEqual(len(installs), 1)
            self.assertIn('--offline', installs[0])
            self.assertFalse(any('--index-url' in args for args in installs))

    def test_idle_check_ignores_nested_resource_coalition_counts(self):
        result = subprocess.CompletedProcess([], 0, stdout='\tactive count = 0\n\tstate = not running\n\tresource coalition = {\n\t\tactive count = 1\n\t}\n')
        with mock.patch('work_tracker.installation.subprocess.run', return_value=result), \
                mock.patch('work_tracker.installation.request', side_effect=RuntimeError('Not running')):
            check_idle(Path('/tmp/not-an-installation'), 'legacy')

    def test_idle_check_rejects_actual_running_collection(self):
        result = subprocess.CompletedProcess([], 0, stdout='\tactive count = 1\n\tstate = running\n')
        with mock.patch('work_tracker.installation.subprocess.run', return_value=result):
            with self.assertRaises(RuntimeError):
                check_idle(Path('/tmp/not-an-installation'), 'legacy')

    def test_legacy_calendar_is_retired_only_after_new_application_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = home/'Library/LaunchAgents/com.scubbo.work-activity.plist'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'legacy plist backup')
            events = []
            unloaded = False
            post_unload_reads = 0
            def run(args, **kwargs):
                nonlocal unloaded, post_unload_reads
                events.append(args)
                if args[:2] == ['launchctl', 'bootout'] and args[-1].endswith('/com.scubbo.work-tracker'):
                    unloaded = True
                if args[:2] == ['launchctl', 'print'] and args[-1].endswith('/com.scubbo.work-tracker') and unloaded:
                    post_unload_reads += 1
                    return subprocess.CompletedProcess(args, 1 if post_unload_reads >= 2 else 0)
                return subprocess.CompletedProcess(args, 0)
            def health(root, method):
                events.append('ready')
                return {'storage': 'sqlite', 'jobs': []}
            with mock.patch('work_tracker.installation.subprocess.run', side_effect=run), \
                    mock.patch('work_tracker.installation.datastore_free', side_effect=[False, True, True]):
                activate(home/'new.plist', home, 'com.scubbo.work-tracker', 'com.scubbo.work-activity',
                         health=health, sleep=lambda value: None)
            self.assertGreaterEqual(post_unload_reads, 3)
            retired = next(index for index, event in enumerate(events) if isinstance(event, list)
                           and event[:2] == ['launchctl', 'bootout'] and event[-1].endswith('/com.scubbo.work-activity'))
            self.assertLess(events.index('ready'), retired)
            self.assertFalse(old.exists())
            backups = list((home/'.local/state/work-activity/backups').glob('*.plist'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'legacy plist backup')
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_readiness_failure_does_not_remove_legacy_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old = home/'Library/LaunchAgents/com.scubbo.work-activity.plist'
            old.parent.mkdir(parents=True)
            old.write_bytes(b'original')
            health_checks = 0
            def unavailable(root, method):
                nonlocal health_checks
                health_checks += 1
                raise RuntimeError('Unavailable')
            def commands(args, **kwargs):
                missing = args[:2] == ['launchctl', 'print'] and args[-1].endswith('/new')
                return subprocess.CompletedProcess(args, 1 if missing else 0)
            with mock.patch('work_tracker.installation.subprocess.run', side_effect=commands) as run:
                with self.assertRaises(RuntimeError):
                    activate(home/'new.plist', home, 'new', 'com.scubbo.work-activity',
                             health=unavailable, sleep=lambda value: None)
            self.assertEqual(old.read_bytes(), b'original')
            self.assertEqual(health_checks, 100)
            self.assertFalse(any(call.args[0][:2] == ['launchctl', 'bootout']
                                 and call.args[0][-1].endswith('/com.scubbo.work-activity') for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
