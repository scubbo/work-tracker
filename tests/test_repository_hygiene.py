from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('git'), 'Git is required to test ignore rules')
class RepositoryHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copyfile(ROOT/'.gitignore', self.root/'.gitignore')
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True, capture_output=True)

    def ignored(self, paths):
        result = subprocess.run(
            ['git', '-c', 'core.excludesFile=/dev/null', 'check-ignore', '--no-index', '-z', '--stdin'],
            input=b'\0'.join(path.encode() for path in paths)+b'\0',
            cwd=self.root, capture_output=True)
        self.assertIn(result.returncode, (0, 1), result.stderr.decode())
        return {path.decode() for path in result.stdout.split(b'\0') if path}

    def test_private_state_credentials_and_scan_reports_are_ignored(self):
        paths = [
            '.env', '.env.local', '.venv/bin/python', '.local/state/prs.json',
            'state/prs.json', 'custom-state/prs.json', 'custom-state/checkpoints.json',
            'custom-state/correlations.json', 'custom-state/application.json',
            'custom-state/app-config.json', 'custom-state/.lock',
            'custom-state/.activity-temporary', 'custom-state/app.sock',
            'custom-state/tracker.sqlite3', 'custom-state/tracker.sqlite3-wal',
            'custom-state/tracker.sqlite3-shm', 'custom-state/backup.sqlite',
            'custom-state/backup.sqlite-journal', 'custom-state/backup.db',
            'custom-state/backup.db-wal', 'custom-state/backups/original.json',
            'custom-state/snapshots/day/slack.json', 'custom-state/transcripts/session.json.gz',
            'custom-state/logs/stdout.log', 'debug.log', 'credentials.json',
            '.credentials/token', '.aws/credentials', '.ssh/id_ed25519',
            'id_rsa', 'id_ed25519', 'signing.key', 'client.pem', 'client.p12',
            'client.pfx', 'client.keystore', '.netrc', '.npmrc', '.pypirc',
            'gitleaks-report.json', 'gitleaks-report.sarif',
            'events.jsonl', 'events.jsonl.gz', 'export.ndjson', 'daily-stats.csv',
            'project-stats.tsv', 'stats.parquet', 'stats.arrow', 'stats.json',
            'statistics.json', 'daily-stats.json', 'stats-history.json', 'metrics.json',
            'custom-state/stats.json', 'custom-state/statistics.json',
        ]
        self.assertEqual(self.ignored(paths), set(paths))

    def test_source_docs_tests_and_example_environment_are_not_ignored(self):
        paths = ['.gitignore', '.env.example', 'README.md', 'AGENTS.md', 'requirements.lock',
                 'tracker.py', 'work_activity.py', 'work_tracker/storage.py',
                 'tests/test_repository_hygiene.py', 'docs/requirements.md',
                 'skills/work-activity/SKILL.md']
        self.assertEqual(self.ignored(paths), set())


if __name__ == '__main__':
    unittest.main()
