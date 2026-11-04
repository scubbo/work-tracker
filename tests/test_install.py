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
            installer.install(source, home, "/usr/bin/python3", {}, "/usr/bin", b"# Work activity\n")
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
