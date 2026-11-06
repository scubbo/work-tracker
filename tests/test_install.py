import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest
import subprocess
import sys
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def source(self, home):
        source = home / "source"
        source.mkdir()
        (source / "work_activity.py").write_text("print('collector')\n")
        (source / "tracker.py").write_text("print('application')\n")
        (source / "work_tracker").mkdir()
        (source / "work_tracker/__init__.py").write_text("")
        return source

    def test_launch_agent_supervises_application_without_owning_job_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            plist = installer.install(source, home, "/usr/bin/python3", {"gh": "/usr/bin/gh"}, "/usr/bin")
            config = plistlib.loads(plist.read_bytes())
            self.assertNotIn("StartCalendarInterval", config)
            self.assertNotIn("StartInterval", config)
            self.assertEqual(config["ProgramArguments"][-1], "serve")
            self.assertTrue(config["RunAtLoad"])
            self.assertEqual(config["KeepAlive"], {"SuccessfulExit": False})
            self.assertTrue((home / ".local/bin/work-tracker").is_file())
            self.assertNotIn("TZ", config.get("EnvironmentVariables", {}))
            self.assertTrue((home / ".local/bin/work-activity").is_file())
            self.assertTrue((home / ".local/lib/work-activity/work_activity.py").is_file())
            self.assertEqual(Path(config["StandardErrorPath"]).stat().st_mode & 0o777, 0o600)
            self.assertNotIn(str(source), str(config))

    def test_installs_companion_skill_without_changing_existing_daily_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            summary = home / ".agents/skills/daily-summary/SKILL.md"
            summary.parent.mkdir(parents=True)
            summary.write_text("existing summary instructions")
            registration = b'# Register a PR with work-tracker\n'
            installer.install(source, home, "/usr/bin/python3", {}, "/usr/bin", b"# Work activity\n", registration)
            self.assertEqual((home / '.agents/skills/register-pr/SKILL.md').read_bytes(), registration)
            self.assertEqual(summary.read_text(), "existing summary instructions")
            self.assertEqual((home / ".agents/skills/work-activity/SKILL.md").read_text(), "# Work activity\n")

    def test_refuses_to_replace_unrelated_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            existing = home / ".agents/skills/work-activity/SKILL.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("user instructions")
            with self.assertRaises(ValueError):
                installer.install(source, home, "/usr/bin/python3", {}, "/usr/bin", b"# Work activity\n")
            self.assertEqual(existing.read_text(), "user instructions")
            self.assertFalse((home / ".local/bin/work-activity").exists())

    def test_refuses_to_replace_unrelated_registration_skill_before_installing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            existing = home / '.agents/skills/register-pr/SKILL.md'
            existing.parent.mkdir(parents=True)
            existing.write_text('User-owned registration skill')
            with self.assertRaises(ValueError):
                installer.install(source, home, sys.executable, {}, '/usr/bin',
                                  b'# Work activity\n', b'# Register a PR with work-tracker\n')
            self.assertEqual(existing.read_text(), 'User-owned registration skill')
            self.assertFalse((home / '.local/bin/work-tracker').exists())
            self.assertFalse((home / '.agents/skills/work-activity/SKILL.md').exists())

    def test_refuses_registration_skill_file_and_directory_symlinks(self):
        for directory in (False, True):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                source = self.source(home)
                destination = home / '.agents/skills/register-pr/SKILL.md'
                external = home / 'external/SKILL.md'
                external.parent.mkdir()
                content = '# Register a PR with work-tracker\nExternal copy\n'
                external.write_text(content)
                destination.parent.parent.mkdir(parents=True)
                if directory:
                    destination.parent.symlink_to(external.parent, target_is_directory=True)
                else:
                    destination.parent.mkdir()
                    destination.symlink_to(external)
                with self.assertRaises(ValueError):
                    installer.install(source, home, sys.executable, {}, '/usr/bin',
                                      registration_skill=b'# Register a PR with work-tracker\nReplacement\n')
                self.assertEqual(external.read_text(), content)
                self.assertFalse((home / '.local/bin/work-tracker').exists())

    def test_reinstall_updates_an_owned_registration_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            for version in ('First', 'Updated'):
                content = f'# Register a PR with work-tracker\n{version}\n'.encode()
                installer.install(source, home, sys.executable, {}, '/usr/bin', registration_skill=content)
                self.assertEqual((home / '.agents/skills/register-pr/SKILL.md').read_bytes(), content)

    def test_reinstall_updates_owned_fx_skill_and_refuses_unrelated_shadow_copy(self):
        for owned in (True, False):
            with self.subTest(owned=owned), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                source = self.source(home)
                destination = home / '.fx/skills/register-pr/SKILL.md'
                destination.parent.mkdir(parents=True)
                previous = '# Register a PR with work-tracker\nOld\n' if owned else 'Unrelated user skill'
                destination.write_text(previous)
                updated = b'# Register a PR with work-tracker\nUpdated resume verification\n'
                if owned:
                    installer.install(source, home, sys.executable, {}, '/usr/bin', registration_skill=updated)
                    self.assertEqual(destination.read_bytes(), updated)
                    self.assertEqual((home / '.agents/skills/register-pr/SKILL.md').read_bytes(), updated)
                else:
                    with self.assertRaises(ValueError):
                        installer.install(source, home, sys.executable, {}, '/usr/bin', registration_skill=updated)
                    self.assertEqual(destination.read_text(), previous)
                    self.assertFalse((home / '.local/bin/work-tracker').exists())

    def test_main_installs_from_standalone_checkout_without_source_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(installer.Path, "home", return_value=home), \
                    mock.patch.object(installer.sys, "platform", "darwin"), \
                    mock.patch.object(installer.sys, "argv", [str(ROOT / "install.py")]), \
                    mock.patch.object(installer.shutil, "which", side_effect=lambda name: f"/tools/{name}"), \
                    mock.patch('work_tracker.installation.prepare_python', return_value='/tools/python3'), \
                    mock.patch.object(installer.subprocess, "run") as run:
                installer.main()
            self.assertEqual((home / ".local/lib/work-activity/work_activity.py").read_bytes(),
                             (ROOT / "work_activity.py").read_bytes())
            self.assertEqual((home / ".local/lib/work-activity/ACTIVITY.md").read_bytes(),
                             (ROOT / "ACTIVITY.md").read_bytes())
            self.assertEqual((home / ".agents/skills/work-activity/SKILL.md").read_bytes(),
                             (ROOT / "skills/work-activity/SKILL.md").read_bytes())
            self.assertEqual((home / '.agents/skills/register-pr/SKILL.md').read_bytes(),
                             (ROOT / 'skills/register-pr/SKILL.md').read_bytes())
            plist = plistlib.loads((home / 'Library/LaunchAgents' / f'{installer.LABEL}.plist').read_bytes())
            installed_path = plist['EnvironmentVariables']['PATH']
            for directory in ('/usr/bin', '/bin', '/usr/sbin', '/sbin'):
                self.assertIn(directory, installed_path.split(':'))
            import json
            collector_config = json.loads((home / '.config/work-activity/config.json').read_text())
            self.assertEqual(collector_config['path'], installed_path)
            run.assert_called_once_with(["plutil", "-lint", str(home / "Library/LaunchAgents" / f"{installer.LABEL}.plist")],
                                        check=True)

    def test_reinstall_preserves_private_data_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            state = home / ".local/state/work-activity"
            state.mkdir(parents=True)
            names = ("prs.json", "checkpoints.json", "correlations.json")
            for name in names:
                (state / name).write_bytes(b'{"existing": "private data"}\n')
            installer.install(source, home, sys.executable, {}, "/usr/bin")
            for name in names:
                self.assertEqual((state / name).read_bytes(), b'{"existing": "private data"}\n')

    def test_installed_launcher_does_not_require_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = self.source(home)
            program = source / "work_activity.py"
            program.write_text("import sys\nprint(sys.argv[1])\n")
            installer.install(source, home, sys.executable, {}, "/usr/bin")
            program.unlink()
            result = subprocess.run([str(home / ".local/bin/work-activity"), "status"],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "status\n")


if __name__ == "__main__":
    unittest.main()
