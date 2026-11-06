#!/usr/bin/env python3
"""Install the local application; launchd supervises its process, not its jobs."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile


LABEL = "com.scubbo.work-tracker"
LEGACY_LABEL = "com.scubbo.work-activity"


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install(source, home, python, commands, path, skill=None, registration_skill=None):
    programs = {name: (source / name).read_bytes() for name in ("work_activity.py", "tracker.py")}
    package = source / "work_tracker"
    if not (package / "__init__.py").is_file():
        raise ValueError("Source must be a complete work-tracker checkout")
    skills = [(home / '.agents/skills/work-activity/SKILL.md', skill, '# Work activity'),
              (home / '.agents/skills/register-pr/SKILL.md', registration_skill, '# Register a PR with work-tracker')]
    fx_skill = home / '.fx/skills/register-pr/SKILL.md'
    if fx_skill.exists() or fx_skill.is_symlink():
        skills.append((fx_skill, registration_skill, '# Register a PR with work-tracker'))
    for destination, content, heading in skills:
        if content is not None and (destination.is_symlink() or destination.parent.is_symlink()
                                    or destination.exists() and heading not in destination.read_text()):
            raise ValueError(f'Refusing to replace an unrelated skill at {destination}')
    installed = home / ".local/lib/work-activity"
    for name, content in programs.items():
        write(installed / name, content)
    for module in package.glob("*.py"):
        write(installed / "work_tracker" / module.name, module.read_bytes())
    for name in ("ACTIVITY.md", "requirements.lock"):
        if (source / name).exists():
            write(installed / name, (source / name).read_bytes())
    for command, program in (("work-activity", "work_activity.py"), ("work-tracker", "tracker.py")):
        launcher = f"#!/bin/sh\nexec {shlex.quote(python)} {shlex.quote(str(installed / program))} \"$@\"\n"
        write(home / ".local/bin" / command, launcher.encode(), 0o700)
    config = {"commands": commands, "path": path}
    write(home / ".config/work-activity/config.json", (json.dumps(config, indent=2) + "\n").encode())
    for destination, content, heading in skills:
        if content is not None:
            write(destination, content)
    state = home / ".local/state/work-activity"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    logs = state / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("stdout.log", "stderr.log"):
        log = logs / name
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python, str(installed / "tracker.py"), "--state-dir", str(state), "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"PATH": path, "HOME": str(home)},
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
        "ProcessType": "Background",
        "Umask": 0o077,
    }
    destination = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    write(destination, plistlib.dumps(plist))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--source", type=Path, default=root,
                        help="Collector source directory (default: this checkout)")
    parser.add_argument("--load", action="store_true", help="Start the application at login; it owns and runs scheduled jobs")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer requires macOS")
    commands = {}
    for name in ("opencode", "gh", "slack-cli", "node"):
        command = shutil.which(name)
        if not command:
            parser.error(f"{name} is not installed/on PATH")
        # fnm's multishell paths disappear. Resolve to the actual installed executable.
        commands[name] = str(Path(command).resolve())
    from work_tracker.installation import activate, check_idle, prepare_python
    if sys.version_info < (3, 11):
        parser.error("The application and MCP SDK require Python 3.11 or later")
    if args.load:
        check_idle(Path.home(), LEGACY_LABEL)
    source = args.source.expanduser().resolve()
    python = prepare_python(source, Path.home())
    path = ":".join(dict.fromkeys([str(Path(commands["node"]).parent), "/opt/homebrew/bin",
                                    str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin",
                                    "/usr/sbin", "/sbin"]))
    skill = source / "skills/work-activity/SKILL.md"
    registration_skill = source / 'skills/register-pr/SKILL.md'
    plist = install(source, Path.home(), python, commands, path, skill.read_bytes(), registration_skill.read_bytes())
    subprocess.run(["plutil", "-lint", str(plist)], check=True)
    if args.load:
        activate(plist, Path.home(), LABEL, LEGACY_LABEL)
    print(f"Installed {plist}\nJobs are owned by the local application, not launchd.\nCLI: {Path.home() / '.local/bin/work-tracker'}")


if __name__ == "__main__":
    main()
