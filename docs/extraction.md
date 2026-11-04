# Standalone repository extraction

The collector previously lived in the `work-tracker` worktree of scubbo-scripts,
under `daily-summary`. The installer and companion skill lived in the
`work-tracker` worktree of dotfiles.

The standalone checkout is `~/Code/work-tracker`.
The source files now belong to this repository:

| Previous location | New location |
| --- | --- |
| `scubbo-scripts/daily-summary/work_activity.py` | `work_activity.py` |
| `scubbo-scripts/daily-summary/tests/test_work_activity.py` | `tests/test_work_activity.py` |
| `scubbo-scripts/daily-summary/ACTIVITY.md` | `ACTIVITY.md` |
| `dotfiles/work-activity/install.py` | `install.py` |
| `dotfiles/work-activity/test_install.py` | `tests/test_install.py` |
| `dotfiles/opencode/.config/opencode/skills/work-activity/SKILL.md` | `skills/work-activity/SKILL.md` |

Six exact source copies were made before repository-specific edits.
Recovery copies and their SHA-256 hashes were retained in a private temporary
directory. They contain source code, not private collected activity.

The collector implementation and its tests retain their behavior.
The installer now resolves its default source and skill from this checkout.
Its tests use the standalone directory layout.
The old repositories retain documentation pointers rather than duplicate
implementations. The daily-summary integration continues to use the installed
CLI, skill, and guide.

No private state is moved into the repository. The installed command remains
`~/.local/bin/work-activity`. The installed program remains
`~/.local/lib/work-activity/work_activity.py`. The LaunchAgent label remains
`com.scubbo.work-activity`, with its daily 10:00 local-time schedule.

This setup does not implement the TUI, create a SQLite database, select a remote
backend, start a managed coordinator, commit, or publish a repository.

## Verification

Run the tests from the standalone checkout:

```sh
python3 -m unittest discover -s tests -v
```

Install from the new checkout with:

```sh
python3 install.py --load
~/.local/bin/work-activity status
```

Verification on 2026-11-03 passed:

- `python3 -m unittest discover -s tests -v`: 30 tests passed, exit 0.
- `python3 install.py --load`: installation and plist lint passed, exit 0.
- Installed collector, guide, and skill matched the standalone source.
- The installer left private register, checkpoint, and correlation files byte-for-byte unchanged.
- The collector, collector tests, and companion skill retained their source hashes.
- Private state, snapshot, transcript, SQLite, and environment-file paths matched Git ignore rules.
- The new Git repository uses `main`, with no commit and no remote.

A separate live run used:

```sh
launchctl kickstart gui/$(id -u)/com.scubbo.work-activity
```

The run completed at 2026-11-03T20:17:12Z with LaunchAgent exit code 0.
OpenCode, Slack, and GitHub collection all reported `ok`.
The effective schedule remained daily at 10:00 local time.
This live check created a normal private snapshot and advanced checkpoints.
It did not move private activity into the source repository.
